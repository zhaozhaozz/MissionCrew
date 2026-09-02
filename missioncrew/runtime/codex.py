"""Codex ``app-server`` 原生 Runtime provider。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from ..core.models import Backend, ExecutionConfig, RunResult
from . import adapters
from .base import host_isolated_environ
from .base import (RuntimeCapabilities, RuntimeExecutionInfo, RuntimeInstance,
                   RuntimeProvider, RuntimeUsageMetric, RuntimeUsageSnapshot,
                   RuntimeUsageWindow)
from .native import (JsonLineProcess, OutputAssembler, RuntimeProtocolError,
                     build_usage, emit_json, safe_emit, usage_section)


def _unique_paths(paths: list[str]) -> list[str]:
    found: list[str] = []
    for raw in paths:
        if not raw:
            continue
        value = str(Path(raw).expanduser().resolve())
        if value not in found:
            found.append(value)
    return found


def _codex_usage_window(
        key: str, label_prefix: str, payload: dict) -> RuntimeUsageWindow | None:
    try:
        used = round(max(0.0, min(100.0, float(payload["usedPercent"]))), 2)
    except (KeyError, TypeError, ValueError):
        return None
    try:
        duration = int(payload.get("windowDurationMins"))
    except (TypeError, ValueError):
        duration = None
    label = (
        "5 小时" if duration == 300 else
        "本周" if duration == 10080 else
        f"{duration} 分钟" if duration else "当前周期"
    )
    if label_prefix:
        label = f"{label} · {label_prefix}"
    try:
        resets_at = float(payload.get("resetsAt"))
    except (TypeError, ValueError):
        resets_at = None
    return RuntimeUsageWindow(
        key=key, label=label, used_percent=used,
        resets_at=resets_at, duration_minutes=duration)


def parse_codex_usage(backend: Backend, payload: dict) -> RuntimeUsageSnapshot:
    """把 Codex app-server 的多限额桶规范化为统一账户快照。"""
    windows: list[RuntimeUsageWindow] = []
    by_limit_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict) and by_limit_id:
        groups = sorted(
            ((str(limit_id), snapshot)
             for limit_id, snapshot in by_limit_id.items()),
            key=lambda item: (item[0].lower() != "codex", item[0].lower()),
        )
    else:
        groups = [("", payload.get("rateLimits") or payload)]
    for limit_id, snapshot in groups:
        if not isinstance(snapshot, dict):
            continue
        display_id = limit_id.replace("_", " ").replace("-", " ").title()
        for bucket_name, bucket in (
            ("primary", snapshot.get("primary")),
            ("secondary", snapshot.get("secondary")),
        ):
            if not isinstance(bucket, dict):
                continue
            window = _codex_usage_window(
                f"{limit_id or 'default'}-{bucket_name}",
                display_id, bucket)
            if window:
                windows.append(window)

    rate_limits = payload.get("rateLimits")
    rate_limits = rate_limits if isinstance(rate_limits, dict) else {}
    plan = str(payload.get("planType") or rate_limits.get("planType") or "")
    credits = payload.get("credits")
    credits = credits if isinstance(credits, dict) else rate_limits.get("credits")
    metrics: list[RuntimeUsageMetric] = []
    if isinstance(credits, dict):
        balance = credits.get("balance")
        if credits.get("hasCredits") and balance is not None:
            metrics.append(RuntimeUsageMetric("Credits 余额", str(balance)))
        if credits.get("unlimited"):
            metrics.append(RuntimeUsageMetric("Credits", "不限"))
    return RuntimeUsageSnapshot(
        backend_id=backend.id, backend_name=backend.name,
        adapter=backend.adapter,
        status="ok" if windows else "unavailable",
        source="codex_app_server", plan=plan,
        windows=tuple(windows), metrics=tuple(metrics),
        message="" if windows else "Codex 未返回可识别的限额窗口",
    )


def _approval_policy(config: ExecutionConfig) -> str:
    # auto 仍使用 on-request，由 MissionCrew 逐次自动批准并留下审计事件。
    return "never" if config.runtime_policy.permissions.approval == "deny" else "on-request"


def _sandbox_mode(config: ExecutionConfig) -> str:
    return {
        "read-only": "read-only",
        "workspace-write": "workspace-write",
        "full-access": "danger-full-access",
    }[config.runtime_policy.permissions.filesystem]


def _sandbox_policy(config: ExecutionConfig) -> dict:
    permissions = config.runtime_policy.permissions
    # 本地协作平台默认放行网络:Agent Tool 的回环 API、git fetch/push 都
    # 依赖它,禁网只会把每次访问变成"沙箱失败→审批升级重试"的浪费;
    # 显式 network=deny 仍完全禁网。
    network = permissions.network != "deny"
    if permissions.filesystem == "read-only":
        return {"type": "readOnly", "networkAccess": network}
    if permissions.filesystem == "full-access":
        return {"type": "dangerFullAccess"}
    return {
        "type": "workspaceWrite",
        "writableRoots": _unique_paths(config.runtime_policy.writable_paths),
        "networkAccess": network,
    }


def _question_answers(response: dict, questions: list[dict]) -> dict:
    raw = response.get("answers") or {}
    answers: dict[str, dict[str, list[str]]] = {}
    for index, question in enumerate(questions):
        key = str(question.get("id", index))
        value = raw.get(key, raw.get(str(index), [])) if isinstance(raw, dict) else []
        if isinstance(value, str):
            value = [value]
        if isinstance(value, dict):
            value = value.get("answers", [])
        answers[key] = {"answers": [str(item) for item in value or []]}
    return answers


# app-server ``thread/tokenUsage/updated`` 的 tokenUsage:last 为本轮、total 为
# 会话累计,均为驼峰字段;cachedInputTokens 是 inputTokens 的子集
_USAGE_FIELDS = {"input": "inputTokens", "cache_read": "cachedInputTokens",
                 "cache_write": "cacheWriteInputTokens", "output": "outputTokens",
                 "reasoning": "reasoningOutputTokens", "total": "totalTokens"}


def _usage_payload(token_usage: dict) -> dict:
    return build_usage(token_usage,
                       turn=usage_section(token_usage.get("last"), _USAGE_FIELDS),
                       total=usage_section(token_usage.get("total"), _USAGE_FIELDS),
                       context_window=token_usage.get("modelContextWindow"))


class _CodexSession:
    def __init__(self, command: list[str], backend_id: str,
                 session_key: str, workdir: str, persistent: bool):
        self.command = command
        self.backend_id = backend_id
        self.session_key = session_key
        self.workdir = str(Path(workdir).expanduser().resolve())
        self.persistent = persistent
        self.client: Optional[JsonLineProcess] = None
        self.thread_id = ""
        self._signature = ""
        self._run_lock = threading.Lock()
        self._turn_done = threading.Event()
        self._active_config: Optional[ExecutionConfig] = None
        self._active_turn_id = ""
        self._turn_status = ""
        self._turn_error = ""
        self._assembler = OutputAssembler()
        self._saw_text_delta = False
        self._message_item_id = ""
        self._restarting_client = False
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.last_task_id = ""
        self.last_stage_name = ""
        self.last_project_id = ""
        self.last_role_id = ""
        self.last_model = ""

    def compatible(self, backend: Backend, workdir: str) -> bool:
        return (self.backend_id == backend.id
                and self.workdir == str(Path(workdir).expanduser().resolve()))

    @staticmethod
    def _process_signature(config: ExecutionConfig, command: list[str]) -> str:
        env = adapters._runtime_env(config, "codex")
        return json.dumps({"command": command, "env": sorted(env.items())},
                          ensure_ascii=False, separators=(",", ":"))

    def _thread_params(self, config: ExecutionConfig) -> dict:
        params = {
            "cwd": self.workdir,
            "approvalPolicy": _approval_policy(config),
            "approvalsReviewer": "user",
            "sandbox": _sandbox_mode(config),
            "runtimeWorkspaceRoots": _unique_paths(config.allowed_dirs),
        }
        if config.backend.model:
            params["model"] = config.backend.model
        return params

    def _ensure_client(self, config: ExecutionConfig) -> None:
        signature = self._process_signature(config, self.command)
        if self.client and self.client.alive and signature == self._signature:
            return
        if self.client:
            self._restarting_client = True
            try:
                self.client.close()
                self.client = None
            finally:
                self._restarting_client = False
        self._signature = signature
        self.client = JsonLineProcess(
            self.command, cwd=self.workdir,
            env=adapters._runtime_env(config, "codex"),
            notification_handler=self._notification,
            request_handler=self._server_request,
            stderr_handler=lambda text: safe_emit(
                self._active_config.emit if self._active_config else None,
                "stderr", text),
            exit_handler=self._process_exited,
        )
        self.client.connect()
        self.client.request("initialize", {
            "clientInfo": {
                "name": "missioncrew",
                "title": "MissionCrew",
                "version": "0.2.0",
            },
            "capabilities": {"experimentalApi": True},
        }, timeout=30)
        self.client.notify("initialized")

        resume_id = self.thread_id or config.session_id
        if resume_id:
            try:
                result = self.client.request(
                    "thread/resume",
                    {"threadId": resume_id, **self._thread_params(config)},
                    timeout=30,
                )
            except Exception as exc:
                adapters._clear_session(config)
                self.thread_id = ""
                self.client.close()
                self.client = None
                raise RuntimeProtocolError(
                    f"Codex thread {resume_id} 无法恢复；本轮未创建新会话: {exc}") from exc
        else:
            result = self.client.request(
                "thread/start", self._thread_params(config), timeout=30)
        result = result if isinstance(result, dict) else {}
        thread = result.get("thread") or {}
        native_id = str(thread.get("id") or resume_id or "")
        if not native_id:
            raise RuntimeProtocolError("Codex app-server 未返回 thread id")
        self.thread_id = native_id
        adapters._save_session(config, native_id)
        safe_emit(config.emit, "status", f"Codex 会话已{'恢复' if resume_id else '启动'} thread={native_id}\n")

    def run(self, config: ExecutionConfig) -> RunResult:
        with self._run_lock:
            if config.cancellation_requested():
                return RunResult(False, "执行已停止")
            adapters._refresh_session(config)
            if self.thread_id and not config.session_id:
                # 持久记录已被显式清理，不继续使用仅存在内存中的旧 thread。
                self.close()
                self.thread_id = ""
            self._active_config = config
            self.last_activity = time.time()
            self.last_task_id = config.task_id
            self.last_stage_name = config.stage_name
            self.last_project_id = config.project_id
            self.last_role_id = config.role_id
            self.last_model = config.backend.model
            prompt, injection_mode = adapters._session_input(
                config, recovery=not bool(config.session_id))
            adapters._emit_execution_start(config.emit, [*self.command], prompt)
            self._turn_done.clear()
            self._turn_status = ""
            self._turn_error = ""
            self._active_turn_id = ""
            self._assembler = OutputAssembler()
            self._saw_text_delta = False
            self._message_item_id = ""
            try:
                self._ensure_client(config)
                assert self.client
                if config.cancellation_requested():
                    self.close()
                    return RunResult(False, "执行已停止")
                params = {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": self.workdir,
                    "approvalPolicy": _approval_policy(config),
                    "approvalsReviewer": "user",
                    "sandboxPolicy": _sandbox_policy(config),
                    "runtimeWorkspaceRoots": _unique_paths(config.allowed_dirs),
                }
                if config.backend.model:
                    params["model"] = config.backend.model
                if config.effort:
                    params["effort"] = config.effort
                    params["summary"] = "detailed"
                if config.cancellation_requested():
                    self.close()
                    return RunResult(False, "执行已停止")
                response = self.client.request(
                    "turn/start", params,
                    timeout=(min(30, config.timeout)
                             if config.timeout is not None else 30))
                response = response if isinstance(response, dict) else {}
                self._active_turn_id = str((response.get("turn") or {}).get("id") or
                                           self._active_turn_id)
                if not self._turn_done.wait(config.timeout):
                    if self._active_turn_id:
                        try:
                            self.client.request("turn/interrupt", {
                                "threadId": self.thread_id,
                                "turnId": self._active_turn_id,
                            }, timeout=10)
                            self._turn_done.wait(10)
                        except Exception:
                            pass
                    return RunResult(False, f"执行超时({config.timeout}s)")
                output = self._assembler.text.strip()
                success = self._turn_status == "completed"
                if success and self.thread_id:
                    adapters._save_session(
                        config, self.thread_id, injection_mode,
                        adapters._turn_bytes(prompt, output))
                # 成功但零输出时 summary 保持为空:交给聊天层的"无输出"
                # 守卫处理,固定文案不能被当成 Agent 回复发布
                if self._turn_error:
                    summary = self._turn_error
                elif success:
                    summary = "Codex turn completed" if output else ""
                else:
                    summary = f"Codex turn {self._turn_status or 'failed'}"
                return RunResult(success, summary[-300:], output=output)
            except Exception as exc:
                return RunResult(False, str(exc)[-300:])
            finally:
                self.last_activity = time.time()
                self._active_config = None

    def reclaimable(self, cutoff: float) -> bool:
        """空闲回收判定:turn 进行中不回收;thread id 已持久化可恢复。"""
        return self._active_config is None and self.last_activity < cutoff

    def snapshot(self) -> RuntimeInstance:
        client = self.client
        alive = bool(client and client.alive)
        state = ("running" if alive and self._active_config else
                 "idle" if alive else
                 "starting" if self._active_config else "disconnected")
        process = client.process if client else None
        return RuntimeInstance(
            instance_id=f"codex:{self.backend_id}:{self.session_key}",
            backend_id=self.backend_id, adapter="codex",
            mode="persistent" if self.persistent else "one_shot",
            transport="codex-app-server", state=state,
            pid=process.pid if process and process.poll() is None else None,
            session_key=self.session_key if self.persistent else "",
            native_session_id=self.thread_id, workdir=self.workdir,
            task_id=self.last_task_id, stage_name=self.last_stage_name,
            project_id=self.last_project_id, role_id=self.last_role_id,
            model=self.last_model, executable=Path(self.command[0]).name,
            started_at=self.created_at, last_activity=self.last_activity,
        )

    def _notification(self, method: str, params: dict) -> None:
        config = self._active_config
        if not config:
            return
        emit = config.emit
        if method == "turn/started":
            turn = params.get("turn") or {}
            self._active_turn_id = str(turn.get("id") or self._active_turn_id)
            safe_emit(emit, "status", f"Codex turn 已启动 {self._active_turn_id}\n")
            return
        if method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            if delta:
                item_id = str(params.get("itemId") or "")
                if item_id != self._message_item_id:
                    # 新的一条 agentMessage 开始:与上一条之间插横线分隔
                    self._finish_message_line()
                    self._message_item_id = item_id
                self._saw_text_delta = True
                self._append_output(emit, delta)
            return
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            safe_emit(emit, "thinking", str(params.get("delta") or ""))
            return
        if method in ("item/plan/delta", "turn/plan/updated"):
            safe_emit(emit, "plan", str(params.get("delta") or
                                         json.dumps(params.get("plan") or params,
                                                    ensure_ascii=False)))
            return
        if method == "item/started":
            self._emit_item(emit, params.get("item") or {}, completed=False)
            return
        if method == "item/completed":
            self._emit_item(emit, params.get("item") or {}, completed=True)
            return
        if method in ("item/commandExecution/outputDelta", "command/exec/outputDelta"):
            safe_emit(emit, "tool_result", str(params.get("delta") or ""))
            return
        if method in ("item/fileChange/outputDelta", "item/fileChange/patchUpdated",
                      "turn/diff/updated"):
            value = params.get("delta") or params.get("patch") or params.get("diff")
            safe_emit(emit, "file_change", str(value or ""))
            return
        if method in ("thread/compacted", "thread/compact/start"):
            adapters._mark_compact(config)
            safe_emit(emit, "status",
                      "检测到上下文压缩;下一轮将重新注入完整公共上下文\n")
            return
        if method == "thread/tokenUsage/updated":
            usage = _usage_payload(params.get("tokenUsage") or {})
            if usage:
                emit_json(emit, "usage", usage)
            return
        if method == "error":
            error = params.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            if message:
                self._turn_error = str(message)
                safe_emit(emit, "stderr", str(message) + "\n")
            return
        if method == "turn/completed" and str(params.get("threadId") or "") == self.thread_id:
            turn = params.get("turn") or {}
            self._turn_status = str(turn.get("status") or "failed")
            error = turn.get("error") or {}
            if error:
                self._turn_error = str(
                    error.get("message") if isinstance(error, dict) else error)
            if not self._assembler.text:
                # 兜底聚合 turn.items:同样经聚合器,空白消息不产生空横线段
                for item in turn.get("items") or []:
                    if item.get("type") == "agentMessage" and item.get("text"):
                        self._assembler.append(str(item["text"]))
                        self._assembler.finish_message()
            safe_emit(emit, "status", f"Codex turn {self._turn_status}\n")
            self._turn_done.set()

    def _finish_message_line(self) -> None:
        """一条完整输出结束:空白消息丢弃,有内容才在下一条前插横线分隔。"""
        self._assembler.finish_message()

    def _append_output(self, emit, text: str) -> None:
        """输出正文统一入口:消息之间补 Markdown 横线,过程与结论可区分。"""
        safe_emit(emit, "text", self._assembler.append(text))

    def _emit_item(self, emit, item: dict, *, completed: bool) -> None:
        item_type = str(item.get("type") or "")
        if item_type == "agentMessage":
            text = str(item.get("text") or "")
            if completed and text and not self._saw_text_delta:
                self._append_output(emit, text)
            if completed:
                self._finish_message_line()
        elif item_type == "commandExecution":
            if completed:
                safe_emit(emit, "status", f"命令结束 exit={item.get('exitCode')} status={item.get('status')}\n")
            else:
                safe_emit(emit, "tool", f"Bash {item.get('command', '')}\n")
        elif item_type == "fileChange":
            paths = [str(change.get("path") or change.get("filePath") or "")
                     for change in item.get("changes") or [] if isinstance(change, dict)]
            safe_emit(emit, "file_change",
                      f"{'完成' if completed else '修改'}文件: {', '.join(filter(None, paths))}\n")
        elif item_type in ("mcpToolCall", "dynamicToolCall", "collabAgentToolCall"):
            name = item.get("tool") or item_type
            detail = item.get("result") if completed else item.get("arguments")
            safe_emit(emit, "tool_result" if completed else "tool",
                      f"{name} {json.dumps(detail, ensure_ascii=False, default=str)[:800]}\n")
        elif item_type == "webSearch":
            safe_emit(emit, "tool", f"WebSearch {item.get('query', '')}\n")
        elif item_type == "plan" and completed:
            safe_emit(emit, "plan", str(item.get("text") or ""))

    def _decision(self, kind: str, params: dict) -> dict:
        config = self._active_config
        if not config:
            return {"decision": "cancel"}
        policy = config.runtime_policy.permissions.approval
        payload = {"runtime": "codex", "request_type": kind,
                   "details": params, "can_approve_session": True}
        if policy == "auto":
            emit_json(config.emit, "permission_request",
                      {**payload, "status": "auto_approved"})
            return {"decision": "approve"}
        if policy == "deny" or not config.interact:
            emit_json(config.emit, "permission_request",
                      {**payload, "status": "denied"})
            return {"decision": "deny"}
        return config.interact("permission_request", payload)

    @staticmethod
    def _codex_decision(response: dict) -> str:
        return {
            "approve": "accept",
            "approve_session": "acceptForSession",
            "deny": "decline",
            "cancel": "cancel",
        }.get(str(response.get("decision") or "deny"), "decline")

    def _server_request(self, method: str, params: dict) -> dict:
        config = self._active_config
        if method == "item/tool/requestUserInput":
            questions = params.get("questions") or []
            payload = {"runtime": "codex", "request_type": method,
                       "questions": questions}
            if not config or not config.interact:
                return {"answers": {}}
            response = config.interact("user_input_request", payload)
            if response.get("decision") in ("cancel", "deny"):
                return {"answers": {}}
            return {"answers": _question_answers(response, questions)}

        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval", "item/tool/requestApproval"):
            return {"decision": self._codex_decision(self._decision(method, params))}

        if method in ("execCommandApproval", "applyPatchApproval"):
            response = self._decision(method, params)
            decision = {
                "approve": "approved",
                "approve_session": "approved_for_session",
                "deny": "denied",
                "cancel": "abort",
            }.get(str(response.get("decision") or "deny"), "denied")
            return {"decision": decision}

        if method == "item/permissions/requestApproval":
            response = self._decision(method, params)
            approved = response.get("decision") in ("approve", "approve_session")
            return {
                "permissions": (params.get("permissions") if approved else
                                {"network": None, "fileSystem": None}),
                "scope": "session" if response.get("decision") == "approve_session" else "turn",
            }

        if method == "mcpServer/elicitation/request":
            # 普通 MCP elicitation 的 schema 差异很大；先明确取消而不是构造
            # 不匹配的 content。request_user_input 走上面的完整交互通道。
            safe_emit(config.emit if config else None, "status",
                      "暂不支持的 MCP elicitation 已取消\n")
            return {"action": "cancel", "content": None, "_meta": None}
        if method == "currentTime/read":
            return {"currentTimeAt": int(time.time())}
        raise RuntimeProtocolError(f"不支持的 Codex server request: {method}")

    def _process_exited(self, error: RuntimeProtocolError) -> None:
        if self._active_config and not self._restarting_client:
            self._turn_error = str(error)
            self._turn_status = "failed"
            self._turn_done.set()

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def interrupt(self) -> bool:
        client = self.client
        turn_id = self._active_turn_id
        if not client or not client.alive or not turn_id:
            return False
        client.request("turn/interrupt", {
            "threadId": self.thread_id, "turnId": turn_id,
        }, timeout=10)
        return True


class CodexRuntimeProvider(RuntimeProvider):
    """把 Codex app-server 的 thread/turn 能力封装为统一 Runtime。"""

    def effort_catalog(self) -> dict[str, list[str]]:
        # 原生 turn/start.effort;具体模型未必支持全部档位,越界时 app-server
        # 自行报错并照常回流到频道
        return {"codex": ["minimal", "low", "medium", "high",
                          "xhigh", "max", "ultra"]}

    def __init__(self, fallback: RuntimeProvider,
                 command: Optional[list[str]] = None):
        self.fallback = fallback
        self.command = command
        self._sessions: dict[str, _CodexSession] = {}
        self._guard = threading.Lock()

    def _command(self, backend: Backend) -> list[str]:
        return list(self.command or [backend.binary_path or "codex", "app-server"])

    def start(self, config: ExecutionConfig) -> RunResult:
        ephemeral = not config.session_key
        key = config.session_key or f"{config.task_id}:{config.stage_name}:{id(config)}"
        with self._guard:
            session = self._sessions.get(key)
            if session and not session.compatible(config.backend, config.workdir):
                session.close()
                self._sessions.pop(key, None)
                session = None
            if not session:
                session = _CodexSession(
                    self._command(config.backend), config.backend.id, key,
                    config.workdir, persistent=not ephemeral)
                self._sessions[key] = session
        try:
            return session.run(config)
        finally:
            if ephemeral:
                with self._guard:
                    self._sessions.pop(key, None)
                session.close()

    def stop(self, backend: Backend, session_key: str = "") -> int:
        stopped = self.fallback.stop(backend, session_key)
        with self._guard:
            keys = [key for key, session in self._sessions.items()
                    if session.backend_id == backend.id
                    and (not session_key or key == session_key)]
            sessions = [self._sessions.pop(key) for key in keys]
        for session in sessions:
            session.close()
        return stopped + len(sessions)

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            session_reuse=True, structured_events=True,
            user_interaction=True, permission_control=True, interrupt=True,
            account_usage=True)

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        return RuntimeExecutionInfo(
            mode="persistent" if config.session_key else "one_shot",
            transport="codex-app-server",
        )

    def interrupt(self, backend: Backend, session_key: str = "") -> int:
        with self._guard:
            sessions = [session for key, session in self._sessions.items()
                        if session.backend_id == backend.id
                        and (not session_key or key == session_key)]
        interrupted = 0
        for session in sessions:
            try:
                interrupted += int(session.interrupt())
            except Exception:
                pass
        return interrupted

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        client = JsonLineProcess(
            self._command(backend), cwd=str(Path.cwd()),
            env=host_isolated_environ(),
            notification_handler=lambda _method, _params: None,
            request_handler=lambda _method, _params: {},
        )
        try:
            client.connect()
            client.request("initialize", {
                "clientInfo": {"name": "missioncrew", "title": "MissionCrew",
                               "version": "0.2.0"},
            }, timeout=min(timeout, 30))
            client.notify("initialized")
            cursor = None
            models: list[str] = []
            while True:
                result = client.request("model/list", {
                    "cursor": cursor, "includeHidden": False, "limit": 100,
                }, timeout=min(timeout, 30))
                result = result if isinstance(result, dict) else {}
                for item in result.get("data") or []:
                    model = str(item.get("model") or item.get("id") or "")
                    if model and model not in models:
                        models.append(model)
                cursor = result.get("nextCursor")
                if not cursor:
                    return models
        except Exception:
            return self.fallback.list_models(backend, timeout)
        finally:
            client.close()

    def account_usage(self, backend: Backend,
                      timeout: int = 15) -> RuntimeUsageSnapshot:
        client = JsonLineProcess(
            self._command(backend), cwd=str(Path.cwd()),
            env=host_isolated_environ(),
            notification_handler=lambda _method, _params: None,
            request_handler=lambda _method, _params: {},
        )
        try:
            client.connect()
            client.request("initialize", {
                "clientInfo": {
                    "name": "missioncrew", "title": "MissionCrew",
                    "version": "0.2.0",
                },
                "capabilities": {"experimentalApi": True},
            }, timeout=min(timeout, 30))
            client.notify("initialized")
            result = client.request(
                "account/rateLimits/read", {}, timeout=min(timeout, 30))
            return parse_codex_usage(
                backend, result if isinstance(result, dict) else {})
        except Exception:
            return RuntimeUsageSnapshot(
                backend_id=backend.id, backend_name=backend.name,
                adapter=backend.adapter, status="unavailable",
                source="codex_app_server",
                message="Codex app-server 限额接口暂时不可用",
            )
        finally:
            client.close()

    def instances(self, backend: Backend) -> list[RuntimeInstance]:
        with self._guard:
            sessions = [session for session in self._sessions.values()
                        if session.backend_id == backend.id]
        return [session.snapshot() for session in sessions]

    def cleanup_idle(self, cutoff: float) -> int:
        with self._guard:
            stale = [key for key, session in self._sessions.items()
                     if session.persistent and session.reclaimable(cutoff)]
            sessions = [self._sessions.pop(key) for key in stale]
        for session in sessions:
            session.close()
        return len(sessions)

    def shutdown(self) -> None:
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
