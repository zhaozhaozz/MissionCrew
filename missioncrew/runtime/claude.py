"""Claude Code 双向 ``stream-json`` 原生 Runtime provider。"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.models import Backend, ExecutionConfig, RunResult
from . import adapters
from .base import (RuntimeCapabilities, RuntimeExecutionInfo, RuntimeInstance,
                   RuntimeProvider, RuntimeUsageSnapshot)
from .native import (OutputAssembler, RuntimeProtocolError, build_usage, emit_json,
                     safe_emit, usage_section)
from .usage import probe_claude_usage

# 后台任务结束后 Claude CLI 会自发开启新 turn(无用户输入)汇报结果。
# 平台层(ChatEngine)注册该回调,把这类"自唤醒 turn"落成频道里的新运行。
_WAKE_HANDLER: Optional[Callable[[dict], None]] = None


def set_wake_handler(handler: Optional[Callable[[dict], None]]) -> None:
    global _WAKE_HANDLER
    _WAKE_HANDLER = handler


# ``result`` 消息的 usage 是 Anthropic API 字段(本轮累计;input_tokens 不含缓存
# 读写),另带 total_cost_usd / duration_ms。raw 只保留用量相关键,不复制回复正文。
_RESULT_USAGE_FIELDS = {"input": "input_tokens", "cache_read": "cache_read_input_tokens",
                        "cache_write": "cache_creation_input_tokens",
                        "output": "output_tokens"}
_RESULT_USAGE_KEYS = ("usage", "total_cost_usd", "duration_ms", "duration_api_ms",
                      "num_turns", "modelUsage")


def _result_usage(result: dict) -> dict:
    raw = {key: result[key] for key in _RESULT_USAGE_KEYS if key in result}
    return build_usage(raw, turn=usage_section(result.get("usage"), _RESULT_USAGE_FIELDS),
                       cost_usd=result.get("total_cost_usd"),
                       duration_ms=result.get("duration_ms"))


def _agent_usage(usage: object) -> dict:
    """后台 Agent(task_progress/task_notification)的 usage:累计 total_tokens 与
    tool_uses/duration_ms。"""
    if not isinstance(usage, dict) or not usage:
        return {}
    return build_usage(usage, total=usage_section(usage, {"total": "total_tokens"}),
                       tool_uses=usage.get("tool_uses"), duration_ms=usage.get("duration_ms"))


@dataclass
class _TurnSink:
    """一个 turn 的输出汇聚点。运行 turn 落到 config.emit;自唤醒 turn
    没有对应运行,事件先缓冲,turn 结束后整体交给 wake handler 回放。"""
    emit: Optional[Callable[[str, str], None]] = None
    assembler: OutputAssembler = field(default_factory=OutputAssembler)
    saw_partial_text: bool = False
    saw_partial_thinking: bool = False
    events: list[tuple[str, str]] = field(default_factory=list)
    # 自唤醒 turn 对应的后台任务(CLI 一条 task_notification 起一个 turn)
    tasks: list[dict] = field(default_factory=list)


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


def _os_sandbox_requested() -> bool:
    """Claude OS 级 Bash 沙箱默认关闭,MISSIONCREW_CLAUDE_SANDBOX=on 启用。

    默认关闭的原因:该沙箱的网络命名空间连宿主回环/私网都不可达、域名默认
    全部拒绝,对纯本地协作限制过强;平台其余 Runtime(ACP yolo 系)本就无
    OS 沙箱,codex 也有审批升级逃生门。关闭后文件写入边界仍由 _policy_denial
    的应用层硬检查约束。
    """
    return os.environ.get(
        "MISSIONCREW_CLAUDE_SANDBOX", "").strip().lower() in ("1", "on", "true")


def _sandbox_settings(config: ExecutionConfig) -> dict:
    """把统一文件系统策略映射为 Claude 的 OS 级 Bash sandbox。"""
    permissions = config.runtime_policy.permissions
    if permissions.filesystem == "full-access" or not _os_sandbox_requested():
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
        # 沙箱把 Bash 放进隔离网络命名空间,宿主回环与私网段(NO_PROXY 同样
        # 排除)一律不可达,network.allowedDomains 救不了 Agent Tool API;
        # 唯一可靠通路是把该 CLI 排除出沙箱:命令仍经平台 can_use_tool 审批,
        # 批准后在宿主侧执行。通配前缀是为了同时匹配 missioncrew-tool 与
        # `"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool` 两种
        # 调用形式;代价是含该片段的复合命令也会脱离沙箱,由审批层兜底。
        "excludedCommands": ["* -m missioncrew.agent_tool *",
                             "missioncrew-tool *"],
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
        # Claude 会在频道 turn 之外自发执行后台任务汇报。显式 run 与这类
        # wake turn 必须在同一状态边界串行，否则旧 wake sink 会吞掉新 run
        # 的事件和最终 result，令调用线程永久等待。
        self._turn_condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._result_ready = threading.Event()
        self._active_config: Optional[ExecutionConfig] = None
        self._result: dict = {}
        self._run_sink = _TurnSink()
        # 后台命令(run_in_background 的 local_bash 等)跨 turn 存活;
        # 结束时 CLI 自发唤醒模型,输出经 _wake_sink 缓冲后交给 wake handler。
        # CLI 把每条 task_notification 排成队列,turn 之间按先后各起一个
        # 自唤醒 turn;turn 内到达且随后还有模型调用的通知则就地喂给模型,
        # 不再单独唤醒。_pending_wakes 按同样规则排队,唤醒 turn 开始时
        # 取队首一条作为它的任务,这样 origin_trigger 不会串到别的唤醒上。
        self._background_tasks: dict[str, dict] = {}
        self._wake_sink: Optional[_TurnSink] = None
        self._pending_wakes: list[dict] = []
        self._turn_serial = 0
        # 最近一轮运行的配置:自唤醒 turn 没有对应运行,工具审批沿用它的策略
        self._last_config: Optional[ExecutionConfig] = None
        # Claude 的后台 Agent 会跨越一次或多次顶层 result。task lifecycle
        # 消息是权威状态；tool id 只用于兼容尚未发送 task_started 的旧版本。
        self._native_agents: dict[str, dict] = {}
        self._pending_native_agents: set[str] = set()
        self._agent_tool_calls: dict[str, dict] = {}
        self._provisional_result_count = 0
        # 进程带着未结束的后台命令被杀后,下次 --resume 启动时 CLI 会在首个
        # init 之前自行补一条 task_notification(stopped),并紧跟一对不调模型
        # 的空 init/result。这对 result 不属于本轮发出的消息,不能当本轮结束。
        self._process_inits = 0
        self._stale_replays = 0
        self._turn_activity = False
        self._interrupt_requested = False
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
        self._process_inits = 0
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
            with self._turn_condition:
                while self._wake_sink is not None:
                    if config.cancellation_requested():
                        return RunResult(False, "执行已停止")
                    self._turn_condition.wait(0.1)
                if config.cancellation_requested():
                    return RunResult(False, "执行已停止")
                adapters._refresh_session(config)
                if self.session_id and not config.session_id:
                    self.close()
                    self.session_id = ""
                recovery = not bool(config.session_id or self.session_id)
                prompt, injection_mode = adapters._session_input(
                    config, recovery=recovery)
                # 与 _current_sink 使用同一条件锁：从确认无 wake turn 到登记
                # 当前 run 之间不留可新建 _wake_sink 的竞态窗口。
                self._active_config = config
                self._last_config = config
                self._turn_serial += 1
                self.last_activity = time.time()
                self.last_task_id = config.task_id
                self.last_stage_name = config.stage_name
                self.last_project_id = config.project_id
                self.last_role_id = config.role_id
                self.last_model = config.backend.model
                self._result = {}
                self._run_sink = _TurnSink(emit=config.emit)
                self._native_agents = {}
                self._pending_native_agents = set()
                self._agent_tool_calls = {}
                self._provisional_result_count = 0
                self._stale_replays = 0
                self._turn_activity = False
                self._interrupt_requested = False
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
                    output = self._run_sink.assembler.text.strip()
                success = not bool(result.get("is_error")) and str(
                    result.get("subtype") or "success") == "success"
                if result.get("session_id"):
                    self.session_id = str(result["session_id"])
                    adapters._save_session(
                        config, self.session_id, injection_mode,
                        adapters._turn_bytes(prompt, output))
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
                # 成功但零输出时 summary 保持为空:交给聊天层的"无输出"
                # 守卫处理,不能把 result subtype 字面值("success")当回复发布
                summary = output[-300:]
                if not summary:
                    summary = str(result.get("error") or (
                        "" if success else
                        result.get("subtype") or "Claude 执行失败"))
                return RunResult(success, summary, output=output)
            except Exception as exc:
                return RunResult(False, str(exc)[-300:])
            finally:
                self.last_activity = time.time()
                with self._turn_condition:
                    self._active_config = None
                    self._turn_condition.notify_all()

    def snapshot(self) -> RuntimeInstance:
        alive = self.alive
        with self._turn_condition:
            active = self._active_config is not None
            background_tasks = len(self._background_tasks)
        state = ("running" if alive and active else
                 "idle" if alive else
                 "starting" if active else "disconnected")
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
            background_tasks=background_tasks,
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
        sink = self._current_sink()
        return sink.emit if sink else None

    def _register_background_task(self, message: dict) -> None:
        task_id = str(message.get("task_id") or "")
        if not task_id or task_id in self._background_tasks:
            return
        config = self._active_config
        meta = {
            "task_id": task_id,
            "task_type": str(message.get("task_type") or "local_bash"),
            "description": str(message.get("description") or "后台命令"),
            "started_at": time.time(),
            # 记录发起 turn 的触发消息:唤醒汇报按它继承派发语义
            # (人类直接点名的结果不自动交回主控)
            "origin_trigger": int(getattr(config, "trigger_message_id", 0) or 0),
        }
        self._background_tasks[task_id] = meta
        emit_json(self._emit(), "backend_agent", {
            "runtime": "claude", "status": "running",
            "task_id": task_id, "task_type": meta["task_type"],
            "description": f"后台命令：{meta['description']}",
        })

    def _finish_background_task(self, message: dict, status: str) -> None:
        task_id = str(message.get("task_id") or "")
        meta = self._background_tasks.pop(task_id, None)
        if meta is None:
            return
        summary = str(message.get("summary") or "")
        emit_json(self._emit(), "backend_agent", {
            "runtime": "claude", "status": status,
            "task_id": task_id, "task_type": meta["task_type"],
            "description": f"后台命令：{meta['description']}",
            "summary": summary,
        })
        # 先按"会单独唤醒"入队并记下到达时所在的 turn;若该 turn 随后
        # 还有模型调用(收到 user 消息),CLI 已把通知就地喂给模型,届时
        # 从队列剔除。空闲时到达的通知 turn 记 0,只能由下一个唤醒消费。
        with self._turn_condition:
            in_turn = (self._active_config is not None
                       or self._wake_sink is not None)
            self._pending_wakes.append({
                **meta, "status": status, "summary": summary,
                "output_file": str(message.get("output_file") or ""),
                "turn": self._turn_serial if in_turn else 0,
            })
            del self._pending_wakes[:-10]

    def _consume_in_turn_wakes(self) -> None:
        """当前 turn 又发起了模型调用:本 turn 内到达的通知已随该调用
        喂给模型,不会再单独唤醒,从待唤醒队列剔除。"""
        with self._turn_condition:
            if self._active_config is None and self._wake_sink is None:
                return
            self._pending_wakes = [
                task for task in self._pending_wakes
                if task.get("turn") != self._turn_serial]

    def _finish_wake_turn(self, result: dict) -> None:
        payload = None
        with self._turn_condition:
            sink = self._wake_sink
            self._wake_sink = None
            reasons = list(sink.tasks) if sink is not None else []
            if sink is not None:
                if result.get("session_id"):
                    self.session_id = str(result["session_id"])
                self.last_activity = time.time()
                output = (str(result.get("result") or "").strip()
                          or sink.assembler.text.strip())
                success = (not bool(result.get("is_error"))
                           and str(result.get("subtype") or "success") == "success")
                if _WAKE_HANDLER is not None and output and self.persistent:
                    payload = {
                        "runtime": "claude",
                        "backend_id": self.backend_id,
                        "session_key": self.session_key,
                        "workdir": self.workdir,
                        "project_id": self.last_project_id,
                        "role_id": self.last_role_id,
                        "success": success,
                        "output": output,
                        "events": list(sink.events),
                        "tasks": reasons,
                    }
            self._turn_condition.notify_all()
        if sink is None:
            return
        handler = _WAKE_HANDLER
        if handler is None or payload is None:
            return
        # 平台回调可能发消息/落库,不能阻塞 stdout 读取线程
        threading.Thread(target=handler, args=(payload,),
                         daemon=True, name="claude-wake").start()

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
            usage=_agent_usage(message.get("usage")),
        )

    def _note_turn_activity(self) -> None:
        """模型活动(正文、思考、工具调用/结果)登记到当前运行 turn。"""
        if self._active_config is not None:
            self._turn_activity = True

    def _note_stale_replay(self, message: dict, status: str) -> None:
        """首个 init 之前到达的任务通知是 --resume 启动时的回放:上个进程
        遗留的后台任务被 CLI 标记为 stopped。只记数并提示,随后那对空
        init/result 由 _is_replayed_result 跳过。"""
        with self._turn_condition:
            self._stale_replays += 1
        task_id = str(message.get("task_id") or "")
        safe_emit(self._emit(), "status",
                  f"上个 Claude 进程遗留的后台任务 {task_id} 已由 CLI 标记为 "
                  f"{status},本轮不受影响\n")

    def _is_replayed_result(self, message: dict) -> bool:
        """成功且 num_turns 为 0 的 result 没有调过模型,不可能是本轮发出的
        消息的结果:刚记录过回放通知,或本轮尚无任何模型活动时跳过。
        中断后的 result 一律认,否则中断会拖到超时。"""
        success = (not bool(message.get("is_error"))
                   and str(message.get("subtype") or "success") == "success")
        if (not success or message.get("num_turns") != 0
                or self._interrupt_requested):
            return False
        with self._turn_condition:
            if self._stale_replays > 0:
                self._stale_replays -= 1
            elif self._turn_activity:
                return False
        safe_emit(self._emit(), "status",
                  "跳过 Claude 启动回放的空 result(未调模型),继续等待本轮结果\n")
        return True

    def _finish_output_line(self, sink: _TurnSink) -> None:
        """一条完整输出结束:空白消息丢弃,有内容才在下一条前插横线分隔。"""
        sink.assembler.finish_message()

    def _append_output_text(self, sink: _TurnSink, text: str) -> None:
        """输出正文统一入口:消息之间补 Markdown 横线,过程与结论可区分。"""
        safe_emit(sink.emit, "text", sink.assembler.append(text))

    def _current_sink(self, begin_wake: bool = False) -> Optional[_TurnSink]:
        """事件归属:自唤醒 turn 进行中时优先归它——即使新运行已把用户消息
        排入队列,CLI 也会先送完自唤醒 turn 的事件与 result 再处理排队消息。"""
        with self._turn_condition:
            if self._wake_sink is not None:
                return self._wake_sink
            if self._active_config is not None:
                return self._run_sink
            if begin_wake and self.persistent:
                sink = _TurnSink()
                sink.emit = lambda kind, text: sink.events.append((kind, text))
                # 一个唤醒 turn 只对应队首一条通知;同批的下一条由 CLI
                # 紧接着再起一个 turn 来汇报
                if self._pending_wakes:
                    sink.tasks = [self._pending_wakes.pop(0)]
                self._turn_serial += 1
                self._wake_sink = sink
                return sink
            return None

    def _handle_message(self, message: dict) -> None:
        message_type = message.get("type")
        # 空闲时收到模型活动 => CLI 的自唤醒 turn(后台任务结束后自动汇报)。
        # 裸 result 不开启唤醒:真正的自唤醒 turn 一定先有 init/assistant
        # 事件;超时后迟到的运行 result 不能被误包装成唤醒汇报。
        begin_wake = (message_type in ("stream_event", "assistant", "user")
                      or (message_type == "system"
                          and str(message.get("subtype") or "") == "init"))
        sink = self._current_sink(begin_wake=begin_wake)
        emit = sink.emit if sink else None
        if message_type == "stream_event":
            if sink is None:
                return
            event = message.get("event") or {}
            if event.get("type") != "content_block_delta":
                return
            delta = event.get("delta") or {}
            self._note_turn_activity()
            if delta.get("type") == "text_delta":
                sink.saw_partial_text = True
                self._append_output_text(sink, str(delta.get("text") or ""))
            elif delta.get("type") == "thinking_delta":
                sink.saw_partial_thinking = True
                safe_emit(emit, "thinking", str(delta.get("thinking") or ""))
            return
        if message_type == "assistant":
            if sink is None:
                return
            self._note_turn_activity()
            for block in (message.get("message") or {}).get("content") or []:
                block_type = block.get("type")
                if block_type == "text" and not sink.saw_partial_text:
                    self._append_output_text(sink, str(block.get("text") or ""))
                elif block_type == "thinking" and not sink.saw_partial_thinking:
                    safe_emit(emit, "thinking", str(block.get("thinking") or ""))
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
                    safe_emit(emit, "tool",
                              f"{block.get('name', '?')} {detail[:800]}\n")
            # 一条 assistant 消息结束(流式与整块两条路径都会收到该事件):
            # 补换行,后续消息不与它拼在同一行
            self._finish_output_line(sink)
            return
        if message_type == "user":
            self._note_turn_activity()
            self._consume_in_turn_wakes()
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
                    safe_emit(emit, "tool_result",
                              mark + summary + "\n")
            return
        if message_type == "system":
            subtype = str(message.get("subtype") or "")
            if subtype == "init":
                self._process_inits += 1
                native_id = str(message.get("session_id") or "")
                if native_id:
                    self.session_id = native_id
                    self._resume_attempt = ""
                    config = self._active_config
                    if config:
                        adapters._save_session(config, native_id)
                safe_emit(emit, "status",
                          f"Claude 会话已连接 session={native_id} model={message.get('model', '')}\n")
                return
            if subtype in ("compact_boundary", "microcompact_boundary"):
                config = self._active_config
                if config is not None:
                    adapters._mark_compact(config)
                safe_emit(emit, "status",
                          "检测到上下文压缩;下一轮将重新注入完整公共上下文\n")
                return
            if subtype == "task_started":
                task_type = str(message.get("task_type") or "")
                if task_type.endswith("_agent"):
                    self._register_native_agent(message)
                else:
                    self._register_background_task(message)
                return
            if subtype == "background_tasks_changed":
                # 权威后台任务列表。只做补登(错过 task_started 的任务);
                # 移除统一走 task_notification,它在本事件之后到达。
                for task in message.get("tasks") or []:
                    if not str(task.get("task_type") or "").endswith("_agent"):
                        self._register_background_task(task)
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
                        usage=_agent_usage(message.get("usage")),
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
                    meta = (self._native_agents.get(task_id)
                            or self._background_tasks.get(task_id))
                    if meta:
                        meta["updated_status"] = status
                return
            if subtype == "task_notification":
                status = str(message.get("status") or "completed")
                if self._process_inits == 0:
                    self._note_stale_replay(message, status)
                if str(message.get("task_id") or "") in self._background_tasks:
                    self._finish_background_task(message, status)
                else:
                    self._finish_native_agent(message, status)
                return
        if message_type == "result":
            if self._wake_sink is not None:
                # 自唤醒 turn 的收尾:不触碰运行 turn 的 result 状态机
                self._finish_wake_turn(message)
                return
            if self._is_replayed_result(message):
                return
            self._result = message
            usage = _result_usage(message)
            if usage:
                emit_json(self._emit(), "usage", usage)
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
        # 自唤醒 turn(后台命令结束后的自动汇报)没有对应运行:沿用该会话
        # 最近一轮的权限策略,让 Agent 能读到后台输出;但没有可交互的
        # 运行,需要人工确认的工具与提问只能拒绝。
        wake_turn = config is None
        if wake_turn:
            config = self._last_config
        if not config:
            return {"behavior": "deny", "message": "MissionCrew turn 已结束"}
        interact = None if wake_turn else config.interact
        emit = self._emit()
        if tool_name in _QUESTION_TOOLS:
            questions = _normalized_questions(tool_input)
            if not interact:
                return {"behavior": "deny", "message": (
                    "后台命令汇报回合无法接收用户回答" if wake_turn
                    else "当前执行入口无法接收用户回答")}
            response = interact("user_input_request", {
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
            emit_json(emit, "permission_request",
                      {**payload, "status": "denied", "reason": denial})
            return {"behavior": "deny", "message": denial}
        if policy == "auto":
            emit_json(emit, "permission_request",
                      {**payload, "status": "auto_approved"})
            return {"behavior": "allow", "updatedInput": tool_input}
        if policy == "deny" or not interact:
            reason = ("后台命令汇报回合无法交互审批,请在下一轮频道回合中重试"
                      if wake_turn and policy != "deny"
                      else "MissionCrew 权限策略拒绝了工具调用")
            emit_json(emit, "permission_request",
                      {**payload, "status": "denied", "reason": reason})
            return {"behavior": "deny", "message": reason}
        response = interact("permission_request", payload)
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
        # 中断后 CLI 可能回一个零轮次 result,必须照常收口,不能被回放守卫跳过
        self._interrupt_requested = True
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
        with self._turn_condition:
            return self._active_config is not None

    def reclaimable(self, cutoff: float) -> bool:
        """空闲回收判定:显式 turn、wake turn、后台命令或未完成的后台
        Agent 存活时都不回收——关进程会连带杀掉它们。"""
        with self._turn_condition:
            busy = (self._active_config is not None
                    or self._wake_sink is not None
                    or bool(self._background_tasks)
                    or bool(self._pending_native_agents))
        return not busy and self.last_activity < cutoff

    def close(self) -> None:
        process = self.process
        self.process = None
        # 进程终止会连带杀掉其后台命令;未派发的自唤醒缓冲一并作废
        with self._turn_condition:
            self._background_tasks.clear()
            self._wake_sink = None
            self._pending_wakes = []
            if self._active_config and not self._result_ready.is_set():
                self._result = {
                    "subtype": "error_during_execution", "is_error": True,
                    "error": "Claude session 已停止",
                }
                self._result_ready.set()
            self._turn_condition.notify_all()
        if process and process.poll() is None:
            adapters._kill_process_group(process)


class ClaudeRuntimeProvider(RuntimeProvider):
    """把 Claude Code SDK-compatible stream-json 封装为统一 Runtime。"""

    def effort_catalog(self) -> dict[str, list[str]]:
        # 原生 `--effort` 标志,档位来自 `claude --help`
        return {"claude_code": ["low", "medium", "high", "xhigh", "max"]}

    def __init__(self, fallback: RuntimeProvider,
                 command: Optional[list[str]] = None):
        self.fallback = fallback
        self.command = command
        self._sessions: dict[str, _ClaudeSession] = {}
        self._guard = threading.Lock()

    def _command(self, backend: Backend) -> list[str]:
        return list(self.command or [backend.binary_path or "claude"])

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
        return RuntimeCapabilities(
            session_reuse=True, structured_events=True,
            user_interaction=True, permission_control=True, interrupt=True,
            account_usage=True)

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
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

    def account_usage(self, backend: Backend,
                      timeout: int = 15) -> RuntimeUsageSnapshot:
        return probe_claude_usage(backend, self._command(backend), timeout)

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
