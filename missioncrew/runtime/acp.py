"""ACP(Agent Client Protocol)stdio 客户端。

kimi / kiro / qoder / trae 等 CLI 不走"命令行传 prompt"的打印模式,而是作为
JSON-RPC 服务挂在 stdio 上(换行分隔的 JSON-RPC 2.0)。流程:

    initialize -> session/new -> [session/set_model] -> session/prompt
    期间收集 session/update 通知里的 agent_message_chunk 作为回复文本;
    agent 反向发来的 session/request_permission 必须应答(选它提供的安全
    选项),否则 agent 会阻塞到内部超时,任务假死。
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
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
        raw_emit = emit or (lambda kind, text: None)

        def _safe_emit(kind: str, text: str) -> None:
            # 上报失败只丢事件:读循环死亡会让所有 pending 请求挂到超时
            try:
                raw_emit(kind, text)
            except Exception:
                pass
        self.emit = _safe_emit                # 运行过程实时上报
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._write_lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()

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
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        remaining = self.deadline - time.time()
        if remaining <= 0:
            raise AcpError(f"{method} 超时")
        try:
            msg = q.get(timeout=remaining)
        except queue.Empty:
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


def run_prompt(cmd: list[str], prompt: str, workdir: str, env: dict,
               model: str = "", timeout: int = 900,
               emit: Optional[Callable[[str, str], None]] = None) -> tuple[bool, str]:
    """启动 ACP 服务进程,完成一轮 prompt,返回 (成功, 回复文本/错误)。

    emit 非空时,会话期间的思考/工具/文本/权限事件实时上报 (kind, text)。
    """
    try:
        client = _AcpClient(cmd, workdir, env, timeout, emit=emit)
    except OSError as e:
        return False, f"ACP 进程启动失败: {e}"
    try:
        client.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "missioncrew", "version": "0.4.0"},
            "clientCapabilities": {},
        })
        result = client.request("session/new", {"cwd": workdir, "mcpServers": []})
        session_id = result.get("sessionId") or result.get("session_id") or ""
        if not session_id:
            return False, "session/new 未返回 sessionId"
        if model:
            client.request("session/set_model",
                           {"sessionId": session_id, "modelId": model})
        client.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": prompt}],
        })
        reply = "".join(client.chunks).strip()
        return True, reply or "(无输出)"
    except AcpError as e:
        return False, str(e)
    finally:
        client.close()


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
