"""原生 Runtime 协议共享的 JSONL 进程与安全事件工具。"""
from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import adapters


class RuntimeProtocolError(RuntimeError):
    """原生 Runtime 返回协议错误或连接提前退出。"""


# 同一 turn 内多条完整输出之间的分隔。发布成 Agent 消息后按 Markdown
# 渲染为横线,读者可以分清工具调用间隙的过程输出与最后的结论。
MESSAGE_DIVIDER = "\n\n---\n\n"


def safe_emit(emit: Optional[Callable[[str, str], None]],
              kind: str, text: str) -> None:
    """过程事件失败只能丢弃，不能阻塞 Runtime 的 stdout 读取线程。"""
    if not emit or not text:
        return
    try:
        emit(kind, text)
    except Exception:
        pass


def emit_json(emit: Optional[Callable[[str, str], None]],
              kind: str, payload: dict) -> None:
    safe_emit(emit, kind, json.dumps(payload, ensure_ascii=False))


@dataclass
class _PendingResponse:
    ready: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: Optional[BaseException] = None


class JsonLineProcess:
    """省略 ``jsonrpc`` 字段的双向 JSON-RPC/JSONL 子进程客户端。"""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        notification_handler: Callable[[str, dict], None],
        request_handler: Callable[[str, dict], dict],
        stderr_handler: Optional[Callable[[str], None]] = None,
        exit_handler: Optional[Callable[[RuntimeProtocolError], None]] = None,
    ):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.notification_handler = notification_handler
        self.request_handler = request_handler
        self.stderr_handler = stderr_handler
        self.exit_handler = exit_handler
        self.process: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._pending: dict[int | str, _PendingResponse] = {}
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

    def _write(self, payload: dict) -> None:
        process = self.process
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeProtocolError("Runtime 协议进程未运行")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeProtocolError("Runtime 协议 stdin 已关闭") from exc

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._write({"method": method, "params": params or {}})

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = 30) -> object:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingResponse()
            self._pending[request_id] = pending
        try:
            self._write({"method": method, "id": request_id,
                         "params": params or {}})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not pending.ready.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Runtime 请求超时: {method}")
        if pending.error:
            raise pending.error
        return pending.result

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
            if "method" in message and "id" in message:
                # Agent→client 请求可以等待用户，不能卡住主 stdout 读取循环。
                threading.Thread(
                    target=self._handle_server_request,
                    args=(message,), daemon=True,
                ).start()
            elif "method" in message:
                try:
                    self.notification_handler(
                        str(message["method"]), message.get("params") or {})
                except Exception:
                    pass
            elif "id" in message:
                with self._pending_lock:
                    pending = self._pending.pop(message["id"], None)
                if not pending:
                    continue
                if message.get("error") is not None:
                    error = message.get("error") or {}
                    detail = error.get("message") if isinstance(error, dict) else error
                    pending.error = RuntimeProtocolError(str(detail or "Runtime 请求失败"))
                else:
                    pending.result = message.get("result")
                pending.ready.set()
        self._fail_all(RuntimeProtocolError("Runtime 协议进程已退出"))

    def _handle_server_request(self, message: dict) -> None:
        try:
            result = self.request_handler(
                str(message.get("method", "")), message.get("params") or {})
            self._write({"id": message["id"], "result": result})
        except Exception as exc:
            try:
                self._write({
                    "id": message["id"],
                    "error": {"code": -32000, "message": str(exc)},
                })
            except Exception:
                pass

    def _read_stderr(self) -> None:
        process = self.process
        assert process and process.stderr
        for line in process.stderr:
            if self.stderr_handler and line:
                try:
                    self.stderr_handler(line)
                except Exception:
                    pass

    def _fail_all(self, error: RuntimeProtocolError) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.ready.set()
        if self.exit_handler:
            try:
                self.exit_handler(error)
            except Exception:
                pass

    def close(self) -> None:
        process = self.process
        self._fail_all(RuntimeProtocolError("Runtime 协议进程已停止"))
        if process and process.poll() is None:
            adapters._kill_process_group(process)
