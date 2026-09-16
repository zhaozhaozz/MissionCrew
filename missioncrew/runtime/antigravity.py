"""Antigravity headless stream-json;每轮新进程,按 conversation_id 续接。"""
from __future__ import annotations

import json
import subprocess
import threading
from collections import deque
from dataclasses import replace
from pathlib import Path

from ..core.models import Backend, ExecutionConfig, RunResult
from . import adapters
from .base import RuntimeCapabilities, RuntimeExecutionInfo, RuntimeProvider, RuntimeUsageSnapshot
from .clis.antigravity import discover_models
from .native import OutputAssembler, build_usage, emit_json, safe_emit, usage_section
from .usage import probe_antigravity_usage

_USAGE_FIELDS = {
    "input": "input_tokens", "output": "output_tokens",
    "reasoning": "thinking_tokens", "cache_read": "cache_read_tokens",
    "total": "total_tokens",
}


class _Turn:
    def __init__(self, config: ExecutionConfig):
        self.config = config
        self.session_id = ""
        self.output = OutputAssembler()
        self.result: dict | None = None
        self.error = ""
        self.step_usage: dict[int, dict] = {}
        self.tools: set[int] = set()

    def handle(self, message: dict) -> None:
        event = message.get("event")
        payload = message.get(event) if isinstance(event, str) else None
        if not isinstance(payload, dict):
            return
        session_id = message.get("conversation_id") or payload.get("conversation_id")
        if isinstance(session_id, str) and session_id:
            expected = self.session_id or self.config.session_id
            if expected and expected != session_id:
                # agy 可能找不到旧会话后静默新建;不能把缺失公共上下文的轮次交付。
                self.error = "Antigravity 未恢复指定 conversation;请重试以重新注入上下文。"
                adapters._clear_session(self.config)
                return
            if not self.session_id:
                self.session_id = session_id
                if self.config.session_key:
                    adapters._save_session(self.config, session_id)
        if event == "step_update":
            index = payload.get("step_index")
            if payload.get("step_type") == "agent_response":
                delta = payload.get("text_delta")
                if isinstance(delta, str):
                    safe_emit(self.config.emit, "text", self.output.append(delta))
                if payload.get("state") == "DONE":
                    self.output.finish_message()
            elif payload.get("step_type") == "tool":
                info = payload.get("tool_info")
                if not isinstance(info, dict):
                    info = {}
                if isinstance(index, int) and index not in self.tools:
                    self.tools.add(index)
                    emit_json(self.config.emit, "tool", {
                        "name": payload.get("tool_name") or info.get("name"),
                        "input": info.get("parameters", {}),
                    })
                if payload.get("state") in ("DONE", "ERROR"):
                    emit_json(self.config.emit, "tool_result", {
                        "name": payload.get("tool_name") or info.get("name"),
                        "output": info.get("output", ""),
                        **({"error": info["error"]} if "error" in info else {}),
                    })
            if payload.get("state") == "DONE" and isinstance(index, int):
                self.step_usage[index] = usage_section(payload.get("usage"), _USAGE_FIELDS)
        elif event == "result":
            self.result = payload
            turn: dict = {}
            for section in self.step_usage.values():
                for name, count in section.items():
                    turn[name] = turn.get(name, 0) + count
            duration = payload.get("duration_seconds")
            # result.usage 是会话累计值,不能标成本轮用量。
            usage = build_usage(
                {"usage": payload.get("usage"), "duration_seconds": duration},
                turn=turn, total=usage_section(payload.get("usage"), _USAGE_FIELDS),
                duration_ms=duration * 1000 if isinstance(duration, (int, float)) else None)
            if usage:
                emit_json(self.config.emit, "usage", usage)


