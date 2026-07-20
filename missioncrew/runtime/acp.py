"""ACP(Agent Client Protocol)stdio 客户端。

kimi / kiro / qoder / trae 等 CLI 不走"命令行传 prompt"的打印模式,而是作为
JSON-RPC 服务挂在 stdio 上(换行分隔的 JSON-RPC 2.0)。流程:

    initialize -> session/new|session/load -> [session/set_model] -> session/prompt
    期间收集 session/update 通知里的 agent_message_chunk 作为回复文本;
    agent 反向发来的 session/request_permission 必须应答(选它提供的安全
    选项),否则 agent 会阻塞到内部超时,任务假死。
"""
from __future__ import annotations

import atexit
import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


class AcpError(Exception):
    pass


class _AcpClient:
    def __init__(self, cmd: list[str], cwd: str, env: dict, timeout: int,
                 emit: Optional[Callable[[str, str], None]] = None):
        env = {**env, "PWD": cwd}
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env=env,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        self.deadline = time.time() + timeout
        self.chunks: list[str] = []          # agent_message_chunk 文本
        self._raw_emit = emit or (lambda kind, text: None)

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
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def begin_turn(self, timeout: int,
                   emit: Optional[Callable[[str, str], None]]) -> None:
        """为长驻客户端开启新一轮，重置超时、输出和事件接收器。"""
        self.deadline = time.time() + timeout
        self.chunks = []
        self._raw_emit = emit or (lambda kind, text: None)

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
            update = (msg.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate")
            content = update.get("content") or {}
            if kind == "agent_message_chunk":
                if content.get("type") == "text":
                    self.chunks.append(content.get("text", ""))
                    self.emit("text", content.get("text", ""))
            elif kind == "agent_thought_chunk":
                if content.get("type") == "text":
                    self.emit("thinking", content.get("text", ""))
            elif kind in ("tool_call", "tool_call_update"):
                label = update.get("title") or update.get("toolCallId") or ""
                status = update.get("status") or ""
                if label or status:
                    self.emit("tool", f"{label} {status}".strip() + "\n")

    def _handle_agent_request(self, msg: dict) -> None:
        """应答 agent -> client 方向的请求,平台是无头的,权限自动决策。"""
        if msg["method"] == "session/request_permission":
            option = _pick_permission_option((msg.get("params") or {}).get("options") or [])
            if option is not None:
                self.emit("status", f"权限请求:自动选择 {option}\n")
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "result": {"outcome": {"outcome": "selected",
                                                    "optionId": option}}})
            else:  # 没有可安全选择的项:返回协议错误而不是 cancelled(那会取消整轮)
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32000,
                                       "message": "no safely selectable option"}})
        else:  # 其他未知请求返回空结果,避免 agent 阻塞
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

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
        except OSError as exc:
            self._pending.pop(rid, None)
            raise AcpError(f"{method} 写入失败: {exc}") from exc
        remaining = self.deadline - time.time()
        if remaining <= 0:
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
    session_id: str
    signature: tuple
    last_used: float
    busy: bool = False


_LIVE_SESSIONS: dict[str, _LiveSession] = {}
_LIVE_SESSIONS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()
_SESSION_IDLE_SECONDS = 1800


def _session_lock(session_key: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_key, threading.Lock())


