"""pi 编码代理的 RPC 模式原生 Runtime provider。

pi 以 ``--mode rpc`` 作为长驻子进程挂在 stdin/stdout 上,协议为行式 JSON:
客户端发 ``{"type": <command>, "id": ...}``,进程回
``{"type": "response", "id": ...}``,回合过程以 ``{"type": <event>}`` 事件流出。
与 JSON-RPC 形的 codex app-server 不同,这里的请求/事件都按 ``type`` 区分,
因此使用专用客户端而不是 native.JsonLineProcess。

pi 的配置目录经 ``PI_CODING_AGENT_DIR`` 重定向到 ``MC_HOME/pi/agent``
(models.json 在此声明裸 OpenAI/Anthropic 兼容 API),会话文件落在
``MC_HOME/pi/sessions``;二进制使用 vendored 安装,不依赖 ~/.pi 与系统级 pi。
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core import config as core_config
from ..core.models import Backend, ExecutionConfig, RunResult
from . import adapters
from .base import (RuntimeCapabilities, RuntimeExecutionInfo, RuntimeInstance,
                   RuntimeProvider)
from .native import MESSAGE_DIVIDER, RuntimeProtocolError, emit_json, safe_emit

# models.json 支持的 API 协议(与 pi 的 KnownApi 对齐,只放平台明确要
# 支持的裸 API 形态;其余协议按需再放开)。
SUPPORTED_PROVIDER_APIS = {
    "openai-completions", "openai-responses", "anthropic-messages",
    "google-generative-ai",
}

_PROVIDER_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

# agent_end 之后可能立刻跟 auto_retry_start 重开一轮(pi 的瞬态错误自动
# 重试),因此 agent_end 不能直接判终;静默该时长且无重试事件才算回合结束。
_RETRY_GRACE_SECONDS = 1.0

# 平台守卫扩展:随包分发,要求模型为每次 bash 调用显式声明 timeout。
_GUARD_EXTENSION = Path(__file__).with_name("pi_guard.ts")


def read_pi_models() -> list[str]:
    """从平台自有 models.json 枚举 ``provider/model`` 执行单元。"""
    path = core_config.pi_models_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models: list[str] = []
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return []
    for provider, spec in providers.items():
        if not isinstance(spec, dict):
            continue
        for model in spec.get("models") or []:
            model_id = model.get("id") if isinstance(model, dict) else None
            if model_id:
                entry = f"{provider}/{model_id}"
                if entry not in models:
                    models.append(entry)
    return models


def validate_pi_providers(data: dict) -> str:
    """校验 providers 配置的最小结构;返回错误描述,空串表示通过。"""
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return "缺少 providers 对象"
    for name, spec in providers.items():
        # provider 名会拼进执行单元 "provider/model",带斜杠会让模型解析歧义;
        # 同时收敛为可安全出现在页面与 URL 中的字符集。
        if not _PROVIDER_NAME_RE.fullmatch(str(name)):
            return f"provider 名 {name} 只能使用字母、数字、. _ -"
        if not isinstance(spec, dict):
            return f"provider {name} 必须是对象"
        if not str(spec.get("baseUrl") or ""):
            return f"provider {name} 缺少 baseUrl"
        api = str(spec.get("api") or "")
        if api not in SUPPORTED_PROVIDER_APIS:
            return (f"provider {name} 的 api 必须是 "
                    f"{sorted(SUPPORTED_PROVIDER_APIS)} 之一")
        models = spec.get("models")
        if not isinstance(models, list) or not models:
            return f"provider {name} 至少要声明一个模型"
        for model in models:
            if not isinstance(model, dict) or not str(model.get("id") or ""):
                return f"provider {name} 的模型必须带 id"
    return ""


class _PiRpcClient:
    """pi RPC 行式 JSON 客户端:命令按 id 配对响应,其余行都是事件。"""

    def __init__(self, command: list[str], *, cwd: str, env: dict,
                 event_handler: Callable[[dict], None],
                 stderr_handler: Optional[Callable[[str], None]] = None,
                 exit_handler: Optional[Callable[[], None]] = None):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.event_handler = event_handler
        self.stderr_handler = stderr_handler
        self.exit_handler = exit_handler
        self.process: Optional[subprocess.Popen] = None
        self._pending: dict[str, "_Pending"] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()

    def connect(self) -> None:
        if self.process and self.process.poll() is None:
            return
        try:
            self.process = subprocess.Popen(
                self.command, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                bufsize=1, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeProtocolError(f"命令不存在: {self.command[0]}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    @property
    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None
                    and not self._closed.is_set())

    def send(self, payload: dict) -> None:
        """发送无需等待响应的命令(steer/abort 等)。"""
        process = self.process
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeProtocolError("pi RPC 进程未运行")
        encoded = json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeProtocolError("pi RPC stdin 已关闭") from exc

    def request(self, command: str, payload: Optional[dict] = None,
                timeout: float = 30) -> dict:
        request_id = uuid.uuid4().hex
        pending = _Pending()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self.send({"type": command, "id": request_id, **(payload or {})})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not pending.ready.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"pi RPC 请求超时: {command}")
        response = pending.response or {}
        if not response.get("success", False):
            raise RuntimeProtocolError(
                str(response.get("error") or f"pi 命令失败: {command}"))
        data = response.get("data")
        return data if isinstance(data, dict) else {}

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
                if self.stderr_handler:
                    self.stderr_handler(line + "\n")
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "response":
                with self._pending_lock:
                    pending = self._pending.pop(str(message.get("id")), None)
                if pending:
                    pending.response = message
                    pending.ready.set()
                continue
            try:
                self.event_handler(message)
            except Exception:
                pass
        self._on_exit()

    def _read_stderr(self) -> None:
        process = self.process
        assert process and process.stderr
        for line in process.stderr:
            if self.stderr_handler and line:
                try:
                    self.stderr_handler(line)
                except Exception:
                    pass

    def _on_exit(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.response = {"success": False, "error": "pi RPC 进程已退出"}
            item.ready.set()
        if self.exit_handler:
            try:
                self.exit_handler()
            except Exception:
                pass

    def close(self) -> None:
        process = self.process
        self._on_exit()
        if process and process.poll() is None:
            adapters._kill_process_group(process)


@dataclass
class _Pending:
    ready: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None


class _PiSession:
    def __init__(self, prefix: list[str], backend_id: str, session_key: str,
                 workdir: str, persistent: bool):
        self.prefix = prefix
        self.backend_id = backend_id
        self.session_key = session_key
        self.workdir = str(Path(workdir).expanduser().resolve())
        self.persistent = persistent
        self.client: Optional[_PiRpcClient] = None
        self.session_file = ""
        self._signature = ""
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._turn_done = threading.Event()
        self._active_config: Optional[ExecutionConfig] = None
        self._output: list[str] = []
        self._saw_text_delta = False
        self._pending_divider = False
        self._turn_error = ""
        self._stop_reason = ""
        self._last_usage: dict = {}
        self._finish_timer: Optional[threading.Timer] = None
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

    # ---- 进程与会话 ----

    def _runtime_env(self, config: ExecutionConfig) -> dict:
        env = adapters._runtime_env(config, "pi")
        env["PI_CODING_AGENT_DIR"] = str(core_config.pi_agent_dir())
        return env

    def _command(self, config: ExecutionConfig, resume_file: str) -> list[str]:
        command = [*self.prefix, "--mode", "rpc", "--no-extensions",
                   "--session-dir", str(core_config.pi_sessions_dir())]
        # --no-extensions 只禁自动发现的用户扩展;平台守卫扩展显式加载,
        # 强制模型为每次 bash 调用声明 timeout(pi 的 bash 默认无超时)。
        if _GUARD_EXTENSION.is_file():
            command += ["--extension", str(_GUARD_EXTENSION)]
        if not self.persistent:
            command.append("--no-session")
        elif resume_file:
            command += ["--session", resume_file]
        if config.backend.model:
            command += ["--model", config.backend.model]
        if config.effort:
            command += ["--thinking", config.effort]
        return command

    def _ensure_client(self, config: ExecutionConfig) -> None:
        resume_file = self.session_file or config.session_id
        if resume_file and not Path(resume_file).is_file():
            # 会话文件被清理(或记录来自其他机器):明确降级为新会话。
            adapters._clear_session(config)
            resume_file = ""
            self.session_file = ""
        command = self._command(config, resume_file)
        # 签名只含静态启动形态:--session/--model/--thinking 是动态参数,
        # 存活进程内经 resume 记录与 set_model/set_thinking_level 对齐,
        # 不因它们变化而重启进程(重启会丢掉进程内会话连续性)。
        signature = json.dumps(
            {"prefix": self.prefix, "persistent": self.persistent,
             "env": sorted(self._runtime_env(config).items())},
            ensure_ascii=False, separators=(",", ":"))
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
        self.client = _PiRpcClient(
            command, cwd=self.workdir, env=self._runtime_env(config),
            event_handler=self._handle_event,
            stderr_handler=lambda text: safe_emit(
                self._active_config.emit if self._active_config else None,
                "stderr", text),
            exit_handler=self._process_exited,
        )
        self.client.connect()
        state = self.client.request("get_state", timeout=30)
        self._remember_session_file(state)
        safe_emit(config.emit, "status",
                  f"pi 会话已{'恢复' if resume_file else '启动'}"
                  f" model={self._state_model(state)}\n")

    @staticmethod
    def _state_model(state: dict) -> str:
        model = state.get("model")
        if isinstance(model, dict):
            provider = str(model.get("provider") or "")
            model_id = str(model.get("id") or "")
            return f"{provider}/{model_id}" if provider else model_id
        return str(model or "")

    def _remember_session_file(self, state: dict) -> None:
        raw = str(state.get("sessionFile") or "")
        if not raw:
            return
        path = Path(raw)
        if not path.is_absolute():
            path = Path(self.workdir) / path
        self.session_file = str(path)

    def _sync_turn_settings(self, config: ExecutionConfig) -> None:
        """会话进程存活期间模型/思考档位可能被上一轮改过,按本轮配置对齐。"""
        assert self.client
        model = config.backend.model
        if model and "/" in model:
            state = self.client.request("get_state", timeout=15)
            if self._state_model(state) != model:
                provider, _, model_id = model.partition("/")
                self.client.request("set_model", {
                    "provider": provider, "modelId": model_id}, timeout=15)
        if config.effort:
            try:
                self.client.request("set_thinking_level",
                                    {"level": config.effort}, timeout=15)
            except (RuntimeProtocolError, TimeoutError):
                # 模型不支持 thinking 时 pi 会拒绝;不阻塞回合。
                pass

    # ---- 回合执行 ----

    def run(self, config: ExecutionConfig) -> RunResult:
        with self._run_lock:
            if config.cancellation_requested():
                return RunResult(False, "执行已停止")
            adapters._refresh_session(config)
            if self.session_file and not config.session_id:
                # 持久记录被显式清理:不再续用仅存于内存的旧会话。
                self.close()
                self.session_file = ""
            self._active_config = config
            self.last_activity = time.time()
            self.last_task_id = config.task_id
            self.last_stage_name = config.stage_name
            self.last_project_id = config.project_id
            self.last_role_id = config.role_id
            self.last_model = config.backend.model
            prompt, injection_mode = adapters._session_input(
                config, recovery=not bool(config.session_id))
            with self._state_lock:
                self._turn_done.clear()
                self._output = []
                self._saw_text_delta = False
                self._pending_divider = False
                self._turn_error = ""
                self._stop_reason = ""
                self._last_usage = {}
                self._cancel_finish_timer()
            try:
                self._ensure_client(config)
                assert self.client
                adapters._emit_execution_start(
                    config.emit, list(self.client.command), prompt)
                if config.cancellation_requested():
                    self.close()
                    return RunResult(False, "执行已停止")
                self._sync_turn_settings(config)
                self.client.send({"type": "prompt", "message": prompt})
                if not self._turn_done.wait(config.timeout):
                    try:
                        self.client.send({"type": "abort"})
                        self._turn_done.wait(10)
                    except Exception:
                        pass
                    return RunResult(False, f"执行超时({config.timeout}s)")
                output = "".join(self._output).strip()
                success = self._stop_reason not in {"error", "aborted"} and \
                    not self._turn_error
                if success and self.persistent and self.session_file:
                    adapters._save_session(
                        config, self.session_file, injection_mode,
                        adapters._turn_bytes(prompt, output))
                if self._last_usage:
                    emit_json(config.emit, "usage", self._last_usage)
                # 成功但零输出时 summary 保持为空:交给聊天层的"无输出"
                # 守卫处理,固定文案不能被当成 Agent 回复发布。
                if self._turn_error:
                    summary = self._turn_error
                elif not success:
                    summary = f"pi 回合结束于 {self._stop_reason or 'failed'}"
                else:
                    summary = ""
                return RunResult(success, summary[-300:], output=output)
            except Exception as exc:
                return RunResult(False, str(exc)[-300:])
            finally:
                self.last_activity = time.time()
                self._active_config = None

    # ---- 事件处理 ----

    def _handle_event(self, event: dict) -> None:
        kind = str(event.get("type") or "")
        config = self._active_config
        emit = config.emit if config else None
        if kind == "message_update":
            self._handle_message_update(emit, event)
        elif kind == "message_end":
            self._handle_message_end(emit, event.get("message") or {})
        elif kind == "tool_execution_start":
            name = str(event.get("toolName") or "tool")
            args = json.dumps(event.get("args"), ensure_ascii=False,
                              default=str)[:800]
            safe_emit(emit, "tool", f"{name} {args}\n")
        elif kind == "tool_execution_end":
            detail = event.get("result")
            text = _tool_result_text(detail)
            if text:
                safe_emit(emit, "tool_result", text[:2000] + "\n")
        elif kind == "auto_retry_start":
            self._cancel_finish_timer()
            safe_emit(emit, "status",
                      f"pi 瞬态错误自动重试 {event.get('attempt')}/"
                      f"{event.get('maxAttempts')}\n")
        elif kind in ("compaction_start", "compaction_end"):
            if kind == "compaction_end" and config:
                adapters._mark_compact(config)
            safe_emit(emit, "status",
                      "上下文压缩" + ("完成" if kind == "compaction_end"
                                      else "开始") + "\n")
        elif kind == "agent_end":
            self._schedule_finish()

    def _handle_message_update(self, emit, event: dict) -> None:
        sub = event.get("assistantMessageEvent")
        sub = sub if isinstance(sub, dict) else {}
        sub_type = str(sub.get("type") or "")
        delta = str(sub.get("delta") or "")
        if sub_type == "text_delta" and delta:
            with self._state_lock:
                self._saw_text_delta = True
            self._append_output_text(emit, delta)
        elif sub_type == "thinking_delta" and delta:
            safe_emit(emit, "thinking", delta)

    def _handle_message_end(self, emit, message: dict) -> None:
        if message.get("role") != "assistant":
            return
        stop_reason = str(message.get("stopReason") or "")
        error_message = str(message.get("errorMessage") or "")
        fallback_text = ""
        with self._state_lock:
            if stop_reason:
                self._stop_reason = stop_reason
            self._turn_error = error_message
            usage = message.get("usage")
            if isinstance(usage, dict):
                self._last_usage = usage
            if not self._saw_text_delta:
                # 无流式 delta(部分 API 一次性返回):从消息内容兜底取正文。
                text = "\n".join(
                    str(item.get("text") or "")
                    for item in message.get("content") or []
                    if isinstance(item, dict) and item.get("type") == "text")
                if text.strip():
                    fallback_text = text
        if fallback_text:
            self._append_output_text(emit, fallback_text)
        # 一条 assistant 消息结束:下一条输出到来时先插横线分隔
        with self._state_lock:
            if self._output:
                self._pending_divider = True

    def _append_output_text(self, emit, text: str) -> None:
        """输出正文统一入口:消息之间补 Markdown 横线,过程与结论可区分。"""
        if not text:
            return
        with self._state_lock:
            divider = self._pending_divider
            self._pending_divider = False
            if divider:
                self._output.append(MESSAGE_DIVIDER)
            self._output.append(text)
        if divider:
            safe_emit(emit, "text", MESSAGE_DIVIDER)
        safe_emit(emit, "text", text)

    def _schedule_finish(self) -> None:
        """agent_end 后延迟判终:静默期内出现 auto_retry_start 则继续等待。"""
        with self._state_lock:
            self._cancel_finish_timer()
            timer = threading.Timer(_RETRY_GRACE_SECONDS, self._finish_turn)
            timer.daemon = True
            self._finish_timer = timer
            timer.start()

    def _cancel_finish_timer(self) -> None:
        timer = self._finish_timer
        if timer:
            timer.cancel()
            self._finish_timer = None

    def _finish_turn(self) -> None:
        client = self.client
        if client and client.alive:
            try:
                self._remember_session_file(
                    client.request("get_state", timeout=10))
            except Exception:
                pass
        self._turn_done.set()

    def _process_exited(self) -> None:
        if self._active_config and not self._restarting_client:
            with self._state_lock:
                self._turn_error = self._turn_error or "pi RPC 进程已退出"
                self._stop_reason = "error"
            self._turn_done.set()

    # ---- 生命周期 ----

    def reclaimable(self, cutoff: float) -> bool:
        """空闲回收判定:回合进行中不回收;会话文件已持久化可恢复。"""
        return self._active_config is None and self.last_activity < cutoff

    def snapshot(self) -> RuntimeInstance:
        client = self.client
        alive = bool(client and client.alive)
        state = ("running" if alive and self._active_config else
                 "idle" if alive else
                 "starting" if self._active_config else "disconnected")
        process = client.process if client else None
        return RuntimeInstance(
            instance_id=f"pi:{self.backend_id}:{self.session_key}",
            backend_id=self.backend_id, adapter="pi",
            mode="persistent" if self.persistent else "one_shot",
            transport="pi-rpc", state=state,
            pid=process.pid if process and process.poll() is None else None,
            session_key=self.session_key if self.persistent else "",
            native_session_id=self.session_file, workdir=self.workdir,
            task_id=self.last_task_id, stage_name=self.last_stage_name,
            project_id=self.last_project_id, role_id=self.last_role_id,
            model=self.last_model, executable=Path(self.prefix[-1]).name,
            started_at=self.created_at, last_activity=self.last_activity,
        )

    def interrupt(self) -> bool:
        client = self.client
        if not client or not client.alive or not self._active_config:
            return False
        client.send({"type": "abort"})
        return True

    def close(self) -> None:
        self._cancel_finish_timer()
        if self.client:
            self.client.close()
            self.client = None


def _tool_result_text(detail) -> str:
    """把工具结果压成可读文本;pi 的 result 可能是字符串、内容块或对象。"""
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        content = detail.get("content")
        if isinstance(content, list):
            parts = [str(item.get("text") or "") for item in content
                     if isinstance(item, dict) and item.get("type") == "text"]
            if any(parts):
                return "\n".join(part for part in parts if part)
        return json.dumps(detail, ensure_ascii=False, default=str)
    return json.dumps(detail, ensure_ascii=False, default=str)


class PiRuntimeProvider(RuntimeProvider):
    """把 pi RPC 的会话/回合能力封装为统一 Runtime。"""

    def effort_catalog(self) -> dict[str, list[str]]:
        # 映射为 pi 的 thinking level(`--thinking`/set_thinking_level)
        return {"pi": ["off", "minimal", "low", "medium", "high", "xhigh"]}

    def __init__(self, fallback: RuntimeProvider,
                 command: Optional[list[str]] = None):
        self.fallback = fallback
        self.command = command
        self._sessions: dict[str, _PiSession] = {}
        self._guard = threading.Lock()

    def _command_prefix(self, backend: Backend) -> list[str]:
        return list(self.command or [backend.binary_path
                                     or str(core_config.pi_vendor_bin())])

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
                session = _PiSession(
                    self._command_prefix(config.backend), config.backend.id,
                    key, config.workdir, persistent=not ephemeral)
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

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            session_reuse=True, structured_events=True, interrupt=True)

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        return RuntimeExecutionInfo(
            mode="persistent" if config.session_key else "one_shot",
            transport="pi-rpc",
        )

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        # 模型目录与 models.json 同源,直接读文件,不为枚举拉起子进程。
        return read_pi_models()

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
