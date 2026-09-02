"""Claude/Codex 原生双向协议 provider。"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from missioncrew.core.models import (Backend, ExecutionConfig, RunResult,
                                     RuntimePermissions, RuntimePolicy)
from missioncrew.runtime.base import RuntimeCapabilities, RuntimeProvider
from missioncrew.runtime.claude import ClaudeRuntimeProvider, _sandbox_settings
from missioncrew.runtime.codex import CodexRuntimeProvider

FAKE_NATIVE = str(Path(__file__).with_name("fake_native_runtime.py"))


def _fake_command(adapter: str) -> list[str]:
    command = [sys.executable, FAKE_NATIVE]
    return [*command, "app-server"] if adapter == "codex" else command


class _Fallback(RuntimeProvider):
    def __init__(self):
        self.started = False

    def start(self, config: ExecutionConfig) -> RunResult:
        self.started = True
        return RunResult(False, "fallback")

    def stop(self, backend: Backend, session_key: str = "") -> int:
        return 0

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(session_reuse=False)

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return ["fallback-model"]


def _config(tmp_path, adapter: str, prompt: str, saved: dict,
            events: list[tuple[str, str]], *, approval: str = "auto",
            filesystem: str = "workspace-write", network: str = "inherit",
            writable_paths=None, interact=None) -> ExecutionConfig:
    backend = Backend(id=adapter, name=adapter, adapter=adapter,
                      model="test-model")

    def load():
        return saved.get("id", ""), saved.get("context", ""), False

    def save(session_id, context, **stats):
        saved.update(id=session_id, context=context, **stats)

    return ExecutionConfig(
        task_id="chat", stage_name="chat", backend=backend,
        prompt=prompt, workdir=str(tmp_path), project_id="project-a", role_id="lead",
        runtime_policy=RuntimePolicy(
            readable_paths=[str(tmp_path)],
            writable_paths=(list(writable_paths) if writable_paths is not None
                            else [str(tmp_path)]),
            permissions=RuntimePermissions(
                approval=approval, filesystem=filesystem, network=network)),
        env={"FAKE_NATIVE_LAUNCH_LOG": str(tmp_path / f"{adapter}.launches")},
        timeout=10, session_key=f"channel::{adapter}",
        session_id=saved.get("id", ""), common_prompt="COMMON",
        turn_prompt=prompt, recovery_prompt="RECENT\n" + prompt,
        context_version="v1", load_session=load, save_session=save,
        emit=lambda kind, text: events.append((kind, text)),
        interact=interact,
    )


@pytest.mark.parametrize("adapter,provider_cls,expected", [
    ("claude_code", ClaudeRuntimeProvider, "claude answer 2"),
    ("codex", CodexRuntimeProvider, "codex answer 2"),
])
def test_native_provider_reuses_process_and_session(
        tmp_path, adapter, provider_cls, expected):
    fallback = _Fallback()
    provider = provider_cls(fallback, _fake_command(adapter))
    saved: dict = {}
    events: list[tuple[str, str]] = []
    try:
        first = provider.start(_config(tmp_path, adapter, "FIRST", saved, events))
        second = provider.start(_config(tmp_path, adapter, "SECOND", saved, events))
        assert first.success and second.success
        assert second.output == expected
        assert saved["id"].startswith("claude-session" if adapter == "claude_code"
                                      else "codex-thread")
        launches = (tmp_path / f"{adapter}.launches").read_text().splitlines()
        assert len(launches) == 1
        instances = provider.instances(Backend(
            id=adapter, name=adapter, adapter=adapter))
        assert len(instances) == 1
        assert instances[0].mode == "persistent"
        assert instances[0].state == "idle"
        assert instances[0].pid
        assert instances[0].native_session_id == saved["id"]
        assert (instances[0].project_id, instances[0].role_id) == ("project-a", "lead")
        kinds = {kind for kind, _ in events}
        assert {"status", "thinking", "tool", "text", "usage"} <= kinds
        # 用量在 provider 源头归一成 usage/v1,raw 保留原始上报
        usage = [json.loads(text) for kind, text in events if kind == "usage"][-1]
        assert usage["schema"] == "usage/v1"
        if adapter == "codex":
            assert "tool_result" in kinds
            assert usage["total"] == {"input": 10, "output": 4}
            assert usage["raw"] == {"total": {"inputTokens": 10, "outputTokens": 4}}
        else:
            assert usage["turn"] == {"input": 3, "cache_read": 20, "cache_write": 1, "output": 7}
            assert usage["duration_ms"] == 1 and usage["cost_usd"] == 0
            assert set(usage["raw"]) <= {"usage", "total_cost_usd", "duration_ms",
                                         "duration_api_ms", "num_turns"}
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_native_provider_stop_terminates_runtime_process(
        tmp_path, adapter, provider_cls):
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    backend = Backend(id=adapter, name=adapter, adapter=adapter)
    session_key = f"channel::{adapter}"
    try:
        assert provider.start(
            _config(tmp_path, adapter, "FIRST", {}, [])).success
        session = provider._sessions[session_key]
        process = (session.client.process
                   if adapter == "codex" and session.client is not None
                   else session.process)
        assert process is not None and process.poll() is None

        assert provider.stop(backend, session_key) == 1
        assert process.poll() is not None
        assert provider.instances(backend) == []
    finally:
        provider.shutdown()


def test_codex_provider_reads_account_rate_limits_from_app_server():
    provider = CodexRuntimeProvider(_Fallback(), _fake_command("codex"))
    try:
        snapshot = provider.account_usage(
            Backend(id="codex", name="Codex", adapter="codex"))
        assert snapshot.status == "ok" and snapshot.plan == "test-plan"
        assert [(window.label, window.used_percent) for window in snapshot.windows] == [
            ("5 小时 · Codex", 42), ("本周 · Codex", 18)]
        assert snapshot.metrics[0].value == "12"
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_native_compact_event_marks_session_for_reinjection(
        tmp_path, adapter, provider_cls):
    """Runtime 报告压缩后:立即持久化重注入标记,轮末保存携带 compact_seen。"""
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    saved: dict = {}
    events: list[tuple[str, str]] = []
    try:
        assert provider.start(
            _config(tmp_path, adapter, "FIRST", saved, events)).success
        assert saved.get("turn_mode") == "recovery"
        config = _config(tmp_path, adapter, "TRIGGER_COMPACT", saved, events)
        config.mark_reinject = lambda: saved.update(marked=True)
        assert provider.start(config).success
        assert saved.get("marked") is True
        assert config.compact_detected
        assert saved.get("turn_mode") == "lean" and saved.get("turn_bytes", 0) > 0
        assert saved.get("compact_seen") is True
        assert any(kind == "status" and "上下文压缩" in text
                   for kind, text in events)
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_native_multiple_messages_join_with_divider(
        tmp_path, adapter, provider_cls):
    """同一轮的多条完整消息之间用 Markdown 横线分隔,过程与结论可区分。"""
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    try:
        result = provider.start(
            _config(tmp_path, adapter, "TWO_MESSAGES", {}, []))
        assert result.success
        assert result.output == "先说明进度。\n\n---\n\n最终结论。"
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_native_blank_messages_skip_divider(tmp_path, adapter, provider_cls):
    """整条空白的消息被丢弃:最终输出与实时 text 事件都没有空横线段。"""
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(
            _config(tmp_path, adapter, "BLANK_MESSAGES", {}, events))
        assert result.success
        assert result.output == "先说明进度。\n\n---\n\n最终结论。"
        streamed = "".join(text for kind, text in events if kind == "text")
        assert streamed == result.output
    finally:
        provider.shutdown()


def test_output_assembler_drops_blank_messages():
    """空白消息不产生输出或横线;实际内容之间正常补分隔。"""
    from missioncrew.runtime.native import MESSAGE_DIVIDER, OutputAssembler
    assembler = OutputAssembler()
    assert assembler.append(" \n") == ""         # 开头就是纯空白消息
    assembler.finish_message()
    assert assembler.append("第一段") == "第一段"
    assembler.finish_message()
    assert assembler.append("\t \n") == ""       # 中间的纯空白消息
    assembler.finish_message()
    assert assembler.append("结论") == MESSAGE_DIVIDER + "结论"
    assembler.finish_message()
    assert assembler.text == f"第一段{MESSAGE_DIVIDER}结论"


def test_output_assembler_keeps_leading_whitespace_of_real_message():
    """流式前导空白先扣住,出现实际内容时随横线一起写出,不丢正文。"""
    from missioncrew.runtime.native import MESSAGE_DIVIDER, OutputAssembler
    assembler = OutputAssembler()
    assembler.append("进度")
    assembler.finish_message()
    assert assembler.append("\n") == ""
    assert assembler.append("结论") == MESSAGE_DIVIDER + "\n结论"
    assert assembler.append("。") == "。"
    assembler.finish_message()
    assert assembler.text == f"进度{MESSAGE_DIVIDER}\n结论。"


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_native_execution_accepts_no_deadline(tmp_path, adapter, provider_cls):
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    config = _config(tmp_path, adapter, "NO DEADLINE", {}, [])
    config.timeout = None
    try:
        result = provider.start(config)
        assert result.success
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_native_provider_restarts_changed_process_and_resumes_session(
        tmp_path, adapter, provider_cls):
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    saved: dict = {}
    try:
        first = provider.start(_config(tmp_path, adapter, "FIRST", saved, []))
        changed = _config(tmp_path, adapter, "SECOND", saved, [])
        changed.env["RUNTIME_CONFIG_CHANGED"] = "1"
        second = provider.start(changed)
        assert first.success and second.success
        launches = (tmp_path / f"{adapter}.launches").read_text().splitlines()
        assert len(launches) == 2
        assert saved["id"].startswith("claude-session" if adapter == "claude_code"
                                      else "codex-thread")
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_missioncrew_auto_permission_approves_without_native_bypass(
        tmp_path, adapter, provider_cls):
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    saved: dict = {}
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(_config(
            tmp_path, adapter, "ASK_PERMISSION", saved, events,
            interact=lambda *_: pytest.fail("YOLO 不应等待用户")))
        assert result.success
        assert '"accept"' in result.output or '"behavior": "allow"' in result.output
        audits = [json.loads(text) for kind, text in events
                  if kind == "permission_request"]
        assert audits and audits[-1]["status"] == "auto_approved"
    finally:
        provider.shutdown()


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_agent_user_question_round_trips_to_same_turn(
        tmp_path, adapter, provider_cls):
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    saved: dict = {}
    events: list[tuple[str, str]] = []
    requests = []

    def interact(kind, payload):
        requests.append((kind, payload))
        question_id = str(payload["questions"][0]["id"])
        return {"decision": "submit", "answers": {question_id: ["B"]}}

    try:
        result = provider.start(_config(
            tmp_path, adapter, "ASK_USER", saved, events, interact=interact))
        assert result.success and "B" in result.output
        assert requests[0][0] == "user_input_request"
        assert requests[0][1]["questions"][0]["question"] == "Which option?"
    finally:
        provider.shutdown()


def test_claude_session_approval_echoes_native_permission_suggestion(tmp_path):
    provider = ClaudeRuntimeProvider(_Fallback(), _fake_command("claude_code"))
    requests = []

    def interact(kind, payload):
        requests.append((kind, payload))
        return {"decision": "approve_session", "answers": {}}

    try:
        result = provider.start(_config(
            tmp_path, "claude_code", "ASK_PERMISSION", {}, [],
            approval="prompt", interact=interact))
        assert result.success and "updatedPermissions" in result.output
        assert requests[0][1]["can_approve_session"] is True
    finally:
        provider.shutdown()


@pytest.mark.parametrize("prompt", ["BACKGROUND_AGENT", "BACKGROUND_AGENT_LEGACY"])
def test_claude_waits_for_background_agent_and_keeps_turn_policy_alive(
        tmp_path, prompt):
    provider = ClaudeRuntimeProvider(_Fallback(), _fake_command("claude_code"))
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(_config(
            tmp_path, "claude_code", prompt, {}, events,
            interact=lambda *_: pytest.fail("auto 审批不应等待用户")))

        assert result.success
        assert result.output == "claude final after background"
        lifecycle = [json.loads(text) for kind, text in events
                     if kind == "backend_agent"]
        statuses = [event["status"] for event in lifecycle]
        assert statuses[0] == "running"
        assert "waiting" in statuses and "progress" in statuses
        assert "completed" in statuses
        waiting = next(event for event in lifecycle if event["status"] == "waiting")
        assert waiting["pending"] == 1
        # 后台 Agent 的 usage 也在源头归一:累计 total_tokens 与 tool_uses/duration_ms
        progress = next(event for event in lifecycle if event["status"] == "progress")
        assert progress["usage"]["schema"] == "usage/v1"
        assert progress["usage"]["total"] == {"total": 12}
        assert (progress["usage"]["tool_uses"], progress["usage"]["duration_ms"]) == (2, 50)
        completed = next(event for event in lifecycle if event["status"] == "completed")
        assert completed["usage"]["total"] == {"total": 15}
        assert completed["usage"]["raw"] == {"total_tokens": 15, "tool_uses": 2, "duration_ms": 55}
        assert sum(event["status"] == "waiting" for event in lifecycle) == 2
        assert any(kind == "permission_request"
                   and json.loads(text)["status"] == "auto_approved"
                   for kind, text in events)
    finally:
        provider.shutdown()


def test_codex_resume_failure_is_explicit_and_does_not_start_new_thread(tmp_path):
    fallback = _Fallback()
    provider = CodexRuntimeProvider(fallback, _fake_command("codex"))
    saved = {"id": "missing-thread", "context": "v1"}
    try:
        result = provider.start(_config(
            tmp_path, "codex", "SECOND", saved, []))
        assert not result.success
        assert "无法恢复" in result.summary and "未创建新会话" in result.summary
        assert saved["id"] == ""
        assert not fallback.started
    finally:
        provider.shutdown()


def test_claude_resume_failure_is_explicit_and_does_not_start_new_session(tmp_path):
    fallback = _Fallback()
    provider = ClaudeRuntimeProvider(fallback, _fake_command("claude_code"))
    saved = {"id": "missing-session", "context": "v1"}
    try:
        result = provider.start(_config(
            tmp_path, "claude_code", "SECOND", saved, []))
        assert not result.success
        assert "无法恢复" in result.summary and "未创建新会话" in result.summary
        assert saved["id"] == ""
        assert not fallback.started
    finally:
        provider.shutdown()


@pytest.mark.parametrize("prompt,policy", [
    ("ASK_WRITE", {"filesystem": "read-only"}),
    ("ASK_NETWORK", {"network": "deny"}),
])
def test_claude_auto_approval_still_enforces_hard_policy(
        tmp_path, prompt, policy):
    provider = ClaudeRuntimeProvider(_Fallback(), _fake_command("claude_code"))
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(_config(
            tmp_path, "claude_code", prompt, {}, events, **policy,
            interact=lambda *_: pytest.fail("硬策略拒绝不应等待用户")))
        assert result.success and '"behavior": "deny"' in result.output
        audits = [json.loads(text) for kind, text in events
                  if kind == "permission_request"]
        assert audits[-1]["status"] == "denied"
        assert audits[-1]["reason"]
    finally:
        provider.shutdown()


def test_codex_sandbox_network_allowed_unless_denied(tmp_path):
    # 本地协作平台的 Agent Tool 回环 API 与 git 操作都依赖网络:
    # inherit(默认)放行,显式 deny 才禁网。
    from missioncrew.runtime.codex import _sandbox_policy
    events: list[tuple[str, str]] = []
    default = _sandbox_policy(_config(tmp_path, "codex", "任务", {}, events))
    assert default["type"] == "workspaceWrite"
    assert default["networkAccess"] is True
    denied = _sandbox_policy(
        _config(tmp_path, "codex", "任务", {}, events, network="deny"))
    assert denied["networkAccess"] is False
    read_only = _sandbox_policy(
        _config(tmp_path, "codex", "任务", {}, events,
                filesystem="read-only"))
    assert read_only == {"type": "readOnly", "networkAccess": True}


def test_claude_os_sandbox_default_off_and_opt_in(tmp_path, monkeypatch):
    # OS 沙箱默认关闭(其网络命名空间连宿主回环都不可达,对本地协作限制过强);
    # MISSIONCREW_CLAUDE_SANDBOX=on 显式启用时,Agent Tool CLI 必须列入
    # excludedCommands 在沙箱外执行才能连上控制面(实测 allowedDomains 无效),
    # 两种文档调用形式(missioncrew-tool 与 python -m)都要覆盖。
    events: list[tuple[str, str]] = []
    monkeypatch.delenv("MISSIONCREW_CLAUDE_SANDBOX", raising=False)
    cfg = _config(tmp_path, "claude_code", "任务", {}, events)
    assert _sandbox_settings(cfg) == {"sandbox": {"enabled": False}}

    monkeypatch.setenv("MISSIONCREW_CLAUDE_SANDBOX", "on")
    sandbox = _sandbox_settings(cfg)["sandbox"]
    assert sandbox["enabled"] is True
    assert "* -m missioncrew.agent_tool *" in sandbox["excludedCommands"]
    assert "missioncrew-tool *" in sandbox["excludedCommands"]
    assert "network" not in sandbox

    full = _config(tmp_path, "claude_code", "任务", {}, events,
                   filesystem="full-access")
    assert _sandbox_settings(full) == {"sandbox": {"enabled": False}}


def test_codex_model_catalog_comes_from_app_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = CodexRuntimeProvider(_Fallback(), _fake_command("codex"))
    backend = Backend(id="codex", name="Codex", adapter="codex")
    assert provider.list_models(backend) == ["gpt-test"]


def test_claude_background_bash_tracked_and_wake_turn_dispatched(tmp_path):
    """后台命令跨 turn 存活；空闲时的自唤醒 turn 缓冲后交给 wake handler。"""
    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "b-claude", "general::dev2", str(tmp_path), persistent=True)
    session.last_project_id = "webshop"
    session.last_role_id = "dev2"
    woken: list[dict] = []
    claude_mod.set_wake_handler(woken.append)
    try:
        # turn 内启动后台命令并结束 turn：不阻塞 result,任务仍被跟踪
        session._handle_message({
            "type": "system", "subtype": "task_started", "task_id": "bg1",
            "task_type": "local_bash", "description": "sleep 25 && deploy"})
        assert "bg1" in session._background_tasks
        assert session.snapshot().background_tasks == 1
        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "started"})
        assert session._result_ready.is_set()

        # 任务结束(空闲):记为唤醒原因,随后 CLI 自发开启汇报 turn
        session._result_ready.clear()
        session._handle_message({
            "type": "system", "subtype": "task_notification", "task_id": "bg1",
            "status": "completed", "summary": "exit 0"})
        assert session._background_tasks == {}
        session._handle_message({
            "type": "system", "subtype": "init", "session_id": "native-2"})
        session._handle_message({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "部署已完成，服务健康。"}]}})
        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "部署已完成，服务健康。"})

        deadline = time.time() + 5
        while not woken and time.time() < deadline:
            time.sleep(0.01)
        assert woken, "wake handler 未被调用"
        payload = woken[0]
        assert payload["session_key"] == "general::dev2"
        assert payload["role_id"] == "dev2"
        assert payload["output"] == "部署已完成，服务健康。"
        assert [task["task_id"] for task in payload["tasks"]] == ["bg1"]
        assert any(kind == "text" for kind, _ in payload["events"])
        # 自唤醒 turn 不污染运行 turn 的 result 状态机
        assert not session._result_ready.is_set()
        assert session._wake_sink is None
    finally:
        claude_mod.set_wake_handler(None)


def test_claude_waits_for_wake_turn_before_starting_channel_turn(
        tmp_path, monkeypatch):
    """旧 wake turn 未收尾时，新 run 不得把输入和 result 混入 wake sink。"""
    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "claude_code", "channel::expert", str(tmp_path),
        persistent=True)
    session.session_id = "native-wake"
    session._handle_message({
        "type": "system", "subtype": "init", "session_id": "native-wake"})
    assert session._wake_sink is not None

    process_started = threading.Event()
    prompt_written = threading.Event()
    monkeypatch.setattr(
        session, "_ensure_process", lambda _config: process_started.set())
    monkeypatch.setattr(
        session, "_write", lambda _payload: prompt_written.set())
    result = {}
    config = _config(
        tmp_path, "claude_code", "NEW TURN",
        {"id": "native-wake", "context": "v1"}, [])
    config.session_key = "channel::expert"
    worker = threading.Thread(
        target=lambda: result.setdefault("value", session.run(config)))
    worker.start()
    try:
        assert not process_started.wait(0.2)

        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": ""})
        assert process_started.wait(2)
        assert prompt_written.wait(2)

        session._handle_message({
            "type": "assistant", "message": {"content": [
                {"type": "text", "text": "channel completed"}]}})
        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "channel completed"})
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result["value"].success
        assert result["value"].output == "channel completed"
    finally:
        session.close()
        worker.join(timeout=2)


def test_claude_background_task_finishing_inside_turn_is_not_wake_reason(tmp_path):
    """运行 turn 内结束的后台任务由 CLI 直接喂给当前回合,不再触发唤醒。"""
    from types import SimpleNamespace

    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "b-claude", "general::dev2", str(tmp_path), persistent=True)
    session._handle_message({
        "type": "system", "subtype": "task_started", "task_id": "bg2",
        "task_type": "local_bash", "description": "quick job"})
    session._active_config = SimpleNamespace(emit=None)
    try:
        session._handle_message({
            "type": "system", "subtype": "task_notification", "task_id": "bg2",
            "status": "completed", "summary": ""})
        assert session._wake_reasons == []
        assert session._background_tasks == {}
    finally:
        session._active_config = None


def test_claude_background_task_records_origin_trigger(tmp_path):
    """任务在某轮运行中启动:记录该轮触发消息,唤醒时继承派发语义。"""
    from types import SimpleNamespace

    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "b-claude", "general::dev", str(tmp_path), persistent=True)
    session._active_config = SimpleNamespace(emit=None, trigger_message_id=42)
    try:
        session._handle_message({
            "type": "system", "subtype": "task_started", "task_id": "bg9",
            "task_type": "local_bash", "description": "deploy"})
        assert session._background_tasks["bg9"]["origin_trigger"] == 42
    finally:
        session._active_config = None
    session._handle_message({
        "type": "system", "subtype": "task_notification", "task_id": "bg9",
        "status": "completed", "summary": ""})
    assert session._wake_reasons[0]["origin_trigger"] == 42


def test_claude_resume_replay_does_not_end_turn(tmp_path):
    """--resume 启动回放的 stopped 通知与空 init/result 不结束本轮;
    真正的 result 到达后正常收口,过程流里能看到两条说明。"""
    provider = ClaudeRuntimeProvider(_Fallback(), _fake_command("claude_code"))
    events: list[tuple[str, str]] = []
    saved = {"id": "stale-replay-session", "context": "v1"}
    try:
        result = provider.start(
            _config(tmp_path, "claude_code", "AFTER REPLAY", saved, events))
        assert result.success
        assert result.output == "claude answer 1"
        statuses = [text for kind, text in events if kind == "status"]
        assert any("遗留的后台任务 orphan-1" in text and "stopped" in text
                   for text in statuses)
        assert any("跳过 Claude 启动回放的空 result" in text for text in statuses)
        # 回放的空 result 不计入用量;只有真正那轮的 usage
        assert sum(1 for kind, _ in events if kind == "usage") == 1
    finally:
        provider.shutdown()


def _run_session_in_thread(session, tmp_path, monkeypatch, prompt="TURN"):
    """在线程里跑 session.run,进程与写入都打桩,由测试直接喂事件。"""
    monkeypatch.setattr(session, "_ensure_process", lambda _config: None)
    monkeypatch.setattr(session, "_write", lambda _payload: None)
    events: list[tuple[str, str]] = []
    config = _config(tmp_path, "claude_code", prompt, {}, events)
    config.session_key = session.session_key
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("value", session.run(config)))
    worker.start()
    deadline = time.time() + 2
    while session._active_config is None and time.time() < deadline:
        time.sleep(0.01)
    assert session._active_config is not None
    return worker, result, events


def test_claude_zero_turn_result_without_activity_is_skipped(
        tmp_path, monkeypatch):
    """兜底:即使没收到回放通知,本轮尚无模型活动时的零轮次成功 result
    也不结束 turn;有活动后的零轮次 result 才被当作结果。"""
    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "claude_code", "channel::dev2", str(tmp_path), persistent=True)
    worker, result, _events = _run_session_in_thread(session, tmp_path, monkeypatch)
    try:
        session._handle_message({
            "type": "system", "subtype": "init", "session_id": "native-1"})
        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "", "num_turns": 0})
        worker.join(timeout=0.3)
        assert worker.is_alive(), "零轮次空 result 不应结束本轮"
        assert not session._result_ready.is_set()

        session._handle_message({
            "type": "system", "subtype": "init", "session_id": "native-1"})
        session._handle_message({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "real answer"}]}})
        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "real answer", "num_turns": 1})
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result["value"].success
        assert result["value"].output == "real answer"
    finally:
        session.close()
        worker.join(timeout=2)


def test_claude_zero_turn_result_after_interrupt_ends_turn(
        tmp_path, monkeypatch):
    """中断后的零轮次 result 必须照常收口,否则中断会拖到超时。"""
    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "claude_code", "channel::dev2", str(tmp_path), persistent=True)
    worker, result, _events = _run_session_in_thread(session, tmp_path, monkeypatch)
    try:
        session._handle_message({
            "type": "system", "subtype": "init", "session_id": "native-1"})
        with pytest.raises(TimeoutError):   # _write 已打桩,收不到应答
            session.interrupt(timeout=0.05)
        session._handle_message({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "", "num_turns": 0})
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result["value"].success
        assert result["value"].output == ""
    finally:
        session.close()
        worker.join(timeout=2)


def test_claude_zero_turn_error_result_still_ends_turn(tmp_path, monkeypatch):
    """错误 result 不受回放守卫影响:零轮次也立即结束本轮并报错。"""
    from missioncrew.runtime import claude as claude_mod
    session = claude_mod._ClaudeSession(
        ["claude"], "claude_code", "channel::dev2", str(tmp_path), persistent=True)
    worker, result, _events = _run_session_in_thread(session, tmp_path, monkeypatch)
    try:
        session._handle_message({
            "type": "system", "subtype": "task_notification",
            "task_id": "orphan-9", "status": "stopped"})
        session._handle_message({
            "type": "system", "subtype": "init", "session_id": "native-1"})
        session._handle_message({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "error": "boom", "num_turns": 0})
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert not result["value"].success
        assert "boom" in result["value"].summary
    finally:
        session.close()
        worker.join(timeout=2)


@pytest.mark.parametrize("adapter,provider_cls", [
    ("claude_code", ClaudeRuntimeProvider),
    ("codex", CodexRuntimeProvider),
])
def test_empty_success_turn_keeps_summary_blank(tmp_path, adapter, provider_cls):
    """回归:成功但零输出的回合,summary 不得落到 "success" 等固定文案——
    否则会被聊天层当成 Agent 回复发布;保持为空让"无输出"守卫接管。"""
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    try:
        result = provider.start(_config(tmp_path, adapter, "EMPTY_TURN", {}, []))
        assert result.success
        assert result.output == ""
        assert result.summary == ""
    finally:
        provider.shutdown()
