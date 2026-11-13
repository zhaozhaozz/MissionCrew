"""ACP(Agent Client Protocol)stdio 客户端。

Grok / kimi / kiro / qoder / trae 等 CLI 不走"命令行传 prompt"的打印模式,而是作为
JSON-RPC 服务挂在 stdio 上(换行分隔的 JSON-RPC 2.0)。流程:

    initialize -> session/new|session/load -> [session/set_model]
        -> [session/set_config_option(thought_level)] -> session/prompt
    期间收集 session/update 通知里的 agent_message_chunk 作为回复文本;
    推理力度(effort)若不在 serve 命令里,则按 ACP 标准的会话配置项在会话内
    切换:session/new|load 应答的 configOptions 中 category=thought_level 的
    选项(kimi 的 `thinking`)即档位,每轮 prompt 前 set_config_option 对齐;
    项目额外目录若不在 serve 命令里,则随 session/new|load 的
    additionalDirectories 传入(agent 声明了 sessionCapabilities.additionalDirectories);
    回合结束时把 session/prompt 响应的 usage 与最近一次 usage_update 归一成
    usage/v1 过程事件;
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
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .base import RuntimeInstance, host_isolated_environ


class AcpError(Exception):
    pass


_DETACHED_TERMINAL_STATUSES = {
    "cancelled", "completed", "failed", "killed", "lost", "stopped", "timed_out",
}
_DETACHED_FOLLOWUP_LIMIT = 3
_TERMINAL_OUTPUT_LIMIT = 128 * 1024
# turn 之外的自发 session/update(如 Grok 后台任务结束后的自动汇报、Runtime
# 自带定时器触发的一轮)按静默间隔去抖:这么久没有新事件就认为该自发 turn 已结束
_WAKE_DEBOUNCE_SECONDS = 3.0
# 已落成运行的自发 turn 只在"最后是正文且没有进行中的工具"时按上面的间隔收尾;
# 刚拿到工具结果、模型还在酝酿下一步时最多等 _WAKE_THINK_SECONDS,工具仍在
# 运行(长测试、构建)时最多等 _WAKE_TOOL_SECONDS。过早收尾会撤销令牌,
# 工具跑完后的平台写操作随之失败,还会多出一条"无输出"的失败消息
_WAKE_THINK_SECONDS = 60.0
_WAKE_TOOL_SECONDS = 1800.0
# 已落成运行的自发 turn 静默后一个字的正文都没有时,在同一会话补发一轮
# session/prompt 请 Runtime 把汇报说出来。实测 kimi 的自发 turn 只向客户端发
# 终端与审批请求,不推送任何 session/update,正文只留在它自己的会话记录里。
# Runtime 还在自发 turn 里时这条 prompt 拿不到正文(kimi 立刻应答空的
# end_turn,把它排到自发 turn 之后静默执行),所以空回复后隔更久再试:
# 第 n 次空回复后静默 n×_WAKE_REPORT_RETRY_SECONDS,最多 _WAKE_REPORT_LIMIT 次
_WAKE_REPORT_LIMIT = 3
_WAKE_REPORT_RETRY_SECONDS = 90.0
_WAKE_REPORT_TIMEOUT = 1800.0
_WAKE_REPORT_PROMPT = (
    "MissionCrew wake report. Your latest self-started turn (one this Runtime "
    "began on its own, for example after a background command finished) "
    "delivered no reply text to the platform, so nothing from it has reached "
    "the channel. Reply now with the report for that turn: what finished and "
    "its result, what you already did in response, and what remains. This "
    "reply is published to the channel. Do not repeat work or rerun commands "
    "that the self-started turn already completed."
)

# ACP 用量(规范中仍标 unstable):session/prompt 响应的 usage 是该次 prompt
# 的用量,inputTokens 含缓存读写;usage_update 通知给当前上下文占用 used、
# 窗口 size 与会话累计 cost{amount,currency}。厂商私有扩展不在这里解析。
ACP_USAGE_FIELDS = {"input": "inputTokens", "cache_read": "cachedReadTokens",
                    "cache_write": "cachedWriteTokens", "output": "outputTokens",
                    "reasoning": "thoughtTokens", "total": "totalTokens"}

# 平台注册的自唤醒回调:客户端终端里的后台任务结束、或 Runtime 自带的定时器
# 到点后,Runtime(Grok 的 task_completed 扩展、scheduler 等)会在 turn 之外
# 自发跑一轮。ACP 没有自发 turn 的起止标记:主代理的首个模型输出或客户端
# 终端请求即视为起点,同步调用 begin 让平台落成运行、签发绑定该运行的
# Agent Tool 令牌并返回实时事件接收器;按静默判定结束后把输出交给 handler
# 收尾。begin 缺席或返回 None 时退回旧路径:事件缓冲,结束后整体交付。
_WAKE_HANDLER: Optional[Callable[[dict], None]] = None
_WAKE_BEGIN_HANDLER: Optional[Callable[[dict], Optional[dict]]] = None


def set_wake_handler(handler: Optional[Callable[[dict], None]],
                     begin: Optional[Callable[[dict], Optional[dict]]] = None) -> None:
    global _WAKE_HANDLER, _WAKE_BEGIN_HANDLER
    _WAKE_HANDLER = handler
    _WAKE_BEGIN_HANDLER = begin


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
        self.wake_run_id = 0      # 由已落成运行的自发 turn 创建时记下该运行
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
        # session/new|load 自报的推理力度配置项 id(ACP category=thought_level,
        # kimi 为 "thinking");空 = 该 Runtime 不经会话配置项切换档位
        self.thought_level_option = ""
        # 工具声明的私有用量解析(session/prompt 响应 -> usage/v1 片段);None = 只认标准字段
        self.parse_usage: Optional[Callable[[dict], dict]] = None
        # 本轮最近一次 usage_update(上下文占用/窗口/会话累计费用)
        self.context_usage: dict = {}
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
        # ACP session/load 在应答前回放历史,这些通知不属于当前运行。
        self._load_request_id: Optional[int] = None
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
        # 当前自发 turn 落成的运行:run_id/实时接收器/触发消息,未落成时为 0/None
        self._wake_run_id = 0
        self._wake_emit: Optional[Callable[[str, str], Any]] = None
        self._wake_trigger_id = 0
        self._wake_begin_tried = False       # 每个自发 turn 只请求一次 begin
        self._wake_open_tools: set[str] = set()   # 进行中的工具调用,决定何时收尾
        self._wake_last_kind = ""
        # 无正文的自发 turn 补发汇报 prompt:已发次数,以及是否正在等它的应答
        # (等待期间由应答判定结束,不走静默看门狗)
        self._wake_report_tries = 0
        self._wake_reporting = False
        self.session_id = ""                 # 长驻会话的原生 id,补发汇报时用
        # turn 之外最近一次 Runtime 自发活动(自发更新或向客户端发起的请求):
        # 空闲回收据此判断会话是否真的闲置
        self._spontaneous_at = 0.0
        self.wake_meta: dict = {}
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def begin_turn(self, timeout: Optional[float],
                   emit: Optional[Callable[[str, str], None]]) -> None:
        """为长驻客户端开启新一轮，重置超时、输出和事件接收器。"""
        # 先在锁内置位再交付:begin 回调与本方法并发时,先落成的唤醒运行由
        # 这里收尾,后落成的看到 _turn_active 自行收尾,不会悬在 running
        with self._wake_lock:
            self._turn_active = True
        self._dispatch_wake()   # 尚未交付的自发汇报先于新 turn 交付,避免混流
        self.deadline = time.time() + timeout if timeout is not None else None
        self.chunks = []
        self.context_usage = {}
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
            try:
                self._handle(msg)
            except Exception:
                # 单条畸形消息不能拖死读循环:读循环一死,本会话此后的每个
                # session/prompt 响应都无人消费,运行会永远停在 running。
                traceback.print_exc()
        for q in list(self._pending.values()):   # EOF:让等待方立刻失败
            q.put({"error": {"message": "ACP 进程退出"}})
        self._abort_wake("Runtime 进程已退出,自发汇报未完成")

    def _stderr_loop(self) -> None:
        """持续排空 stderr；长驻进程若写满该管道会阻塞整个协议。"""
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            if line:
                self.emit("stderr", line)

    def _handle(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            if msg["id"] == self._load_request_id:
                # 在读线程按线上的消息顺序结束隔离,不能等请求线程被唤醒,
                # 否则紧随 load 应答的真实通知也可能被丢弃。
                self._load_request_id = None
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
            if self._load_request_id is not None:
                # Kimi 等工具的标准回放没有 isReplay 标记;在进入正文、
                # 工具生命周期或后台唤醒管线之前统一过滤。
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            from_subagent = isinstance(meta, dict) and bool(meta.get("parentToolCallId"))
            if kind == "agent_message_chunk" and from_subagent:
                # 子代理(task 工具)与主代理共用 sessionId,只靠 parentToolCallId
                # 区分(traecli 实测)。子代理正文已作为工具结果交回主代理;并入
                # 回复会与主代理及并行子代理的流式分片逐字交错,只当过程事件展示
                kind = "agent_thought_chunk"
            if kind == "usage_update":
                # 只记最新状态,回合结束时随本轮用量一起上报;不进唤醒缓冲
                self.context_usage = update
                return
            # 消息/思考块的 content 是 {type,text};tool_call 类更新的 content 按
            # ACP 约定是内容块列表,不能当 dict 用
            content = update.get("content")
            if not isinstance(content, dict):
                content = {}
            if not self._turn_active:
                # turn 之外的自发更新:Runtime 的后台任务自动汇报或定时器
                # 触发的一轮。经唤醒管线交付,不能混入上一轮已结束的运行。
                self._buffer_wake_update(str(kind or ""), content, update,
                                         from_subagent=from_subagent)
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

    def _buffer_wake_update(self, kind: str, content: dict, update: dict,
                            from_subagent: bool = False) -> None:
        text = (str(content.get("text") or "")
                if isinstance(content, dict) else "")
        if kind == "agent_message_chunk" and text:
            event = ("text", text)
        elif kind == "agent_thought_chunk" and text:
            event = ("thinking", text)
        elif kind in ("tool_call", "tool_call_update"):
            label = update.get("title") or update.get("toolCallId") or ""
            status = update.get("status") or ""
            if not label and not status:
                return
            event = ("tool", f"{label} {status}".strip() + "\n")
        else:
            return
        # 主代理的模型输出说明自发 turn 已经开始:先落成运行、签发令牌,
        # 这一轮随后的工具调用才拿得到令牌。子代理残留和旧工具调用的
        # 状态更新(tool_call_update)不算起点
        if not from_subagent and kind != "tool_call_update":
            self._begin_wake_run()
        with self._wake_lock:
            if kind == "agent_message_chunk":
                self._wake_chunks.append(text)
            call_id = str(update.get("toolCallId") or "")
            if call_id and kind in ("tool_call", "tool_call_update"):
                if update.get("status") in ("completed", "failed"):
                    self._wake_open_tools.discard(call_id)
                else:
                    self._wake_open_tools.add(call_id)
            self._wake_last_kind = event[0]
            emit = self._wake_emit
            if emit is None:
                self._wake_events.append(event)
            self._touch_wake_locked()
        if emit is not None:
            self._emit_wake(emit, *event)

    def _touch_wake_locked(self) -> None:
        """记一次自发 turn 活动并确保有静默看门狗;调用方持有 _wake_lock。"""
        self._wake_last_at = self._spontaneous_at = time.time()
        if not self._wake_timer_running and not self._wake_reporting:
            self._wake_timer_running = True
            threading.Thread(target=self._wake_watchdog, daemon=True).start()

    @staticmethod
    def _emit_wake(emit: Callable[[str, str], Any], kind: str, text: str) -> None:
        try:
            emit(kind, text)
        except Exception:
            pass   # 上报失败只丢事件,不能拖死读循环

    def _begin_wake_run(self) -> None:
        """自发 turn 起点:同步请平台落成运行并签发令牌。

        在协议读取线程里调用:之后的 terminal/create 等请求排在它后面处理,
        命令真正执行时令牌文件已经就位。"""
        begin, handler = _WAKE_BEGIN_HANDLER, _WAKE_HANDLER
        meta = dict(self.wake_meta)
        with self._wake_lock:
            if self._wake_run_id or self._wake_begin_tried or self._turn_active:
                return
            self._wake_begin_tried = True
            tasks = list(self._wake_tasks)
        if begin is None or handler is None or not meta.get("session_key"):
            return
        try:
            bound = begin({
                "runtime": "acp",
                "backend_id": str(meta.get("backend_id") or ""),
                "session_key": str(meta["session_key"]),
                "workdir": self.cwd,
                "project_id": str(meta.get("project_id") or ""),
                "role_id": str(meta.get("role_id") or ""),
                "tasks": tasks,
            })
        except Exception:
            traceback.print_exc()   # 退回缓冲路径,读循环照常
            return
        run_id = int((bound or {}).get("run_id") or 0)
        emit = (bound or {}).get("emit")
        if run_id <= 0 or not callable(emit):
            return
        with self._wake_lock:
            self._wake_run_id = run_id
            self._wake_emit = emit
            self._wake_trigger_id = int((bound or {}).get("trigger_id") or 0)
            # begin 之前缓冲的事件按序补发到运行
            pending = list(self._wake_events)
            self._wake_events.clear()
            finish_now = self._turn_active
            self._touch_wake_locked()
        for kind, text in pending:
            self._emit_wake(emit, kind, text)
        if finish_now:
            # begin 期间常规 turn 已开始:立即收尾,运行不悬挂
            self._dispatch_wake()

    def _wake_watchdog(self) -> None:
        """自发 turn 没有终止标记,按静默间隔判定结束后交付。"""
        while True:
            time.sleep(min(_WAKE_DEBOUNCE_SECONDS, 0.2))
            with self._wake_lock:
                if self._turn_active:
                    # 新运行 turn 已开始,begin_turn 负责交付缓冲
                    self._wake_timer_running = False
                    return
                quiet = time.time() - self._wake_last_at
                limit = _WAKE_DEBOUNCE_SECONDS
                silent = not "".join(self._wake_chunks).strip()
                if self._wake_run_id:
                    if self._wake_open_tools:
                        limit = _WAKE_TOOL_SECONDS
                    elif self._wake_report_tries and silent:
                        # 上一次补发的汇报没有回音:Runtime 多半还在自发 turn 里
                        limit = _WAKE_REPORT_RETRY_SECONDS * self._wake_report_tries
                    elif self._wake_last_kind != "text":
                        limit = _WAKE_THINK_SECONDS
                if quiet >= limit:
                    self._wake_timer_running = False
                    attempt = 0
                    if (self._wake_run_id and silent and self.session_id
                            and self.wake_meta.get("session_key")
                            and self._wake_report_tries < _WAKE_REPORT_LIMIT):
                        self._wake_report_tries += 1
                        self._wake_reporting = True
                        attempt = self._wake_report_tries
                    break
        if attempt:
            self._request_wake_report(attempt)
        else:
            self._dispatch_wake()

    def _request_wake_report(self, attempt: int) -> None:
        """自发 turn 没有正文:在同一会话补发一轮 prompt,请 Runtime 说出汇报。

        这一轮的 session/update 仍按自发 turn 归属(_turn_active 为 False),
        正文与过程事件直接进已落成的运行;应答到达即结束,不靠静默判定。"""
        lock = _session_lock(str(self.wake_meta.get("session_key") or ""))
        error = ""
        # 常规运行正在接管这个会话时不与它抢 turn:它的 turn 一开始就会收尾
        # 本轮;它没开始成(被取消)则按下面的重试再补发
        if lock.acquire(blocking=False):
            try:
                with self._wake_lock:
                    emit = self._wake_emit
                    bound = bool(self._wake_run_id) and not self._turn_active
                if bound and emit is not None:
                    self._emit_wake(
                        emit, "status",
                        "自发的一轮没有向平台回传正文,请 Runtime 补发汇报"
                        f" ({attempt}/{_WAKE_REPORT_LIMIT})\n")
                    try:
                        response = self.request("session/prompt", {
                            "sessionId": self.session_id,
                            "prompt": [{"type": "text", "text": _WAKE_REPORT_PROMPT}],
                        }, timeout=_WAKE_REPORT_TIMEOUT)
                        usage = _turn_usage(
                            [response], self.context_usage, self.parse_usage)
                        if usage:
                            self._emit_wake(emit, "usage",
                                            json.dumps(usage, ensure_ascii=False))
                    except AcpError as exc:
                        error = str(exc)
            finally:
                lock.release()
        with self._wake_lock:
            self._wake_reporting = False
            emit = self._wake_emit
            retry = (bool(self._wake_run_id) and not self._turn_active and not error
                     and not "".join(self._wake_chunks).strip()
                     and self._wake_report_tries < _WAKE_REPORT_LIMIT)
            if retry:
                self._touch_wake_locked()   # 重新计静默,到点再补发一次
        if retry:
            if emit is not None:
                self._emit_wake(emit, "status",
                                "Runtime 没有回复汇报(可能仍在执行自发的一轮),稍后重试\n")
            return
        # 进程已退出时 _abort_wake 已收尾,这里的调用无事可做
        self._dispatch_wake(success=not error,
                            error=f"补发汇报失败: {error}" if error else "")

    def _dispatch_wake(self, success: bool = True, error: str = "") -> None:
        with self._wake_lock:
            run_id = self._wake_run_id
            output = "".join(self._wake_chunks).strip()
            events = list(self._wake_events)
            tasks = list(self._wake_tasks)
            self._wake_chunks.clear()
            self._wake_events.clear()
            self._wake_run_id = 0
            self._wake_emit = None
            self._wake_trigger_id = 0
            self._wake_begin_tried = False
            self._wake_open_tools.clear()
            self._wake_last_kind = ""
            self._wake_report_tries = 0
            if not output and not run_id:
                # 没有可交付正文(纯思考/工具残留):丢弃事件,保留任务
                # 记录等待真正的汇报 turn
                return
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
            "success": success,
            "output": output,
            "error": error,
            # 已落成运行的事件已实时写入,收尾 payload 不再重复携带
            "events": [] if run_id else events,
            "tasks": tasks,
            "run_id": run_id,
        }
        # 平台回调可能发消息/落库,不能阻塞协议读取线程
        threading.Thread(target=handler, args=(payload,), daemon=True,
                         name="acp-wake").start()

    def _abort_wake(self, error: str) -> None:
        """进程关闭或退出时,已落成的唤醒运行按失败收尾,不悬在 running。"""
        with self._wake_lock:
            bound = bool(self._wake_run_id)
        if bound:
            self._dispatch_wake(success=False, error=error)

    def _emit_live(self, kind: str, text: str) -> None:
        """过程事件归属:常规 turn,或已落成运行的自发 turn。"""
        with self._wake_lock:
            emit = None if self._turn_active else self._wake_emit
        if emit is not None:
            self._emit_wake(emit, kind, text)
        else:
            self.emit(kind, text)

    def _watch_terminal(self, terminal_id: str,
                        terminal: _ClientTerminal) -> None:
        terminal.exited.wait()
        status = terminal.exit_status() or {}
        state = "completed" if status.get("exitCode") == 0 else "failed"
        line = f"客户端终端已退出: {terminal.command[:160]} ({state})\n"
        if self._turn_active:
            self.emit("status", line)
            return
        with self._wake_lock:
            # 本轮自发 turn 自己起的命令在本轮内结束:只是它的过程事件
            emit = (self._wake_emit if terminal.wake_run_id
                    and terminal.wake_run_id == self._wake_run_id else None)
            if emit is None:
                self._wake_tasks.append({
                    "task_id": terminal_id,
                    "description": terminal.command[:200],
                    "status": state,
                    "origin_trigger": terminal.origin_trigger,
                })
                del self._wake_tasks[:-10]
            else:
                self._touch_wake_locked()   # 命令刚结束,模型还要想下一步
        if emit is not None:
            self._emit_wake(emit, "status", line)

    def live_terminal_count(self) -> int:
        with self._terminals_lock:
            return sum(1 for t in self._terminals.values() if t.running)

    def wake_run_bound(self) -> bool:
        """自发 turn 已落成运行且尚未收尾。"""
        with self._wake_lock:
            return bool(self._wake_run_id)

    def spontaneous_activity_at(self) -> float:
        """turn 之外最近一次自发活动的时间;从未有过为 0。"""
        return self._spontaneous_at

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
        if not self._turn_active and method != "terminal/output":
            # turn 之外主动找客户端(审批、起/收终端)说明 Runtime 在自发工作;
            # terminal/output 是对存活终端的周期轮询,不算
            self._spontaneous_at = time.time()
            with self._wake_lock:
                if self._wake_run_id:
                    self._touch_wake_locked()
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
            if not self._turn_active:
                # 自发 turn 要跑命令:命令里可能调 Agent Tool,先落成运行拿令牌
                self._begin_wake_run()
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
            # 记录发起 turn 的触发消息:唤醒汇报按它继承派发语义。自发 turn
            # 起的命令归属该轮落成的运行,触发消息是它的平台锚点
            with self._wake_lock:
                in_wake = not self._turn_active and self._wake_run_id > 0
                terminal.wake_run_id = self._wake_run_id if in_wake else 0
                wake_trigger = self._wake_trigger_id if in_wake else 0
                if in_wake:
                    self._touch_wake_locked()
            terminal.origin_trigger = wake_trigger or int(
                self.wake_meta.get("trigger_message_id") or 0)
            with self._terminals_lock:
                self._terminal_seq += 1
                terminal_id = f"mc-term-{self._terminal_seq}"
                self._terminals[terminal_id] = terminal
            threading.Thread(target=self._watch_terminal,
                             args=(terminal_id, terminal), daemon=True).start()
            self._emit_live(
                "status", f"客户端终端已创建 {terminal_id}: {terminal.command[:160]}\n")
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

    def request(self, method: str, params: dict,
                timeout: Optional[float] = None) -> dict:
        """发请求并等应答;timeout 缺省时沿用本轮 begin_turn 定下的期限。"""
        self._next_id += 1
        rid = self._next_id
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        if method == "session/load":
            self._load_request_id = rid
        try:
            try:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "method": method, "params": params})
            except (OSError, ValueError) as exc:
                raise AcpError(f"{method} 写入失败: {exc}") from exc
            remaining = (timeout if timeout is not None
                         else self.deadline - time.time()
                         if self.deadline is not None else None)
            if remaining is not None and remaining <= 0:
                raise AcpError(f"{method} 超时")
            try:
                msg = q.get(timeout=remaining)
            except queue.Empty:
                raise AcpError(f"{method} 超时")
            if "error" in msg:
                error = msg["error"]
                detail = error.get("message", error) if isinstance(error, dict) else error
                # traecli 的 message 只有通用的 "Invalid params",具体原因在
                # 字符串 data 里(如 Unsupported reasoning effort for selected model)
                data = error.get("data") if isinstance(error, dict) else None
                if isinstance(data, str) and data and data not in str(detail):
                    detail = f"{detail}: {data}"
                raise AcpError(f"{method} 失败: {detail}")
            return msg.get("result") or {}
        finally:
            self._pending.pop(rid, None)
            # 写入失败、超时和 EOF 没有正常响应,也必须释放回放隔离。
            if self._load_request_id == rid:
                self._load_request_id = None

    def close(self) -> None:
        # 客户端终端随客户端关闭而终止(与服务重启杀后台命令的口径一致)
        with self._terminals_lock:
            terminals = list(self._terminals.values())
            self._terminals.clear()
        for terminal in terminals:
            terminal.kill()
        self._abort_wake("Runtime 会话已停止,自发汇报未完成")
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


def _client_signature(cmd: list[str], workdir: str, env: dict,
                      additional_dirs: tuple[str, ...] = ()) -> tuple:
    # ACP serve 进程启动后不能更新环境；项目目录权限渲染进 cmd 或经协议的
    # additionalDirectories 传入(只在建会话时生效,变化同样要换进程)，其余平台
    # 环境只比较稳定的 MISSIONCREW_*。旧调用方可能仍传逐轮 run id；它由
    # Agent Tool 的 run-scoped token 取代，不能因此打断长驻会话。
    platform_env = tuple(sorted(
        (key, str(value)) for key, value in env.items()
        if key.startswith("MISSIONCREW_")
        and key != "MISSIONCREW_AGENT_RUN_ID"
    ))
    return tuple(cmd), workdir, platform_env, tuple(additional_dirs)


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
            client = live.client
            # 客户端终端里仍有存活的后台命令时不回收:关闭客户端会连带
            # 杀掉这些命令,长任务必须等它们退出并完成自发汇报。汇报那一轮
            # 不经 run_prompt,不刷新 last_used:已落成运行的自发 turn 不回收,
            # 空闲时长也从最近一次自发活动算起,否则最后一个终端一退出,
            # 正在汇报的会话就会被当成闲置关掉
            if (not live.busy
                    and max(live.last_used, client.spontaneous_activity_at()) < cutoff
                    and client.live_terminal_count() == 0
                    and not client.wake_run_bound()):
                stale.append(_LIVE_SESSIONS.pop(key))
    for live in stale:
        live.client.close()


def cleanup_idle_sessions() -> None:
    """回收空闲超时的长驻 ACP 会话;供 RuntimeManager 的周期回收调用,
    否则只有下一次 ACP 执行才会触发惰性清理。"""
    _cleanup_idle_sessions()


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
            started_at=client.started_at,
            last_activity=max(live.last_used, client.spontaneous_activity_at()),
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


def _thought_level_option(result: dict) -> tuple[str, list[str]]:
    """从 session/new|load|set_config_option 应答里取推理力度配置项。

    ACP 标准的 configOptions 里 category=thought_level 的 select 选项就是
    档位(kimi: id="thinking",K3 为 low/high/max,K2.7 为 on/high)。返回
    (配置项 id, 取值列表,按协议原顺序);没有该选项时返回 ("", [])。
    """
    for opt in result.get("configOptions") or []:
        if not isinstance(opt, dict):
            continue
        if opt.get("category") == "thought_level" or opt.get("id") == "thought_level":
            values = [str(o.get("value", "")) if isinstance(o, dict) else str(o)
                      for o in opt.get("options") or []]
            return str(opt.get("id") or ""), [v for v in values if v]
    return "", []


def _new_or_load_session(client: _AcpClient, workdir: str,
                         requested_session_id: str,
                         initialize_result: dict,
                         load_meta: Optional[dict] = None,
                         additional_dirs: Optional[list[str]] = None
                         ) -> tuple[str, bool]:
    """返回 (会话 id, 是否成功恢复)。不支持/无法 load 时创建新会话。

    顺带记下应答里的推理力度配置项 id(client.thought_level_option),供
    每轮 prompt 前按 effort 切换档位。
    """
    capabilities = initialize_result.get("agentCapabilities") or {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    can_load = capabilities.get("loadSession") is True
    # 会话级额外目录:agent 声明 sessionCapabilities.additionalDirectories(空对象
    # 即支持)时随 session/new|load 传入,load 时以本次传入的清单为准(traecli
    # 实测);未声明的工具不在协议里传,只靠 serve 命令行参数授权。
    session_caps = capabilities.get("sessionCapabilities")
    extra_dirs = ({"additionalDirectories": list(additional_dirs)}
                  if additional_dirs and isinstance(session_caps, dict)
                  and session_caps.get("additionalDirectories") is not None
                  else {})
    if requested_session_id and can_load:
        try:
            params = {
                "sessionId": requested_session_id,
                "cwd": workdir,
                "mcpServers": [],
                **extra_dirs,
            }
            if load_meta:
                params["_meta"] = dict(load_meta)
            result = client.request("session/load", params)
            client.thought_level_option = _thought_level_option(result)[0]
            return (result.get("sessionId") or result.get("session_id")
                    or requested_session_id), True
        except AcpError as exc:
            client.emit("status", f"ACP 原会话恢复失败，将用最近对话新建会话: {exc}\n")
    elif requested_session_id:
        client.emit("status", "ACP Runtime 未声明 session/load，将用最近对话新建会话。\n")

    result = client.request("session/new", {"cwd": workdir, "mcpServers": [],
                                            **extra_dirs})
    session_id = result.get("sessionId") or result.get("session_id") or ""
    if not session_id:
        raise AcpError("session/new 未返回 sessionId")
    client.thought_level_option = _thought_level_option(result)[0]
    return session_id, False


def _turn_usage(responses: list[dict], context: dict,
                parse_usage: Optional[Callable[[dict], dict]] = None) -> dict:
    """把一轮内各 session/prompt 响应与最近一次 usage_update 归一成 usage/v1。

    同一轮的续接 prompt 各有一份用量:本轮区段、耗时、费用逐项相加,会话累计与
    上下文取最后一次。标准字段优先;工具声明的 parse_usage 钩子解析厂商私有
    扩展(grok 的 _meta.usage 等),只补标准字段缺失的部分。一个数都没有返回空。
    """
    # native 顶层导入 adapters,延迟导入避免 acp <-> adapters 的初始化环
    from .native import build_usage, usage_section
    turn: dict = {}
    summed: dict = {}
    latest: dict = {}
    for response in responses:
        if not isinstance(response, dict):
            continue
        try:
            vendor = parse_usage(response) if parse_usage else {}
        except Exception:
            # 用量只是过程信息,解析失败不能让已经成功的回合失败
            vendor = {}
        if not isinstance(vendor, dict):
            vendor = {}
        section = (usage_section(response.get("usage"), ACP_USAGE_FIELDS)
                   or vendor.get("turn") or {})
        for name, value in section.items():
            turn[name] = turn.get(name, 0) + value
        for key in ("cost_usd", "duration_ms", "tool_uses"):
            if isinstance(vendor.get(key), (int, float)):
                summed[key] = summed.get(key, 0) + vendor[key]
        for key in ("total", "context_used", "context_window"):
            if vendor.get(key):
                latest[key] = vendor[key]
    if isinstance(context.get("used"), (int, float)):
        latest["context_used"] = context["used"]
    if isinstance(context.get("size"), (int, float)):
        latest["context_window"] = context["size"]
    cost = context.get("cost")
    if (isinstance(cost, dict) and str(cost.get("currency", "")).upper() == "USD"
            and isinstance(cost.get("amount"), (int, float))):
        # 标准 cost 是会话累计值,与厂商的本轮费用二者取一,标准优先
        summed["cost_usd"] = cost["amount"]
    raw: dict = {"responses": [{k: v for k, v in r.items() if k != "stopReason"}
                               for r in responses if isinstance(r, dict)]}
    if context:
        raw["usage_update"] = {k: v for k, v in context.items() if k != "sessionUpdate"}
    return build_usage(raw, turn=turn, total=latest.get("total"),
                       context_window=latest.get("context_window"),
                       context_used=latest.get("context_used"), **summed)


def _prompt_turn(client: _AcpClient, session_id: str, prompt: str,
                 model: str, effort: str = "",
                 before_prompt: Optional[Callable[[], None]] = None) -> str:
    """发一轮 prompt 并等待 turn 结束;before_prompt 在 session/prompt 真正
    发出前调用(模型/档位已对齐),供调用方在这一刻持久化会话 id。"""
    if model:
        client.request("session/set_model", {"sessionId": session_id,
                                              "modelId": model})
    if effort:
        # 先定模型再定档位:可选档位随模型变化(kimi K2.7 没有 max)。越界值由
        # Runtime 自己报错(kimi 返回 -32602),照常作为本轮失败回流到频道
        if client.thought_level_option:
            try:
                client.request("session/set_config_option", {
                    "sessionId": session_id,
                    "configId": client.thought_level_option,
                    "value": effort})
            except AcpError as exc:
                raise AcpError(f"ACP Runtime 拒绝推理力度 effort={effort}: {exc}") from exc
        else:
            client.emit("status", f"ACP Runtime 未声明推理力度配置项(thought_level)，"
                                  f"effort={effort} 未生效。\n")
    client.emit("input", prompt)
    if before_prompt is not None:
        before_prompt()
    responses = [client.request("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}],
    })]
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
        responses.append(client.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": continuation}],
        }))
    usage = _turn_usage(responses, client.context_usage, client.parse_usage)
    if usage:
        client.emit("usage", json.dumps(usage, ensure_ascii=False))
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
                  project_id: str = "", role_id: str = "",
                  effort: str = "",
                  additional_dirs: Optional[list[str]] = None,
                  parse_usage: Optional[Callable[[dict], dict]] = None
                  ) -> tuple[bool, str]:
    try:
        client = _AcpClient(cmd, workdir, env, timeout, emit=emit)
    except OSError as e:
        return False, f"ACP 进程启动失败: {e}"
    client.parse_usage = parse_usage
    with _ONE_SHOT_CLIENTS_GUARD:
        _ONE_SHOT_CLIENTS[id(client)] = _OneShotClient(
            runtime_id, client, task_id, stage_name, project_id, role_id, model)
    try:
        initialize_result = _initialize(client)
        session_id, _ = _new_or_load_session(
            client, workdir, "", initialize_result,
            additional_dirs=additional_dirs)
        return True, _prompt_turn(client, session_id, prompt, model, effort)
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
               trigger_message_id: int = 0,
               effort: str = "",
               additional_dirs: Optional[list[str]] = None,
               parse_usage: Optional[Callable[[dict], dict]] = None
               ) -> tuple[bool, str]:
    """完成一轮 ACP prompt，并按 channel×role 复用长驻原生会话。

    无 ``session_key`` 时保持一次性调用。长驻进程不存在（包括服务重启）时，
    仅在 Runtime 声明 loadSession 能力后恢复持久化 id；否则创建新会话并使用
    ``recovery_prompt``，避免把缺失的历史当成已恢复。

    ``effort`` 是要经会话配置项(thought_level)切换的推理力度;serve 命令里
    已带档位的工具(grok/copilot)不要再传,它们改档位靠命令签名变化重启进程。

    ``additional_dirs`` 是工作目录之外的项目授权目录;agent 声明了会话级
    additionalDirectories 能力时经协议传入,否则忽略(由 serve 命令行授权)。
    ``parse_usage`` 是工具声明的私有用量解析钩子,见 :func:`_turn_usage`。
    """
    if cancelled and cancelled():
        return False, "执行已停止"
    if not session_key:
        return _run_one_shot(
            cmd, prompt, workdir, env, model, timeout, runtime_id, emit,
            task_id, stage_name, project_id, role_id, effort=effort,
            additional_dirs=additional_dirs, parse_usage=parse_usage)

    _cleanup_idle_sessions()
    signature = _client_signature(cmd, workdir, env, tuple(additional_dirs or ()))
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
                        load_session_meta, additional_dirs)
                except Exception:
                    client.close()
                    raise
                client.session_id = runtime_session_id
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
            live.client.parse_usage = parse_usage
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

            def _persist_open_session() -> None:
                # prompt 一经交给 Runtime 就落库会话 id(轮内刷新,不动
                # 注入计数):首轮会话在 turn 中途被人工停止时,下一轮仍能按
                # 这个 id session/load 续接已发生的工作,而不是从头新建会话。
                if not save_session:
                    return
                try:
                    save_session(live.session_id, context_version)
                except Exception:
                    live.client.emit("status", "Runtime 会话已建立,但持久化会话 id 失败。\n")

            reply = _prompt_turn(live.client, live.session_id, actual_prompt,
                                 model, effort, before_prompt=_persist_open_session)
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


def list_model_catalog(
        cmd: list[str], env: Optional[dict] = None, timeout: int = 30,
        runtime_id: str = "",
        parse_efforts=None) -> tuple[list[str], dict[str, list[str]]]:
    """一次性会话同时取回模型目录与每个模型支持的推理力度。

    模型目录兼容两种形态:kimi/trae 等返回 configOptions(category=model 的 select
    选项);部分实现只返回 models 块 {availableModels:[{modelId,...}]}(对齐
    Multica parseACPSessionNewModels,兼容 snake_case 与旧的 {available:[...]}
    及裸数组)。两者都有时以 configOptions 为准:trae 的 models 块把模型与档位
    拼成 "glm-5.3/high" 这类组合 id,不是可单独选择的模型。查不到返回空目录与
    空档位表。

    按模型的推理力度有两条路:
    - ACP 标准的会话配置项:configOptions 里有 category=thought_level 的选项
      (kimi 的 `thinking`)时,其取值就是档位;但 session/new 只给当前模型的
      取值,而档位随模型变化(K3 low/high/max,K2.7 on/high),所以在同一会话里
      逐模型 session/set_config_option(model) 并从应答读回该模型的档位;
    - 厂商私有扩展(grok 的 models[]._meta.reasoningEfforts):协议层不认识,
      只把 models 块透传给 ``parse_efforts`` 回调(来自该工具的 clis 声明)。
    两条路都不通 = 档位表为空,调用方回退静态档位。全部信息来自同一次探测
    会话,避免为了档位再启一个 CLI 进程。
    """
    import tempfile
    workdir = tempfile.mkdtemp(prefix="mc-acp-models-")
    try:
        client = _AcpClient(cmd, workdir, env or host_isolated_environ(), timeout)
    except OSError:
        return [], {}
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
        model_option_id = ""
        for opt in sess.get("configOptions") or []:
            if opt.get("category") == "model" or opt.get("id") == "model":
                model_option_id = str(opt.get("id") or "model")
                models = [str(o.get("value", "")) for o in opt.get("options", [])]
                break
        block = sess.get("models")
        if isinstance(block, dict):
            block = (block.get("availableModels") or block.get("available_models")
                     or block.get("available") or [])
        if not models and isinstance(block, list):
            models = [str(m.get("modelId") or m.get("model_id") or m.get("id")
                          or m.get("value") or "")
                      if isinstance(m, dict) else str(m) for m in block]
        models = [m for m in models if m]
        efforts = parse_efforts(block) if parse_efforts else {}
        if not efforts and model_option_id and _thought_level_option(sess)[0]:
            efforts = _probe_thought_levels(
                client, str(sess.get("sessionId") or sess.get("session_id") or ""),
                model_option_id, models)
        return models, efforts
    except AcpError:
        return [], {}
    finally:
        with _ONE_SHOT_CLIENTS_GUARD:
            _ONE_SHOT_CLIENTS.pop(id(client), None)
        client.close()


def _probe_thought_levels(client: _AcpClient, session_id: str,
                          model_option_id: str,
                          models: list[str]) -> dict[str, list[str]]:
    """在探测会话里逐模型切换,读回各模型的 thought_level 取值。

    某个模型切换失败就跳过(不入表 = 回退静态档位),不影响其他模型。
    """
    efforts: dict[str, list[str]] = {}
    for model in models:
        try:
            result = client.request("session/set_config_option", {
                "sessionId": session_id, "configId": model_option_id,
                "value": model})
        except AcpError:
            continue
        levels = _thought_level_option(result)[1]
        if levels:
            efforts[model] = levels
    return efforts


def list_models(cmd: list[str], env: Optional[dict] = None,
                timeout: int = 30, runtime_id: str = "") -> list[str]:
    """只取模型目录;推理力度一并需要时用 :func:`list_model_catalog`。"""
    return list_model_catalog(cmd, env, timeout, runtime_id)[0]
