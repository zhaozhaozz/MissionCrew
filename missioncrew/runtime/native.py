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


class OutputAssembler:
    """聚合一个 turn 内的多条完整输出消息。

    消息之间补 MESSAGE_DIVIDER;整条只有空白的消息直接丢弃,消息开头的空白
    先扣住、等本条出现实际内容时再连同分隔横线一起写出。这样最终回复与实时
    text 事件都不会出现空横线段。append 返回本次真正新增的文本,调用方原样
    emit 即可保证事件流与最终聚合结果一致。
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._held = ""              # 当前消息出现实际内容前扣住的空白前缀
        self._message_open = False   # 当前消息已写出过非空白内容
        self._pending_divider = False

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def append(self, text: str) -> str:
        if not text:
            return ""
        if self._message_open:
            self._parts.append(text)
            return text
        self._held += text
        if not self._held.strip():
            return ""
        emitted = (MESSAGE_DIVIDER if self._pending_divider else "") + self._held
        self._pending_divider = False
        self._held = ""
        self._message_open = True
        self._parts.append(emitted)
        return emitted

    def finish_message(self) -> None:
        """一条完整输出结束:空白消息不留痕迹,有内容才预约下一条前的横线。"""
        self._held = ""
        if self._message_open:
            self._message_open = False
            self._pending_divider = True


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


# ---- 过程事件 ``usage`` 的统一结构 ----
# 各 Runtime 上报的 token 用量字段名并不一致(Codex 驼峰且按 total/last 分区段,
# pi 平铺 input/cacheRead,Claude 后台 Agent 是 total_tokens/tool_uses)。工具知识
# 留在各 provider:它们用自己的 {统一字段: 上报字段} 表调用 usage_section(),再经
# build_usage() 组装成下面这份结构 emit;前端只认这一种,raw 原样保留上报内容。
USAGE_SCHEMA = "usage/v1"
# 区段字段(turn=本轮/最近一次请求,total=会话累计),缺失的字段不出现
USAGE_SECTION_FIELDS = ("input", "cache_read", "cache_write", "output", "reasoning", "total")


def _usage_number(value: object) -> Optional[float]:
    """数值(含数字字符串)原样返回;bool、空值、其他类型视为缺失。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def usage_section(source: object, fields: dict[str, str]) -> dict:
    """按 ``{统一字段: 上报字段}`` 从 source 取数值,组成一个 turn/total 区段。"""
    if not isinstance(source, dict):
        return {}
    section = {}
    for name, key in fields.items():
        value = _usage_number(source.get(key))
        if value is not None and name in USAGE_SECTION_FIELDS:
            section[name] = value
    return section


def build_usage(raw: object, *, turn: Optional[dict] = None, total: Optional[dict] = None,
                context_window: object = None, cost_usd: object = None,
                tool_uses: object = None, duration_ms: object = None) -> dict:
    """组装 ``usage/v1`` payload;一个可识别的数值都没有时返回空 dict(调用方不 emit)。"""
    payload: dict = {"schema": USAGE_SCHEMA}
    if turn:
        payload["turn"] = turn
    if total:
        payload["total"] = total
    for key, value in (("context_window", context_window), ("cost_usd", cost_usd),
                       ("tool_uses", tool_uses), ("duration_ms", duration_ms)):
        number = _usage_number(value)
        if number is not None:
            payload[key] = number
    if len(payload) == 1:
        return {}
    payload["raw"] = raw
    return payload


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