def _client_signature(cmd: list[str], workdir: str, env: dict) -> tuple:
    # ACP serve 进程启动后不能更新环境；项目目录权限已渲染进 cmd，其余平台
    # 环境只比较 MISSIONCREW_*，避免 PATH 等无关变化打断会话。
    platform_env = tuple(sorted(
        (key, str(value)) for key, value in env.items()
        if key.startswith("MISSIONCREW_")
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
            if not live.busy and live.last_used < cutoff:
                stale.append(_LIVE_SESSIONS.pop(key))
    for live in stale:
        live.client.close()


def close_sessions() -> None:
    """关闭全部长驻 ACP 会话，供服务退出和测试清理。"""
    with _LIVE_SESSIONS_GUARD:
        sessions = list(_LIVE_SESSIONS.values())
        _LIVE_SESSIONS.clear()
    for live in sessions:
        live.client.close()


atexit.register(close_sessions)


def _pick_permission_option(options: list[dict]) -> Optional[str]:
    """从 agent 提供的选项里挑安全项:单次允许 > 会话允许 > 单次拒绝。

    必须选 agent 实际提供的 optionId(ACP 契约是"从这些选项里挑"),
    编造的 id 会被当作拒绝。
    """
    for kind in ("allow_once", "allow_always", "reject_once"):
        for o in options:
            if o.get("kind") == kind and o.get("optionId"):
                return o["optionId"]
    return None


def _initialize(client: _AcpClient) -> dict:
    return client.request("initialize", {
        "protocolVersion": 1,
        "clientInfo": {"name": "missioncrew", "version": "0.4.0"},
        "clientCapabilities": {},
    })


def _new_or_load_session(client: _AcpClient, workdir: str,
                         requested_session_id: str,
                         initialize_result: dict) -> tuple[str, bool]:
    """返回 (会话 id, 是否成功恢复)。不支持/无法 load 时创建新会话。"""
    capabilities = initialize_result.get("agentCapabilities") or {}
    can_load = bool(isinstance(capabilities, dict)
                    and capabilities.get("loadSession") is True)
    if requested_session_id and can_load:
        try:
            result = client.request("session/load", {
                "sessionId": requested_session_id,
                "cwd": workdir,
                "mcpServers": [],
            })
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
    return "".join(client.chunks).strip() or "(无输出)"


def _run_one_shot(cmd: list[str], prompt: str, workdir: str, env: dict,
                  model: str, timeout: int,
                  emit: Optional[Callable[[str, str], None]]) -> tuple[bool, str]:
    try:
        client = _AcpClient(cmd, workdir, env, timeout, emit=emit)
    except OSError as e:
        return False, f"ACP 进程启动失败: {e}"
    try:
        initialize_result = _initialize(client)
        session_id, _ = _new_or_load_session(client, workdir, "", initialize_result)
        return True, _prompt_turn(client, session_id, prompt, model)
    except AcpError as e:
        return False, str(e)
    finally:
        client.close()


def run_prompt(cmd: list[str], prompt: str, workdir: str, env: dict,
               model: str = "", timeout: int = 900,
               emit: Optional[Callable[[str, str], None]] = None,
               session_key: str = "", session_id: str = "",
               recovery_prompt: str = "",
               save_session: Optional[Callable[[str, str], None]] = None,
               context_version: str = "") -> tuple[bool, str]:
    """完成一轮 ACP prompt，并按 channel×role 复用长驻原生会话。

    无 ``session_key`` 时保持一次性调用。长驻进程不存在（包括服务重启）时，
    仅在 Runtime 声明 loadSession 能力后恢复持久化 id；否则创建新会话并使用
    ``recovery_prompt``，避免把缺失的历史当成已恢复。
    """
    if not session_key:
        return _run_one_shot(cmd, prompt, workdir, env, model, timeout, emit)

    _cleanup_idle_sessions()
    signature = _client_signature(cmd, workdir, env)
    with _session_lock(session_key):
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
                        client, workdir, session_id, initialize_result)
                except Exception:
                    client.close()
                    raise
                live = _LiveSession(client, runtime_session_id, signature, time.time())
                with _LIVE_SESSIONS_GUARD:
                    _LIVE_SESSIONS[session_key] = live

            live.busy = True
            live.client.begin_turn(timeout, emit)
            actual_prompt = prompt if recovered else (recovery_prompt or prompt)
            reply = _prompt_turn(live.client, live.session_id, actual_prompt, model)
            live.last_used = time.time()
            if save_session:
                try:
                    save_session(live.session_id, context_version)
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


def list_models(cmd: list[str], env: Optional[dict] = None,
                timeout: int = 30) -> list[str]:
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
        client.close()
