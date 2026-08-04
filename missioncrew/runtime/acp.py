"""ACP(Agent Client Protocol)stdio 客户端。

Grok / kimi / kiro / qoder / trae 等 CLI 不走"命令行传 prompt"的打印模式,而是作为
JSON-RPC 服务挂在 stdio 上(换行分隔的 JSON-RPC 2.0)。流程:

    initialize -> session/new|session/load -> [session/set_model] -> session/prompt
    期间收集 session/update 通知里的 agent_message_chunk 作为回复文本;
    agent 反向发来的 session/request_permission 必须应答(选它提供的安全
    选项),否则 agent 会阻塞到内部超时,任务假死。
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .base import RuntimeInstance


class AcpError(Exception):
    pass


_DETACHED_TERMINAL_STATUSES = {
    "cancelled", "completed", "failed", "killed", "lost", "stopped", "timed_out",
}
_DETACHED_FOLLOWUP_LIMIT = 3
_TERMINAL_OUTPUT_LIMIT = 128 * 1024
# turn 之外的自发 session/update(如 Grok 后台任务结束后的自动汇报)按
# 静默间隔去抖:这么久没有新事件就认为该自发 turn 已结束,整体交付
_WAKE_DEBOUNCE_SECONDS = 3.0

# 平台注册的自唤醒回调:客户端终端里的后台任务结束后,支持自发汇报的
# Runtime(Grok 的 task_completed 扩展等)会在 turn 之外产出输出,经此
# 回调交给 ChatEngine 落成频道里的新运行。
_WAKE_HANDLER: Optional[Callable[[dict], None]] = None


def set_wake_handler(handler: Optional[Callable[[dict], None]]) -> None:
    global _WAKE_HANDLER
    _WAKE_HANDLER = handler


class _ClientTerminal:
    """ACP 客户端持有的终端(terminal/*):agent 把命令交给客户端执行。

    进程归客户端所有,退出时间因此对平台可见——这是 ACP 后台任务能被
    触发式跟踪的关键。输出按 ACP 约定超限时从头部截断,保留末尾。"""

    def __init__(self, command: str, args: list[str], cwd: str,
                 env: Optional[dict], output_byte_limit: int):
        self.command = command if not args else " ".join([command, *args])
        self.output_byte_limit = max(
            4096, int(output_byte_limit or _TERMINAL_OUTPUT_LIMIT))
        self.created_at = time.time()
        self.origin_trigger = 0   # 发起 turn 的触发消息 id,创建后由客户端补记
        self.truncated = False
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self.exited = threading.Event()
        self.proc = subprocess.Popen(
            [command, *args] if args else command, shell=not args,
            cwd=cwd or None, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        for chunk in iter(lambda: stream.read(4096), b""):
            with self._buffer_lock:
                self._buffer.extend(chunk)
                if len(self._buffer) > self.output_byte_limit:
                    del self._buffer[:len(self._buffer) - self.output_byte_limit]
                    self.truncated = True
        self.proc.wait()
        self.exited.set()

    def output(self) -> tuple[str, bool]:
        with self._buffer_lock:
            return self._buffer.decode("utf-8", "replace"), self.truncated

    def exit_status(self) -> Optional[dict]:
        code = self.proc.poll()
        if code is None:
            return None
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = str(-code)
            return {"exitCode": None, "signal": name}
        return {"exitCode": code, "signal": None}

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                self.proc.kill()
            except OSError:
                pass


def _task_fields(value: Any) -> tuple[str, str]:
    """从 Runtime 开放的 rawOutput 中提取后台任务 id 和状态。

    ACP v1 只规定 rawOutput 是开放对象，并没有 detached task 类型。Kimi
    当前会返回换行分隔的 ``task_id/status`` 文本；同时兼容其他 Runtime
    直接返回对象以及后台 subagent 使用 ``agent_id`` 的形态。
    """
    if isinstance(value, dict):
        task_id = next(
            (str(value[key]) for key in ("task_id", "taskId", "agent_id", "agentId")
             if value.get(key)),
            "",
        )
        status = str(value.get("status") or "")
        return task_id, status.lower()
    if not isinstance(value, str):
        return "", ""

    fields: dict[str, str] = {}
    for line in value.splitlines():
        key, separator, raw_value = line.partition(":")
        if separator:
            fields[key.strip()] = raw_value.strip()
    task_id = next(
        (fields[key] for key in ("task_id", "taskId", "agent_id", "agentId")
         if fields.get(key)),
        "",
    )
    return task_id, fields.get("status", "").lower()


class _AcpClient:
    def __init__(self, cmd: list[str], cwd: str, env: dict,
                 timeout: Optional[float],
                 emit: Optional[Callable[[str, str], None]] = None):
        env = {**env, "PWD": cwd}
        self.command = list(cmd)
        self.cwd = cwd
        self.env = dict(env)   # 客户端终端以此为基础环境
        self.started_at = time.time()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env=env,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        self.deadline = time.time() + timeout if timeout is not None else None
        self.chunks: list[str] = []          # agent_message_chunk 文本
        self._raw_emit = emit or (lambda kind, text: None)
        try:
            permissions = json.loads(env.get("MISSIONCREW_RUNTIME_PERMISSIONS", "{}"))
        except json.JSONDecodeError:
            permissions = {}
        self.approval_mode = str(permissions.get("approval", "auto"))

        def _safe_emit(kind: str, text: str) -> None:
            # 上报失败只丢事件:读循环死亡会让所有 pending 请求挂到超时
            try:
                self._raw_emit(kind, text)
            except Exception:
                pass
        self.emit = _safe_emit                # 运行过程实时上报
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._write_lock = threading.Lock()
        self._detached_lock = threading.Lock()
        self._tool_inputs: dict[str, dict] = {}
        self._detached_launchers: set[str] = set()
        self._detached_tasks: dict[str, str] = {}
        # 客户端终端与自唤醒缓冲。_turn_active 默认 True:一次性调用与
        # list_models 不走 begin_turn/end_turn,事件应按常规 turn 归属。
        self._turn_active = True
        self._terminals: dict[str, _ClientTerminal] = {}
        self._terminals_lock = threading.Lock()
        self._terminal_seq = 0
        self._wake_lock = threading.Lock()
        self._wake_chunks: list[str] = []
        self._wake_events: list[tuple[str, str]] = []
        self._wake_tasks: list[dict] = []
        self._wake_last_at = 0.0
        self._wake_timer_running = False
        self.wake_meta: dict = {}
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def begin_turn(self, timeout: Optional[float],
                   emit: Optional[Callable[[str, str], None]]) -> None:
        """为长驻客户端开启新一轮，重置超时、输出和事件接收器。"""
        self._dispatch_wake()   # 尚未交付的自发汇报先于新 turn 交付,避免混流
        self._turn_active = True
        self.deadline = time.time() + timeout if timeout is not None else None
        self.chunks = []
        self._raw_emit = emit or (lambda kind, text: None)
        with self._detached_lock:
            self._tool_inputs = {}
            self._detached_launchers = set()
            self._detached_tasks = {}

    def end_turn(self) -> None:
        """turn 收尾:此后到达的 session/update 视为 Runtime 自发行为。"""
        self._turn_active = False

    # ---- 读循环:响应 / agent 请求 / 通知 ----
    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle(msg)
        for q in list(self._pending.values()):   # EOF:让等待方立刻失败
            q.put({"error": {"message": "ACP 进程退出"}})

    def _stderr_loop(self) -> None:
        """持续排空 stderr；长驻进程若写满该管道会阻塞整个协议。"""
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            if line:
                self.emit("stderr", line)

    def _handle(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            q = self._pending.pop(msg["id"], None)
            if q:
                q.put(msg)
        elif "id" in msg and "method" in msg:
            self._handle_agent_request(msg)
        elif msg.get("method") in ("session/update", "session/notification"):
            params = msg.get("params") or {}
            meta = params.get("_meta") or {}
            # Grok 在恢复时会标记历史通知。即使 provider 忽略了 noReplay，
            # 历史事件也不能进入当前 Run 的输出和工具生命周期。
            if isinstance(meta, dict) and meta.get("isReplay") is True:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            content = update.get("content") or {}
            if not self._turn_active:
                # turn 之外的自发更新:Runtime 的后台任务自动汇报。缓冲后
                # 经唤醒管线交付,不能混入上一轮已结束的运行。
                self._buffer_wake_update(str(kind or ""), content, update)
                return
            if kind == "agent_message_chunk":
                if content.get("type") == "text":
                    self.chunks.append(content.get("text", ""))
                    self.emit("text", content.get("text", ""))
            elif kind == "agent_thought_chunk":
                if content.get("type") == "text":
                    self.emit("thinking", content.get("text", ""))
            elif kind in ("tool_call", "tool_call_update"):
                self._record_tool_update(update)
                label = update.get("title") or update.get("toolCallId") or ""
                status = update.get("status") or ""
                if label or status:
                    self.emit("tool", f"{label} {status}".strip() + "\n")

    def _buffer_wake_update(self, kind: str, content: dict, update: dict) -> None:
        text = str((content or {}).get("text") or "")
        with self._wake_lock:
            if kind == "agent_message_chunk" and text:
                self._wake_chunks.append(text)
                self._wake_events.append(("text", text))
            elif kind == "agent_thought_chunk" and text:
                self._wake_events.append(("thinking", text))
            elif kind in ("tool_call", "tool_call_update"):
                label = update.get("title") or update.get("toolCallId") or ""
                status = update.get("status") or ""
                if not label and not status:
                    return
                self._wake_events.append(
                    ("tool", f"{label} {status}".strip() + "\n"))
            else:
                return
            self._wake_last_at = time.time()
            if not self._wake_timer_running:
                self._wake_timer_running = True
                threading.Thread(target=self._wake_watchdog,
                                 daemon=True).start()

    def _wake_watchdog(self) -> None:
        """自发 turn 没有终止标记,按静默间隔判定结束后整体交付。"""
        while True:
            time.sleep(min(_WAKE_DEBOUNCE_SECONDS, 0.2))
            with self._wake_lock:
                if self._turn_active:
                    # 新运行 turn 已开始,begin_turn 负责交付缓冲
                    self._wake_timer_running = False
                    return
                if time.time() - self._wake_last_at >= _WAKE_DEBOUNCE_SECONDS:
                    self._wake_timer_running = False
                    break
        self._dispatch_wake()

    def _dispatch_wake(self) -> None:
        with self._wake_lock:
            output = "".join(self._wake_chunks).strip()
            events = list(self._wake_events)
            tasks = list(self._wake_tasks)
            if not output:
                # 没有可交付正文(纯思考/工具残留):丢弃事件,保留任务
                # 记录等待真正的汇报 turn
                self._wake_chunks.clear()
                self._wake_events.clear()
                return
            self._wake_chunks.clear()
            self._wake_events.clear()
            self._wake_tasks.clear()
        handler = _WAKE_HANDLER
        meta = dict(self.wake_meta)
        if handler is None or not meta.get("session_key"):
            return
        payload = {
            "runtime": "acp",
            "backend_id": str(meta.get("backend_id") or ""),
            "session_key": str(meta["session_key"]),
            "workdir": self.cwd,
            "project_id": str(meta.get("project_id") or ""),
            "role_id": str(meta.get("role_id") or ""),
            "success": True,
            "output": output,
            "events": events,
            "tasks": tasks,
        }
        # 平台回调可能发消息/落库,不能阻塞协议读取线程
        threading.Thread(target=handler, args=(payload,), daemon=True,
                         name="acp-wake").start()

    def _watch_terminal(self, terminal_id: str,
                        terminal: _ClientTerminal) -> None:
        terminal.exited.wait()
        status = terminal.exit_status() or {}
        state = "completed" if status.get("exitCode") == 0 else "failed"
        if self._turn_active:
            self.emit("status",
                      f"客户端终端已退出: {terminal.command[:160]} ({state})\n")
            return
        with self._wake_lock:
            self._wake_tasks.append({
                "task_id": terminal_id,
                "description": terminal.command[:200],
                "status": state,
                "origin_trigger": getattr(terminal, "origin_trigger", 0),
            })
            del self._wake_tasks[:-10]

    def live_terminal_count(self) -> int:
        with self._terminals_lock:
            return sum(1 for t in self._terminals.values() if t.running)

    def _record_tool_update(self, update: dict) -> None:
        """跟踪 Runtime 通过 ACP 开放字段暴露的 detached task 生命周期。"""
        tool_call_id = str(update.get("toolCallId") or "")
        if not tool_call_id:
            return

        raw_input = update.get("rawInput")
        raw_output = update.get("rawOutput")
        output_task_id, output_status = _task_fields(raw_output)
        new_task = ""
        terminal_task = ""
        with self._detached_lock:
            if isinstance(raw_input, dict):
                self._tool_inputs[tool_call_id] = raw_input
            tool_input = self._tool_inputs.get(tool_call_id) or {}
            if tool_input.get("run_in_background") is True:
                self._detached_launchers.add(tool_call_id)

            referenced_task_id = next(
                (str(tool_input[key])
                 for key in ("task_id", "taskId", "agent_id", "agentId")
                 if tool_input.get(key)),
                "",
            )
            if tool_call_id in self._detached_launchers:
                task_id = output_task_id or referenced_task_id
                if task_id:
                    status = output_status or "running"
                    if task_id not in self._detached_tasks:
                        new_task = task_id
                    self._detached_tasks[task_id] = status
            else:
                task_id = output_task_id or referenced_task_id
                if task_id in self._detached_tasks and output_status:
                    previous = self._detached_tasks[task_id]
                    self._detached_tasks[task_id] = output_status
                    if (previous not in _DETACHED_TERMINAL_STATUSES
                            and output_status in _DETACHED_TERMINAL_STATUSES):
                        terminal_task = task_id

        if new_task:
            self.emit("status", f"ACP 后台任务已纳入跟踪: {new_task}\n")
        if terminal_task:
            self.emit(
                "status",
                f"ACP 后台任务已结束: {terminal_task} ({output_status})\n",
            )

    def pending_detached_tasks(self) -> dict[str, str]:
        with self._detached_lock:
            return {
                task_id: status
                for task_id, status in self._detached_tasks.items()
                if status not in _DETACHED_TERMINAL_STATUSES
            }

    def _handle_agent_request(self, msg: dict) -> None:
        """应答 agent -> client 方向的请求,平台是无头的,权限自动决策。"""
        method = msg["method"]
        params = msg.get("params") or {}
        if method == "session/request_permission":
            option = _pick_permission_option(
                params.get("options") or [], self.approval_mode)
            if option is not None:
                self.emit("status", f"权限请求:自动选择 {option}\n")
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "result": {"outcome": {"outcome": "selected",
                                                    "optionId": option}}})
            else:  # 没有可安全选择的项:返回协议错误而不是 cancelled(那会取消整轮)
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32000,
                                       "message": "no safely selectable option"}})
        elif method.startswith("terminal/"):
            self._handle_terminal_request(msg, method, params)
        else:  # 其他未知请求返回空结果,避免 agent 阻塞
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    def _handle_terminal_request(self, msg: dict, method: str,
                                 params: dict) -> None:
        """客户端终端(clientCapabilities.terminal):进程由客户端持有。"""
        def reply(result=None, error=None):
            body = ({"error": error} if error is not None
                    else {"result": result if result is not None else {}})
            self._write({"jsonrpc": "2.0", "id": msg["id"], **body})

        if method == "terminal/create":
            command = str(params.get("command") or "")
            if not command:
                reply(error={"code": -32602, "message": "missing command"})
                return
            args = [str(item) for item in params.get("args") or []]
            env = dict(self.env)
            for item in params.get("env") or []:
                if isinstance(item, dict) and item.get("name"):
                    env[str(item["name"])] = str(item.get("value") or "")
            try:
                terminal = _ClientTerminal(
                    command, args, str(params.get("cwd") or "") or self.cwd,
                    env, int(params.get("outputByteLimit") or 0))
            except OSError as exc:
                reply(error={"code": -32603, "message": f"spawn failed: {exc}"})
                return
            # 记录发起 turn 的触发消息:唤醒汇报按它继承派发语义
            terminal.origin_trigger = int(
                self.wake_meta.get("trigger_message_id") or 0)
            with self._terminals_lock:
                self._terminal_seq += 1
                terminal_id = f"mc-term-{self._terminal_seq}"
                self._terminals[terminal_id] = terminal
            threading.Thread(target=self._watch_terminal,
                             args=(terminal_id, terminal), daemon=True).start()
            self.emit("status",
                      f"客户端终端已创建 {terminal_id}: {terminal.command[:160]}\n")
            reply({"terminalId": terminal_id})
            return

        terminal_id = str(params.get("terminalId") or "")
        with self._terminals_lock:
            terminal = self._terminals.get(terminal_id)
        if terminal is None:
            reply(error={"code": -32602,
                         "message": f"unknown terminal: {terminal_id}"})
            return
        if method == "terminal/output":
            output, truncated = terminal.output()
            result = {"output": output, "truncated": truncated}
            status = terminal.exit_status()
            if status is not None:
                result["exitStatus"] = status
            reply(result)
        elif method == "terminal/wait_for_exit":
            def _waiter(mid=msg["id"], target=terminal):
                target.exited.wait()
                try:
                    self._write({"jsonrpc": "2.0", "id": mid,
                                 "result": target.exit_status() or {}})
                except Exception:
                    pass   # 客户端已关闭:agent 进程也将随之退出
            # 阻塞等待不能占用协议读取线程,进程退出后再写回响应
            threading.Thread(target=_waiter, daemon=True).start()
        elif method == "terminal/kill":
            terminal.kill()
            reply({})
        elif method == "terminal/release":
            with self._terminals_lock:
                self._terminals.pop(terminal_id, None)
            terminal.kill()
            reply({})
        else:
            reply(error={"code": -32601, "message": f"unsupported: {method}"})

    # ---- 写与请求 ----
    def _write(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        data = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            self.proc.stdin.write(data + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        try:
            self._write({"jsonrpc": "2.0", "id": rid,
                         "method": method, "params": params})
        except (OSError, ValueError) as exc:
            self._pending.pop(rid, None)
            raise AcpError(f"{method} 写入失败: {exc}") from exc
        remaining = (self.deadline - time.time()
                     if self.deadline is not None else None)
        if remaining is not None and remaining <= 0:
            self._pending.pop(rid, None)
            raise AcpError(f"{method} 超时")
        try:
            msg = q.get(timeout=remaining)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise AcpError(f"{method} 超时")
        if "error" in msg:
            detail = msg["error"].get("message", msg["error"]) if isinstance(msg["error"], dict) else msg["error"]
            raise AcpError(f"{method} 失败: {detail}")
        return msg.get("result") or {}

    def close(self) -> None:
        # 客户端终端随客户端关闭而终止(与服务重启杀后台命令的口径一致)
        with self._terminals_lock:
            terminals = list(self._terminals.values())
            self._terminals.clear()
        for terminal in terminals:
            terminal.kill()
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


@dataclass
class _LiveSession:
    client: _AcpClient
    runtime_id: str
    session_id: str
    signature: tuple
    last_used: float
    busy: bool = False
    task_id: str = ""
    stage_name: str = ""
    project_id: str = ""
    role_id: str = ""
    model: str = ""


@dataclass
class _OneShotClient:
    runtime_id: str
    client: _AcpClient
    task_id: str = ""
    stage_name: str = ""
    project_id: str = ""
    role_id: str = ""
    model: str = ""


_LIVE_SESSIONS: dict[str, _LiveSession] = {}
_LIVE_SESSIONS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()
_ONE_SHOT_CLIENTS: dict[int, _OneShotClient] = {}
_ONE_SHOT_CLIENTS_GUARD = threading.Lock()
_SESSION_IDLE_SECONDS = 1800


def _session_lock(session_key: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_key, threading.Lock())


def _client_signature(cmd: list[str], workdir: str, env: dict) -> tuple:
    # ACP serve 进程启动后不能更新环境；项目目录权限已渲染进 cmd，其余平台
    # 环境只比较稳定的 MISSIONCREW_*。旧调用方可能仍传逐轮 run id；它由
    # Agent Tool 的 run-scoped token 取代，不能因此打断长驻会话。
    platform_env = tuple(sorted(
        (key, str(value)) for key, value in env.items()
        if key.startswith("MISSIONCREW_")
        and key != "MISSIONCREW_AGENT_RUN_ID"
    ))
    return tuple(cmd), workdir, platform_env


def _drop_live_session(session_key: str, expected: Optional[_LiveSession] = None) -> None:
    live = None
    with _LIVE_SESSIONS_GUARD:
        current = _LIVE_SESSIONS.get(session_key)
        if current is not None and (expected is None or current is expected):
            live = _LIVE_SESSIONS.pop(session_key)
    if live is not None:
        live.client.close()


def _cleanup_idle_sessions() -> None:
    cutoff = time.time() - _SESSION_IDLE_SECONDS
    stale: list[_LiveSession] = []
    with _LIVE_SESSIONS_GUARD:
        for key, live in list(_LIVE_SESSIONS.items()):
            # 客户端终端里仍有存活的后台命令时不回收:关闭客户端会连带
            # 杀掉这些命令,长任务必须等它们退出并完成自发汇报
            if (not live.busy and live.last_used < cutoff
                    and live.client.live_terminal_count() == 0):
                stale.append(_LIVE_SESSIONS.pop(key))
    for live in stale:
        live.client.close()


def close_sessions() -> None:
    """关闭全部长驻 ACP 会话，供服务退出和测试清理。"""
    with _LIVE_SESSIONS_GUARD:
        sessions = list(_LIVE_SESSIONS.values())
        _LIVE_SESSIONS.clear()
    with _ONE_SHOT_CLIENTS_GUARD:
        one_shots = [item.client for item in _ONE_SHOT_CLIENTS.values()]
        _ONE_SHOT_CLIENTS.clear()
    for client in [*(live.client for live in sessions), *one_shots]:
        client.close()


def stop_runtime_sessions(runtime_id: str, session_key: str = "") -> int:
    """停止指定 Runtime 的 ACP 长驻会话和一次性执行。"""
    live_clients: list[_AcpClient] = []
    with _LIVE_SESSIONS_GUARD:
        for key, live in list(_LIVE_SESSIONS.items()):
            if live.runtime_id == runtime_id and (not session_key or key == session_key):
                live_clients.append(_LIVE_SESSIONS.pop(key).client)
    one_shots: list[_AcpClient] = []
    if not session_key:
        with _ONE_SHOT_CLIENTS_GUARD:
            for key, active in list(_ONE_SHOT_CLIENTS.items()):
                if active.runtime_id == runtime_id:
                    one_shots.append(active.client)
                    _ONE_SHOT_CLIENTS.pop(key, None)
    for client in [*live_clients, *one_shots]:
        client.close()
    return len(live_clients) + len(one_shots)


def active_instances(runtime_id: str = "") -> list[RuntimeInstance]:
    """返回 ACP 长驻 session 与一次性 stdio client 的实时快照。"""
    with _LIVE_SESSIONS_GUARD:
        live_items = list(_LIVE_SESSIONS.items())
    with _ONE_SHOT_CLIENTS_GUARD:
        one_shots = list(_ONE_SHOT_CLIENTS.values())
    instances = []
    for session_key, live in live_items:
        if runtime_id and live.runtime_id != runtime_id:
            continue
        client = live.client
        alive = client.proc.poll() is None
        instances.append(RuntimeInstance(
            instance_id=f"acp:{live.runtime_id}:{session_key}",
            backend_id=live.runtime_id, adapter="acp",
            mode="persistent", transport="acp-stdio",
            state=("running" if alive and live.busy else
                   "idle" if alive else "disconnected"),
            pid=client.proc.pid if alive else None,
            session_key=session_key, native_session_id=live.session_id,
            workdir=client.cwd, task_id=live.task_id,
            stage_name=live.stage_name, model=live.model,
            project_id=live.project_id, role_id=live.role_id,
            executable=Path(client.command[0]).name,
            started_at=client.started_at, last_activity=live.last_used,
            background_tasks=client.live_terminal_count(),
        ))
    for active in one_shots:
        if runtime_id and active.runtime_id != runtime_id:
            continue
        client = active.client
        if client.proc.poll() is not None:
            continue
        instances.append(RuntimeInstance(
            instance_id=f"acp-one-shot:{client.proc.pid}",
            backend_id=active.runtime_id, adapter="acp",
            mode="one_shot", transport="acp-stdio", state="running",
            pid=client.proc.pid, workdir=client.cwd,
            task_id=active.task_id, stage_name=active.stage_name,
            project_id=active.project_id, role_id=active.role_id,
            model=active.model, executable=Path(client.command[0]).name,
            started_at=client.started_at, last_activity=client.started_at,
        ))
    return instances


atexit.register(close_sessions)


def _pick_permission_option(options: list[dict], approval: str = "auto") -> Optional[str]:
    """从 agent 提供的选项里挑安全项:单次允许 > 会话允许 > 单次拒绝。

    必须选 agent 实际提供的 optionId(ACP 契约是"从这些选项里挑"),
    编造的 id 会被当作拒绝。
    """
    order = (("allow_once", "allow_always", "reject_once")
             if approval == "auto" else ("reject_once", "reject_always"))
    for kind in order:
        for o in options:
            if o.get("kind") == kind and o.get("optionId"):
                return o["optionId"]
    return None


def _initialize(client: _AcpClient) -> dict:
    return client.request("initialize", {
        "protocolVersion": 1,
        "clientInfo": {"name": "missioncrew", "version": "0.4.0"},
        # terminal:命令交给客户端终端执行,进程归客户端持有,后台任务的
        # 退出对平台可见;Grok 等 Runtime 会据此在任务结束后自发汇报。
        "clientCapabilities": {
            "terminal": True,
            "fs": {"readTextFile": False, "writeTextFile": False},
        },
    })


def _new_or_load_session(client: _AcpClient, workdir: str,
                         requested_session_id: str,
                         initialize_result: dict,
                         load_meta: Optional[dict] = None) -> tuple[str, bool]:
    """返回 (会话 id, 是否成功恢复)。不支持/无法 load 时创建新会话。"""
    capabilities = initialize_result.get("agentCapabilities") or {}
    can_load = bool(isinstance(capabilities, dict)
                    and capabilities.get("loadSession") is True)
    if requested_session_id and can_load:
        try:
            params = {
                "sessionId": requested_session_id,
                "cwd": workdir,
                "mcpServers": [],
            }
            if load_meta:
                params["_meta"] = dict(load_meta)
            result = client.request("session/load", params)
            return (result.get("sessionId") or result.get("session_id")
                    or requested_session_id), True
        except AcpError as exc:
            client.emit("status", f"ACP 原会话恢复失败，将用最近对话新建会话: {exc}\n")
    elif requested_session_id:
        client.emit("status", "ACP Runtime 未声明 session/load，将用最近对话新建会话。\n")

    result = client.request("session/new", {"cwd": workdir, "mcpServers": []})
    session_id = result.get("sessionId") or result.get("session_id") or ""
    if not session_id:
        raise AcpError("session/new 未返回 sessionId")
    return session_id, False


def _prompt_turn(client: _AcpClient, session_id: str, prompt: str,
                 model: str) -> str:
    if model:
        client.request("session/set_model", {"sessionId": session_id,
                                              "modelId": model})
    client.emit("input", prompt)
    client.request("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}],
    })
    for attempt in range(1, _DETACHED_FOLLOWUP_LIMIT + 1):
        pending = client.pending_detached_tasks()
        if not pending:
            break
        task_ids = ", ".join(sorted(pending))
        client.emit(
            "status",
            f"ACP 返回了阶段性回复，继续等待后台任务: {task_ids}"
            f" ({attempt}/{_DETACHED_FOLLOWUP_LIMIT})\n",
        )
        if client.chunks and not client.chunks[-1].endswith(("\n", " ")):
            client.chunks.append("\n\n")
        continuation = (
            "MissionCrew ACP detached-work continuation. The previous ACP turn "
            f"reported these background task IDs as still active: {task_ids}. "
            "Keep this response active until every listed task reaches a terminal "
            "state. Use this Runtime's supported task-output or task-wait tool with "
            "blocking enabled and a sufficiently long timeout; if a wait times out "
            "while a task is still running, wait again. Then inspect the completed "
            "results, finish the original user request, and provide the final reply. "
            "Do not launch replacement background tasks."
        )
        client.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": continuation}],
        })
    pending = client.pending_detached_tasks()
    if pending:
        task_ids = ", ".join(sorted(pending))
        raise AcpError(
            "ACP Runtime 返回后后台任务仍未结束，无法安全地把本轮标记为完成: "
            f"{task_ids}"
        )
    return "".join(client.chunks).strip() or "(无输出)"


def _run_one_shot(cmd: list[str], prompt: str, workdir: str, env: dict,
                  model: str, timeout: Optional[float], runtime_id: str,
                  emit: Optional[Callable[[str, str], None]],
                  task_id: str = "", stage_name: str = "",
                  project_id: str = "", role_id: str = "") -> tuple[bool, str]:
    try:
        client = _AcpClient(cmd, workdir, env, timeout, emit=emit)
    except OSError as e:
        return False, f"ACP 进程启动失败: {e}"
    with _ONE_SHOT_CLIENTS_GUARD:
        _ONE_SHOT_CLIENTS[id(client)] = _OneShotClient(
            runtime_id, client, task_id, stage_name, project_id, role_id, model)
    try:
        initialize_result = _initialize(client)
        session_id, _ = _new_or_load_session(client, workdir, "", initialize_result)
        return True, _prompt_turn(client, session_id, prompt, model)
    except AcpError as e:
        return False, str(e)
    finally:
        with _ONE_SHOT_CLIENTS_GUARD:
            _ONE_SHOT_CLIENTS.pop(id(client), None)
        client.close()


def run_prompt(cmd: list[str], prompt: str, workdir: str, env: dict,
               model: str = "", timeout: Optional[float] = None,
               emit: Optional[Callable[[str, str], None]] = None,
               session_key: str = "", session_id: str = "",
               recovery_prompt: str = "",
               save_session: Optional[Callable[..., None]] = None,
               prompt_mode: str = "", recovery_mode: str = "recovery",
               mode_labels: Optional[dict] = None,
               context_version: str = "", runtime_id: str = "",
               task_id: str = "", stage_name: str = "",
               project_id: str = "", role_id: str = "",
               cancelled: Optional[Callable[[], bool]] = None,
               load_session_meta: Optional[dict] = None,
               trigger_message_id: int = 0) -> tuple[bool, str]:
    """完成一轮 ACP prompt，并按 channel×role 复用长驻原生会话。

    无 ``session_key`` 时保持一次性调用。长驻进程不存在（包括服务重启）时，
    仅在 Runtime 声明 loadSession 能力后恢复持久化 id；否则创建新会话并使用
    ``recovery_prompt``，避免把缺失的历史当成已恢复。
    """
    if cancelled and cancelled():
        return False, "执行已停止"
    if not session_key:
        return _run_one_shot(
            cmd, prompt, workdir, env, model, timeout, runtime_id, emit,
            task_id, stage_name, project_id, role_id)

    _cleanup_idle_sessions()
    signature = _client_signature(cmd, workdir, env)
    with _session_lock(session_key):
        if cancelled and cancelled():
            return False, "执行已停止"
        with _LIVE_SESSIONS_GUARD:
            live = _LIVE_SESSIONS.get(session_key)
        if live is not None and (
                live.signature != signature or live.client.proc.poll() is not None):
            _drop_live_session(session_key, live)
            live = None

        try:
            recovered = live is not None
            if live is None:
                try:
                    client = _AcpClient(cmd, workdir, env, timeout, emit=emit)
                except OSError as exc:
                    return False, f"ACP 进程启动失败: {exc}"
                try:
                    initialize_result = _initialize(client)
                    runtime_session_id, recovered = _new_or_load_session(
                        client, workdir, session_id, initialize_result,
                        load_session_meta)
                except Exception:
                    client.close()
                    raise
                live = _LiveSession(
                    client, runtime_id, runtime_session_id, signature, time.time(),
                    task_id=task_id, stage_name=stage_name,
                    project_id=project_id, role_id=role_id, model=model)
                with _LIVE_SESSIONS_GUARD:
                    _LIVE_SESSIONS[session_key] = live

            if cancelled and cancelled():
                _drop_live_session(session_key, live)
                live = None
                return False, "执行已停止"

            live.busy = True
            live.task_id = task_id
            live.stage_name = stage_name
            live.project_id = project_id
            live.role_id = role_id
            live.model = model
            live.last_used = time.time()
            # 自唤醒汇报按会话定位频道×角色;每轮刷新,保持与最近执行一致
            live.client.wake_meta = {
                "session_key": session_key, "backend_id": runtime_id,
                "project_id": project_id, "role_id": role_id,
                "trigger_message_id": int(trigger_message_id or 0),
            }
            if cancelled and cancelled():
                return False, "执行已停止"
            live.client.begin_turn(timeout, emit)
            if recovered:
                actual_prompt, actual_mode = prompt, prompt_mode
            else:
                actual_prompt = recovery_prompt or prompt
                actual_mode = recovery_mode if recovery_prompt else prompt_mode
            label = (mode_labels or {}).get(actual_mode)
            if label:
                live.client.emit("status", f"公共上下文:{label}\n")
            reply = _prompt_turn(live.client, live.session_id, actual_prompt, model)
            live.last_used = time.time()
            if save_session:
                try:
                    save_session(live.session_id, context_version,
                                 turn_mode=actual_mode,
                                 turn_bytes=sum(
                                     len(t.encode("utf-8", "ignore"))
                                     for t in (actual_prompt, reply)))
                except Exception:
                    live.client.emit("status", "Runtime 会话已继续，但持久化会话 id 失败。\n")
            return True, reply
        except AcpError as exc:
            missing = bool(re.search(
                r"(?:session|conversation|thread).{0,40}"
                r"(?:not found|不存在|invalid|unknown)", str(exc), re.I | re.S))
            if live is not None and (live.client.proc.poll() is not None or missing):
                _drop_live_session(session_key, live)
            if missing and save_session:
                try:
                    save_session("", "")
                except Exception:
                    pass
            return False, str(exc)
        finally:
            if live is not None:
                live.busy = False
                live.client.end_turn()


def list_models(cmd: list[str], env: Optional[dict] = None,
                timeout: int = 30, runtime_id: str = "") -> list[str]:
    """向 ACP 工具查询可用模型:一次性会话,从 session/new 响应解析模型目录。

    兼容两种形态:kimi 等返回 configOptions(category=model 的 select 选项);
    部分实现返回 models 块——trae 用 {availableModels:[{modelId,...}]}
    (对齐 Multica parseACPSessionNewModels,兼容 snake_case 与旧的
    {available:[...]} 及裸数组)。查不到返回空列表。
    """
    import os
    import tempfile
    workdir = tempfile.mkdtemp(prefix="mc-acp-models-")
    try:
        client = _AcpClient(cmd, workdir, env or dict(os.environ), timeout)
    except OSError:
        return []
    if runtime_id:
        with _ONE_SHOT_CLIENTS_GUARD:
            _ONE_SHOT_CLIENTS[id(client)] = _OneShotClient(
                runtime_id, client, "model-catalog", "discovery")
    try:
        client.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "missioncrew-model-discovery", "version": "0.4.0"},
            "clientCapabilities": {},
        })
        sess = client.request("session/new", {"cwd": workdir, "mcpServers": []})
        models: list[str] = []
        for opt in sess.get("configOptions") or []:
            if opt.get("category") == "model" or opt.get("id") == "model":
                models = [str(o.get("value", "")) for o in opt.get("options", [])]
                break
        if not models:
            block = sess.get("models")
            if isinstance(block, dict):
                block = (block.get("availableModels") or block.get("available_models")
                         or block.get("available") or [])
            if isinstance(block, list):
                models = [str(m.get("modelId") or m.get("model_id") or m.get("id")
                              or m.get("value") or "")
                          if isinstance(m, dict) else str(m) for m in block]
        return [m for m in models if m]
    except AcpError:
        return []
    finally:
        with _ONE_SHOT_CLIENTS_GUARD:
            _ONE_SHOT_CLIENTS.pop(id(client), None)
        client.close()
