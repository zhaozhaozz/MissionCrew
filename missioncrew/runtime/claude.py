"""Claude Code 双向 ``stream-json`` 原生 Runtime provider。"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ..core.models import Backend, ExecutionConfig, RunResult
from . import adapters
from .base import (RuntimeCapabilities, RuntimeExecutionInfo, RuntimeInstance,
                   RuntimeProvider)
from .native import RuntimeProtocolError, emit_json, safe_emit


_QUESTION_TOOLS = {
    "AskUserQuestion", "ask_user_question", "request_user_input",
}
_WRITE_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write",
    "multi_edit", "notebook_edit",
}
_SHELL_TOOLS = {"Bash", "Shell", "bash", "shell"}
_NETWORK_TOOLS = {"WebFetch", "WebSearch", "web_fetch", "web_search"}
_PATH_KEYS = {
    "file_path", "path", "notebook_path", "target_path", "destination",
}


def _resolved_tool_paths(tool_input: dict, workdir: str) -> list[Path]:
    paths: list[Path] = []
    for key, value in tool_input.items():
        if key not in _PATH_KEYS or not isinstance(value, str) or not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(workdir) / path
        paths.append(path.resolve())
    return paths


def _is_within(path: Path, roots: list[str]) -> bool:
    for raw in roots:
        try:
            path.relative_to(Path(raw).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _policy_denial(tool_name: str, tool_input: dict,
                   config: ExecutionConfig) -> str:
    """在人工/自动批准前执行 MissionCrew 的硬权限边界。"""
    permissions = config.runtime_policy.permissions
    if permissions.filesystem == "read-only" and (
            tool_name in _WRITE_TOOLS or tool_name in _SHELL_TOOLS):
        return "当前 Runtime 文件系统策略为只读"
    if permissions.filesystem == "workspace-write" and tool_name in _WRITE_TOOLS:
        paths = _resolved_tool_paths(tool_input, config.workdir)
        if not paths or any(not _is_within(
                path, config.runtime_policy.writable_paths) for path in paths):
            return "目标文件不在 Runtime 可写目录中"
    if permissions.network == "deny" and (
            tool_name in _NETWORK_TOOLS or tool_name in _SHELL_TOOLS):
        # Claude CLI 没有独立的网络 sandbox；禁止 shell 才能保证 deny 不被绕过。
        return "当前 Runtime 策略禁止网络访问"
    return ""


def _sandbox_settings(config: ExecutionConfig) -> dict:
    """把统一文件系统策略映射为 Claude 的 OS 级 Bash sandbox。"""
    permissions = config.runtime_policy.permissions
    if permissions.filesystem == "full-access":
        return {"sandbox": {"enabled": False}}
    sandbox: dict = {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "filesystem": {
            "allowWrite": [str(Path(path).expanduser().resolve())
                           for path in config.runtime_policy.writable_paths],
        },
    }
    if permissions.filesystem == "read-only":
        sandbox["filesystem"]["denyWrite"] = [
            str(Path(config.workdir).expanduser().resolve()),
            *[str(Path(path).expanduser().resolve())
              for path in config.runtime_policy.readable_paths],
        ]
    return {"sandbox": sandbox}


def _normalized_questions(tool_input: dict) -> list[dict]:
    found = []
    for index, raw in enumerate(tool_input.get("questions") or []):
        if not isinstance(raw, dict):
            continue
        options = []
        for option in raw.get("options") or []:
            if isinstance(option, dict):
                options.append({
                    "label": str(option.get("label") or ""),
                    "description": str(option.get("description") or ""),
                })
            else:
                options.append({"label": str(option), "description": ""})
        found.append({
            "id": str(raw.get("id", index)),
            "header": str(raw.get("header") or f"问题 {index + 1}"),
            "question": str(raw.get("question") or ""),
            "options": options,
            "multiSelect": bool(raw.get("multiSelect", False)),
            "isSecret": bool(raw.get("isSecret", False)),
        })
    return found


def _answer_values(response: dict, question_id: str, index: int) -> list[str]:
    answers = response.get("answers") or {}
    if not isinstance(answers, dict):
        return []
    value = answers.get(question_id, answers.get(str(index), []))
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = value.get("answers", [])
    return [str(item) for item in value or []]


class _ClaudeSession:
    def __init__(self, command: list[str], backend_id: str,
                 session_key: str, workdir: str, persistent: bool):
        self.command = command
        self.backend_id = backend_id
        self.session_key = session_key
        self.workdir = str(Path(workdir).expanduser().resolve())
        self.persistent = persistent
        self.process = None
        self.session_id = ""
        self._signature = ""
        self._run_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._result_ready = threading.Event()
        self._active_config: Optional[ExecutionConfig] = None
        self._result: dict = {}
        self._output: list[str] = []
        self._saw_partial_text = False
        self._saw_partial_thinking = False
        # Claude 的后台 Agent 会跨越一次或多次顶层 result。task lifecycle
        # 消息是权威状态；tool id 只用于兼容尚未发送 task_started 的旧版本。
        self._native_agents: dict[str, dict] = {}
        self._pending_native_agents: set[str] = set()
        self._agent_tool_calls: dict[str, dict] = {}
        self._provisional_result_count = 0
        self._control_lock = threading.Lock()
        self._control_pending: dict[str, tuple[threading.Event, dict]] = {}
        self._stderr_tail: list[str] = []
        self._resume_attempt = ""
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

    def _args(self, config: ExecutionConfig, resume_id: str) -> list[str]:
        args = [
            *self.command,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-prompt-tool", "stdio",
            "--permission-mode", "manual",
        ]
        if config.backend.model:
            args += ["--model", config.backend.model]
        if config.effort:
            args += ["--effort", config.effort]
        args += ["--settings", json.dumps(
            _sandbox_settings(config), ensure_ascii=False,
            separators=(",", ":"))]
        extra_dirs = adapters._additional_allowed_dirs(
            self.workdir, config.allowed_dirs)
        if extra_dirs:
            args += ["--add-dir", *extra_dirs]
        if resume_id:
            args += ["--resume", resume_id]
        return args

    def _process_signature(self, config: ExecutionConfig) -> str:
        # resume id 不属于签名；进程重启时用当前 native id 恢复同一会话。
        args = self._args(config, "")
        env = adapters._runtime_env(config, "claude_code")
        return json.dumps({"command": args, "env": sorted(env.items())},
                          ensure_ascii=False, separators=(",", ":"))

    @property
    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def _ensure_process(self, config: ExecutionConfig) -> None:
        signature = self._process_signature(config)
        if self.alive and signature == self._signature:
            return
        if self.process:
            old_process = self.process
            self.process = None
            adapters._kill_process_group(old_process)
        self._signature = signature
        resume_id = self.session_id or config.session_id
        self._resume_attempt = resume_id
        self._stderr_tail = []
        command = self._args(config, resume_id)
        try:
            self.process = subprocess.Popen(
                command, cwd=self.workdir,
                env=adapters._runtime_env(config, "claude_code"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, encoding="utf-8",
                errors="replace", bufsize=1, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeProtocolError(f"命令不存在: {self.command[0]}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        safe_emit(config.emit, "status",
                  f"Claude stream-json 进程已{'恢复' if resume_id else '启动'}\n")

    def _write(self, payload: dict) -> None:
        process = self.process
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeProtocolError("Claude stream-json 进程未运行")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeProtocolError("Claude stream-json stdin 已关闭") from exc

    def run(self, config: ExecutionConfig) -> RunResult:
        with self._run_lock:
            if config.cancellation_requested():
                return RunResult(False, "执行已停止")
            adapters._refresh_session(config)
            if self.session_id and not config.session_id:
                self.close()
                self.session_id = ""
            recovery = not bool(config.session_id or self.session_id)
            prompt = adapters._session_input(config, recovery=recovery)
            self._active_config = config
            self.last_activity = time.time()
            self.last_task_id = config.task_id
            self.last_stage_name = config.stage_name
            self.last_project_id = config.project_id
            self.last_role_id = config.role_id
            self.last_model = config.backend.model
            self._result = {}
            self._output = []
            self._saw_partial_text = False
            self._saw_partial_thinking = False
            self._native_agents = {}
            self._pending_native_agents = set()
            self._agent_tool_calls = {}
            self._provisional_result_count = 0
            self._result_ready.clear()
            try:
                self._ensure_process(config)
                if config.cancellation_requested():
                    self.close()
                    return RunResult(False, "执行已停止")
                adapters._emit_execution_start(
                    config.emit, [*self.command, "--input-format", "stream-json"], prompt)
                if config.cancellation_requested():
                    self.close()
                    return RunResult(False, "执行已停止")
                self._write({
                    "type": "user",
                    "message": {"role": "user", "content": prompt},
                })
                if not self._result_ready.wait(config.timeout):
                    try:
                        self.interrupt(timeout=10)
                        self._result_ready.wait(10)
                    except Exception:
                        pass
                    return RunResult(False, f"执行超时({config.timeout}s)")
                result = self._result
                output = str(result.get("result") or "").strip()
                if not output:
                    output = "".join(self._output).strip()
                success = not bool(result.get("is_error")) and str(
                    result.get("subtype") or "success") == "success"
                if result.get("session_id"):
                    self.session_id = str(result["session_id"])
                    adapters._save_session(config, self.session_id)
                failure_detail = "\n".join((
                    output, str(result.get("error") or ""),
                    "".join(self._stderr_tail),
                ))
                if (not success and self._resume_attempt
                        and adapters._session_missing(failure_detail)):
                    missing_id = self._resume_attempt
                    adapters._clear_session(config)
                    self.session_id = ""
                    detail = failure_detail.strip()[-220:] or "会话不存在"
                    summary = (f"Claude session {missing_id} 无法恢复；"
                               f"本轮未创建新会话: {detail}")
                    return RunResult(False, summary)
                summary = output[-300:] or str(
                    result.get("error") or result.get("subtype") or "Claude 执行失败")
                return RunResult(success, summary, output=output)
            except Exception as exc:
                return RunResult(False, str(exc)[-300:])
            finally:
                self.last_activity = time.time()
                self._active_config = None

    def snapshot(self) -> RuntimeInstance:
        alive = self.alive
        state = ("running" if alive and self._active_config else
                 "idle" if alive else
                 "starting" if self._active_config else "disconnected")
        return RuntimeInstance(
            instance_id=f"claude:{self.backend_id}:{self.session_key}",
            backend_id=self.backend_id, adapter="claude_code",
            mode="persistent" if self.persistent else "one_shot",
            transport="claude-stream-json", state=state,
            pid=self.process.pid if alive else None,
            session_key=self.session_key if self.persistent else "",
            native_session_id=self.session_id, workdir=self.workdir,
            task_id=self.last_task_id, stage_name=self.last_stage_name,
            project_id=self.last_project_id, role_id=self.last_role_id,
            model=self.last_model, executable=Path(self.command[0]).name,
            started_at=self.created_at, last_activity=self.last_activity,
        )

    def _read_stdout(self) -> None:
        process = self.process
        assert process and process.stdout
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                safe_emit(self._emit(), "stdout", raw)
                continue
            message_type = message.get("type")
            if message_type == "control_request":
                threading.Thread(
                    target=self._handle_control_request,
                    args=(message,), daemon=True,
                ).start()
            elif message_type == "control_response":
                response = message.get("response") or {}
                request_id = str(response.get("request_id") or "")
                with self._control_lock:
                    pending = self._control_pending.pop(request_id, None)
                if pending:
                    pending[1].update(response)
                    pending[0].set()
            else:
                self._handle_message(message)
        # 配置变化会有意替换进程；旧 reader 的 EOF 不能把新一轮标成失败。
        if (process is self.process and self._active_config
                and not self._result_ready.is_set()):
            self._result = {
                "subtype": "error_during_execution", "is_error": True,
                "error": "Claude stream-json 进程已退出",
            }
            self._result_ready.set()

    def _read_stderr(self) -> None:
        process = self.process
        assert process and process.stderr
        for line in process.stderr:
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > 200:
                del self._stderr_tail[:-200]
            safe_emit(self._emit(), "stderr", line)

    def _emit(self):
        return self._active_config.emit if self._active_config else None

    def _emit_native_agent(self, status: str, meta: dict, **extra) -> None:
        payload = {
            "runtime": "claude",
            "status": status,
            "task_id": str(meta.get("task_id") or ""),
            "tool_use_id": str(meta.get("tool_use_id") or ""),
            "task_type": str(meta.get("task_type") or "local_agent"),
            "agent_type": str(meta.get("agent_type") or ""),
            "description": str(meta.get("description") or "Claude backend Agent"),
            **extra,
        }
        emit_json(self._emit(), "backend_agent", payload)

    def _register_native_agent(self, message: dict, *, inferred: bool = False) -> str:
        task_id = str(message.get("task_id") or "")
        tool_use_id = str(message.get("tool_use_id") or "")
        key = task_id or (f"tool:{tool_use_id}" if tool_use_id else "")
        if not key:
            return ""
        # task_started 可能晚于 Agent 工具的异步启动结果；用正式 task id
        # 替换兼容 key，避免一个后台 Agent 被计数两次。
        fallback = {}
        if task_id and tool_use_id:
            fallback_key = f"tool:{tool_use_id}"
            self._pending_native_agents.discard(fallback_key)
            fallback = self._native_agents.pop(fallback_key, {})
        previous = self._native_agents.get(key, {}) or fallback
        meta = {
            **previous,
            "task_id": task_id,
            "tool_use_id": tool_use_id or previous.get("tool_use_id", ""),
            "task_type": str(message.get("task_type") or
                             previous.get("task_type") or "local_agent"),
            "agent_type": str(message.get("subagent_type") or
                              previous.get("agent_type") or ""),
            "description": str(message.get("description") or
                               previous.get("description") or
                               "Claude backend Agent"),
        }
        self._native_agents[key] = meta
        self._pending_native_agents.add(key)
        if not previous:
            self._emit_native_agent("running", meta, inferred=inferred)
        return key

    def _finish_native_agent(self, message: dict, status: str) -> None:
        task_id = str(message.get("task_id") or "")
        tool_use_id = str(message.get("tool_use_id") or "")
        keys = [key for key in (task_id, f"tool:{tool_use_id}" if tool_use_id else "")
                if key]
        meta = next((self._native_agents.get(key) for key in keys
                     if key in self._native_agents), None)
        if not meta:
            return
        for key in keys:
            self._pending_native_agents.discard(key)
        self._emit_native_agent(
            status, meta,
            summary=str(message.get("summary") or ""),
            error=str((message.get("patch") or {}).get("error") or ""),
            usage=message.get("usage") or {},
        )

    def _handle_message(self, message: dict) -> None:
        message_type = message.get("type")
        if message_type == "stream_event":
            event = message.get("event") or {}
            if event.get("type") != "content_block_delta":
                return
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = str(delta.get("text") or "")
                self._saw_partial_text = True
                self._output.append(text)
                safe_emit(self._emit(), "text", text)
            elif delta.get("type") == "thinking_delta":
                self._saw_partial_thinking = True
                safe_emit(self._emit(), "thinking", str(delta.get("thinking") or ""))
            return
        if message_type == "assistant":
            for block in (message.get("message") or {}).get("content") or []:
                block_type = block.get("type")
                if block_type == "text" and not self._saw_partial_text:
                    text = str(block.get("text") or "")
                    self._output.append(text)
                    safe_emit(self._emit(), "text", text)
                elif block_type == "thinking" and not self._saw_partial_thinking:
                    safe_emit(self._emit(), "thinking", str(block.get("thinking") or ""))
                elif block_type == "tool_use":
                    tool_id = str(block.get("id") or "")
                    if tool_id and str(block.get("name") or "") in ("Agent", "Task"):
                        tool_input = block.get("input") or {}
                        if not isinstance(tool_input, dict):
                            tool_input = {}
                        self._agent_tool_calls[tool_id] = {
                            "description": str(tool_input.get("description") or
                                               "Claude backend Agent"),
                            "agent_type": str(tool_input.get("subagent_type") or ""),
                        }
                    detail = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    safe_emit(self._emit(), "tool",
                              f"{block.get('name', '?')} {detail[:800]}\n")
            return
        if message_type == "user":
            for block in (message.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    summary = adapters._summarize_tool_result(block.get("content"))
                    agent_call = self._agent_tool_calls.get(tool_use_id)
                    known_task = any(
                        meta.get("tool_use_id") == tool_use_id
                        for meta in self._native_agents.values())
                    if (agent_call and not known_task
                            and "async agent launched" in summary.casefold()):
                        self._register_native_agent({
                            "tool_use_id": tool_use_id,
                            "description": agent_call.get("description", ""),
                            "subagent_type": agent_call.get("agent_type", ""),
                            "task_type": "local_agent",
                        }, inferred=True)
                    mark = "✗ " if block.get("is_error") else ""
                    safe_emit(self._emit(), "tool_result",
                              mark + summary + "\n")
            return
        if message_type == "system":
            subtype = str(message.get("subtype") or "")
            if subtype == "init":
                native_id = str(message.get("session_id") or "")
                if native_id:
                    self.session_id = native_id
                    self._resume_attempt = ""
                    config = self._active_config
                    if config:
                        adapters._save_session(config, native_id)
                safe_emit(self._emit(), "status",
                          f"Claude 会话已连接 session={native_id} model={message.get('model', '')}\n")
                return
            if subtype == "task_started":
                task_type = str(message.get("task_type") or "")
                if task_type.endswith("_agent"):
                    self._register_native_agent(message)
                return
            if subtype == "task_progress":
                task_id = str(message.get("task_id") or "")
                key = task_id if task_id in self._native_agents else ""
                if not key and message.get("subagent_type"):
                    key = self._register_native_agent(message, inferred=True)
                if key:
                    meta = self._native_agents[key]
                    self._emit_native_agent(
                        "progress", meta,
                        summary=str(message.get("summary") or ""),
                        last_tool_name=str(message.get("last_tool_name") or ""),
                        usage=message.get("usage") or {},
                    )
                return
            if subtype == "task_updated":
                patch = message.get("patch") or {}
                status = str(patch.get("status") or "")
                if status in ("completed", "failed", "killed"):
                    # task_updated 描述 Task 记录的变化；真正可供父 Agent
                    # 消费的完成边界是随后到达的 task_notification。在这里
                    # 提前释放 pending 会让夹在两条消息间的 result 误结束本轮。
                    task_id = str(message.get("task_id") or "")
                    meta = self._native_agents.get(task_id)
                    if meta:
                        meta["updated_status"] = status
                return
            if subtype == "task_notification":
                status = str(message.get("status") or "completed")
                self._finish_native_agent(message, status)
                return
        if message_type == "result":
            self._result = message
            success = (not bool(message.get("is_error"))
                       and str(message.get("subtype") or "success") == "success")
            if success and self._pending_native_agents:
                self._provisional_result_count += 1
                emit_json(self._emit(), "backend_agent", {
                    "runtime": "claude",
                    "status": "waiting",
                    "pending": len(self._pending_native_agents),
                    "description": "Claude 主回合等待后台 Agent 汇总",
                    "provisional_results": self._provisional_result_count,
                })
                return
            self._result_ready.set()

    def _permission_response(self, tool_name: str, tool_input: dict,
                             request: Optional[dict] = None) -> dict:
        config = self._active_config
        if not config:
            return {"behavior": "deny", "message": "MissionCrew turn 已结束"}
        if tool_name in _QUESTION_TOOLS:
            questions = _normalized_questions(tool_input)
            if not config.interact:
                return {"behavior": "deny", "message": "当前执行入口无法接收用户回答"}
            response = config.interact("user_input_request", {
                "runtime": "claude", "request_type": tool_name,
                "questions": questions,
            })
            if response.get("decision") in ("cancel", "deny"):
                return {"behavior": "deny", "message": "用户取消了问题"}
            if tool_name in ("AskUserQuestion", "ask_user_question"):
                shaped: dict[str, str] = {}
                raw_questions = tool_input.get("questions") or []
                for index, raw in enumerate(raw_questions):
                    if not isinstance(raw, dict):
                        continue
                    values = _answer_values(
                        response, str(raw.get("id", index)), index)
                    if values and raw.get("question"):
                        shaped[str(raw["question"])] = ",".join(values)
                return {"behavior": "allow",
                        "updatedInput": {**tool_input, "answers": shaped}}
            nested = {}
            for index, question in enumerate(questions):
                values = _answer_values(response, question["id"], index)
                nested[question["id"]] = {"answers": values}
            return {"behavior": "allow",
                    "updatedInput": {**tool_input, "answers": nested}}

        policy = config.runtime_policy.permissions.approval
        payload = {
            "runtime": "claude", "request_type": "can_use_tool",
            "tool": tool_name, "details": tool_input,
        }
        request = request or {}
        suggestions = request.get("permission_suggestions") or []
        if not isinstance(suggestions, list):
            suggestions = []
        payload.update({
            "title": str(request.get("title") or ""),
            "description": str(request.get("description") or ""),
            "can_approve_session": bool(suggestions),
        })
        denial = _policy_denial(tool_name, tool_input, config)
        if denial:
            emit_json(config.emit, "permission_request",
                      {**payload, "status": "denied", "reason": denial})
            return {"behavior": "deny", "message": denial}
        if policy == "auto":
            emit_json(config.emit, "permission_request",
                      {**payload, "status": "auto_approved"})
            return {"behavior": "allow", "updatedInput": tool_input}
        if policy == "deny" or not config.interact:
            emit_json(config.emit, "permission_request",
                      {**payload, "status": "denied"})
            return {"behavior": "deny", "message": "MissionCrew 权限策略拒绝了工具调用"}
        response = config.interact("permission_request", payload)
        if response.get("decision") in ("approve", "approve_session"):
            allowed = {"behavior": "allow", "updatedInput": tool_input}
            if response.get("decision") == "approve_session" and suggestions:
                allowed["updatedPermissions"] = suggestions
            return allowed
        return {"behavior": "deny", "message": str(
            response.get("reason") or "用户拒绝了工具调用")}

    def _handle_control_request(self, message: dict) -> None:
        request_id = str(message.get("request_id") or "")
        request = message.get("request") or {}
        try:
            if request.get("subtype") != "can_use_tool":
                raise RuntimeProtocolError(
                    f"不支持的 Claude control request: {request.get('subtype')}")
            tool_input = request.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            result = self._permission_response(
                str(request.get("tool_name") or ""), tool_input, request)
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success", "request_id": request_id,
                    "response": result,
                },
            }
        except Exception as exc:
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "error", "request_id": request_id,
                    "error": str(exc),
                },
            }
        try:
            self._write(response)
        except Exception:
            pass

    def interrupt(self, timeout: float = 10) -> None:
        request_id = uuid.uuid4().hex
        ready = threading.Event()
        response: dict = {}
        with self._control_lock:
            self._control_pending[request_id] = (ready, response)
        self._write({
            "request_id": request_id,
            "type": "control_request",
            "request": {"subtype": "interrupt"},
        })
        if not ready.wait(timeout):
            with self._control_lock:
                self._control_pending.pop(request_id, None)
            raise TimeoutError("Claude interrupt 响应超时")
        if response.get("subtype") == "error":
            raise RuntimeProtocolError(str(response.get("error") or "interrupt 失败"))

    @property
    def active(self) -> bool:
        return self._active_config is not None

    def close(self) -> None:
        process = self.process
        self.process = None
        if process and process.poll() is None:
            adapters._kill_process_group(process)
        if self._active_config and not self._result_ready.is_set():
            self._result = {
                "subtype": "error_during_execution", "is_error": True,
                "error": "Claude session 已停止",
            }
            self._result_ready.set()


class ClaudeRuntimeProvider(RuntimeProvider):
    """把 Claude Code SDK-compatible stream-json 封装为统一 Runtime。"""

    def __init__(self, fallback: RuntimeProvider,
                 command: Optional[list[str]] = None):
        self.fallback = fallback
        self.command = command
        self._sessions: dict[str, _ClaudeSession] = {}
        self._guard = threading.Lock()

    def _command(self, backend: Backend) -> list[str]:
        return list(self.command or [backend.binary_path or "claude"])

    def start(self, config: ExecutionConfig) -> RunResult:
        if config.backend.command:
            return self.fallback.start(config)
        ephemeral = not config.session_key
        key = config.session_key or f"{config.task_id}:{config.stage_name}:{id(config)}"
        with self._guard:
            session = self._sessions.get(key)
            if session and not session.compatible(config.backend, config.workdir):
                session.close()
                self._sessions.pop(key, None)
                session = None
            if not session:
                session = _ClaudeSession(
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
        if backend.command:
            return self.fallback.capabilities(backend)
        return RuntimeCapabilities(
            session_reuse=True, structured_events=True,
            user_interaction=True, permission_control=True, interrupt=True)

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        if config.backend.command:
            return self.fallback.execution_info(config)
        return RuntimeExecutionInfo(
            mode="persistent" if config.session_key else "one_shot",
            transport="claude-stream-json",
        )

    def interrupt(self, backend: Backend, session_key: str = "") -> int:
        with self._guard:
            sessions = [session for key, session in self._sessions.items()
                        if session.backend_id == backend.id
                        and (not session_key or key == session_key)]
        interrupted = 0
        for session in sessions:
            try:
                if session.alive and session.active:
                    session.interrupt()
                    interrupted += 1
            except Exception:
                pass
        return interrupted

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return self.fallback.list_models(backend, timeout)

    def instances(self, backend: Backend) -> list[RuntimeInstance]:
        fallback = self.fallback.instances(backend)
        if backend.command:
            return fallback
        with self._guard:
            sessions = [session for session in self._sessions.values()
                        if session.backend_id == backend.id]
        return [*fallback, *(session.snapshot() for session in sessions)]

    def shutdown(self) -> None:
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