class AntigravityRuntimeProvider(RuntimeProvider):
    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(session_reuse=True, structured_events=True,
                                   permission_control=False, user_interaction=False,
                                   account_usage=True)

    def account_usage(self, backend: Backend, timeout: int = 15) -> RuntimeUsageSnapshot:
        return probe_antigravity_usage(backend, timeout)

    def effort_catalog(self) -> dict[str, list[str]]:
        return {"antigravity": ["low", "medium", "high"]}

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return discover_models(backend, timeout)[0]

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        return RuntimeExecutionInfo(mode="one_shot", transport="antigravity-stream-json")

    def instances(self, backend: Backend):
        return [replace(item, transport="antigravity-stream-json")
                for item in adapters.active_execution_instances(backend.id)
                if item.adapter == "antigravity"]

    def stop(self, backend: Backend, session_key: str = "") -> int:
        return adapters.stop_active_executions(backend.id, session_key)

    def shutdown(self) -> None:
        for item in adapters.active_execution_instances():
            if item.adapter == "antigravity":
                adapters.stop_active_executions(item.backend_id, item.session_key)

    def _command(self, config: ExecutionConfig, prompt: str) -> list[str]:
        permissions = config.runtime_policy.permissions
        # Headless 没有逐工具审批协议;plan 只是提示词,也不是只读沙箱。
        if permissions.approval != "auto":
            raise ValueError("Antigravity headless 不支持 prompt/deny 审批;请使用 auto 或支持权限交互的 Runtime。")
        if permissions.filesystem == "read-only":
            raise ValueError("Antigravity headless 不支持强制 read-only 文件系统。")
        if permissions.network == "deny":
            raise ValueError("Antigravity headless 不支持覆盖本机配置以强制 network=deny。")
        # agy 的 0s 会立即返回部分结果,并非禁用超时。None 使用接近 Go
        # duration 上限的值,由平台停止按钮管理生命周期,避免默认 5 分钟截断。
        timeout = f"{config.timeout:.9f}s" if config.timeout is not None else "2562047h"
        cmd = [config.backend.binary_path or "agy", "--output-format", "stream-json",
               "--dangerously-skip-permissions", "--disable-slash-commands",
               "--print-timeout", timeout]
        if permissions.filesystem == "workspace-write":
            cmd.append("--sandbox")
        else:
            cmd.append("--sandbox=false")
        if config.backend.model:
            cmd += ["--model", config.backend.model]
        if config.effort:
            cmd += ["--effort", config.effort]
        # cwd 不足以让 agy 的语言服务选中工作区;主目录也必须显式注册。
        workdir = str(Path(config.workdir).expanduser().resolve())
        for directory in [workdir, *adapters._additional_allowed_dirs(workdir, config.allowed_dirs)]:
            cmd += ["--add-dir", directory]
        if config.session_id:
            cmd += ["--conversation", config.session_id]
        return [*cmd, "-p", prompt]

    def start(self, config: ExecutionConfig) -> RunResult:
        if config.session_key:
            with adapters._named_session_lock(config.session_key):
                adapters._refresh_session(config)
                return self._run(config)
        return self._run(config)

    def _run(self, config: ExecutionConfig) -> RunResult:
        if config.cancellation_requested():
            return RunResult(False, "执行已停止")
        prompt, mode = adapters._session_input(config, recovery=not bool(config.session_id))
        try:
            command = self._command(config, prompt)
        except ValueError as exc:
            return RunResult(False, str(exc))
        adapters._emit_execution_start(config.emit, [*command[:-1], "<输入>"], prompt)
        turn = _Turn(config)
        logs: deque[str] = deque(maxlen=400)
        errors: deque[str] = deque(maxlen=100)
        try:
            proc = subprocess.Popen(
                command, cwd=str(Path(config.workdir).expanduser().resolve()),
                env=adapters._runtime_env(config, "antigravity"),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", start_new_session=True, bufsize=1)
        except OSError as exc:
            return RunResult(False, f"Antigravity 启动失败: {exc}")
        adapters._track_process(config, proc, command)

        def read_stdout():
            for line in proc.stdout:
                logs.append(line.rstrip())
                try:
                    message = json.loads(line)
                except ValueError:
                    safe_emit(config.emit, "stdout", line)
                    continue
                if isinstance(message, dict):
                    try:
                        turn.handle(message)
                    except Exception as exc:
                        turn.error = f"Antigravity 协议解析失败: {exc}"
                    if turn.error:
                        adapters._kill_process_group(proc)
                        return

        def read_stderr():
            for line in proc.stderr:
                errors.append(line.rstrip())
                safe_emit(config.emit, "stderr", line)

        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers:
            reader.start()
        failure = ""
        try:
            if config.cancellation_requested():
                failure = "执行已停止"
                adapters._kill_process_group(proc)
            proc.wait(timeout=config.timeout)
        except subprocess.TimeoutExpired:
            failure = f"执行超时({config.timeout}s)"
        finally:
            # 一轮一个进程;连同继承 stdout 的子进程一起回收,避免管道永不 EOF。
            adapters._kill_process_group(proc)
            for reader in readers:
                reader.join(timeout=5)
            adapters._untrack_process(proc)
            for pipe in (proc.stdout, proc.stderr):
                pipe.close()
            try:
                adapters._diagnostic_log_path(config, "antigravity").write_text(
                    "\n".join(logs) + "\n--- stderr ---\n" + "\n".join(errors))
            except OSError:
                pass
        result = turn.result or {}
        output = result.get("response")
        output = output.strip() if isinstance(output, str) else turn.output.text.strip()
        failure = failure or turn.error
        if not failure and any("print timeout" in line.lower() for line in errors):
            failure = "Antigravity print timeout;本轮仅返回部分输出。"
        if not failure and (proc.returncode != 0 or result.get("status") != "SUCCESS"):
            failure = str(result.get("error") or "\n".join(errors) or
                          f"Antigravity 未成功完成(status={result.get('status', 'missing result')}, exit={proc.returncode})")
        if not failure and result.get("denied_actions"):
            failure = "Antigravity 有工具操作被拒绝: " + json.dumps(result["denied_actions"], ensure_ascii=False)
        if not failure and not output:
            failure = "Antigravity 未产生可回传输出。"
        if config.session_id and adapters._session_missing(failure + "\n" + "\n".join(errors)):
            adapters._clear_session(config)
        if failure:
            return RunResult(False, failure, output="\n\n".join(part for part in (failure, output) if part))
        if config.session_key and turn.session_id:
            adapters._save_session(config, turn.session_id, mode, adapters._turn_bytes(prompt, output))
        return RunResult(True, output[-300:], output=output)
