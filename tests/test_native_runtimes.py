"""Claude/Codex 原生双向协议 provider。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from missioncrew.core.models import (Backend, ExecutionConfig, RunResult,
                                     RuntimePermissions, RuntimePolicy)
from missioncrew.runtime.base import RuntimeCapabilities, RuntimeProvider
from missioncrew.runtime.claude import ClaudeRuntimeProvider
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
        assert {"status", "thinking", "tool", "text"} <= kinds
        if adapter == "codex":
            assert {"tool_result", "usage"} <= kinds
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
def test_native_multiple_messages_join_with_newlines(
        tmp_path, adapter, provider_cls):
    """同一轮的多条完整消息在结果里按行分隔,不拼在同一行。"""
    provider = provider_cls(_Fallback(), _fake_command(adapter))
    try:
        result = provider.start(
            _config(tmp_path, adapter, "TWO_MESSAGES", {}, []))
        assert result.success
        assert result.output == "先说明进度。\n最终结论。"
    finally:
        provider.shutdown()


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


def test_codex_model_catalog_comes_from_app_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = CodexRuntimeProvider(_Fallback(), _fake_command("codex"))
    backend = Backend(id="codex", name="Codex", adapter="codex")
    assert provider.list_models(backend) == ["gpt-test"]
