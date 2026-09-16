"""Antigravity 的真实子进程协议契约,不依赖账号或外网。"""
import json
import subprocess
import sys
import threading
import time

import pytest

from missioncrew.core.models import Backend, ExecutionConfig
from missioncrew.runtime import adapters
from missioncrew.runtime.antigravity import AntigravityRuntimeProvider
from missioncrew.runtime.clis.antigravity import discover_models
from missioncrew.runtime.manager import RuntimeManager


@pytest.fixture
def runtime(tmp_path):
    binary = tmp_path / "agy"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ["models"]:
    print("Fetching available models...")
    print("gemini-test\tGemini Test")
    print("custom/model\tCustom Model")
    print("gemini-test\tGemini Test")
    raise SystemExit(0)
with open(os.environ["LAUNCH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
scenario = os.environ.get("SCENARIO", "success")
sid = args[args.index("--conversation") + 1] if "--conversation" in args else "agy-conversation"
if scenario == "mismatch":
    sid = "replacement-conversation"
def emit(event, payload, **extra):
    print(json.dumps({"event": event, event: payload, **extra}), flush=True)
emit("init", {"cwd": os.getcwd()}, conversation_id=sid)
if scenario == "wait":
    time.sleep(30)
if scenario == "missing":
    print("conversation not found", file=sys.stderr)
    raise SystemExit(1)
if scenario == "truncated":
    raise SystemExit(0)
print("startup diagnostic", flush=True)
print("[]", flush=True)
emit("step_update", {"step_index": 1, "step_type": "tool", "state": "ACTIVE", "tool_name": "run_command"})
emit("step_update", {"step_index": 1, "step_type": "tool", "state": "DONE", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "echo ok"}, "output": "ok"}})
if scenario == "tool_error":
    emit("step_update", {"step_index": 3, "step_type": "tool", "state": "ERROR", "tool_name": "run_command", "tool_info": {"error": {"type": "TOOL_ERROR", "message": "sandbox unavailable"}}})
emit("step_update", {"step_index": 2, "step_type": "agent_response", "state": "ACTIVE", "text_delta": "answer "})
emit("step_update", {"step_index": 2, "step_type": "agent_response", "state": "DONE", "text_delta": "complete", "usage": {"input_tokens": 10, "output_tokens": 2, "thinking_tokens": 1, "cache_read_tokens": 5, "total_tokens": 12}})
result = {"conversation_id": sid, "status": "SUCCESS", "response": "answer complete", "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, "duration_seconds": 0.25}
if scenario == "error":
    result.update(status="ERROR", error="invalid model")
if scenario == "denied":
    result["denied_actions"] = [{"tool": "run_command"}]
if scenario == "empty":
    result["response"] = ""
if scenario == "timeout_result":
    print("[agy] print timeout after 1s with turn in progress; returning partial output", file=sys.stderr)
emit("result", result)
''')
    binary.chmod(0o755)
    resource = tmp_path.parent / f"{tmp_path.name} resource with spaces"
    resource.mkdir(exist_ok=True)
    saved = {}
    events = []

    def save(sid, context, **stats):
        saved.update(id=sid, context=context, **stats)

    config = ExecutionConfig(
        task_id="chat", stage_name="chat",
        backend=Backend(id="agy", name="Antigravity", adapter="antigravity",
                        binary_path=str(binary), model="gemini-test"),
        prompt="FIRST", workdir=str(tmp_path), timeout=5,
        common_prompt="COMMON", turn_prompt="FIRST", recovery_prompt="RECENT FIRST",
        session_key=f"channel::{tmp_path.name}", context_version="v1",
        load_session=lambda: (saved.get("id", ""), saved.get("context", ""), False),
        save_session=save, emit=lambda kind, text: events.append((kind, text)),
        env={"LAUNCH_LOG": str(tmp_path / "launches")},
        effort="high", allowed_dirs=[str(resource)],
    )
    provider = AntigravityRuntimeProvider()
    yield provider, config, saved, events
    provider.shutdown()


def test_detection_models_and_provider_registration(runtime, monkeypatch):
    provider, config, _, _ = runtime
    monkeypatch.setattr(adapters.shutil, "which", lambda name: config.backend.binary_path if name == "agy" else None)
    report = next(item for item in adapters.detect_report(False) if item["adapter"] == "antigravity")
    backend = adapters.detect_backends([report])[0]
    assert backend.adapter == "antigravity" and backend.binary_path == config.backend.binary_path
    manager = RuntimeManager()
    assert isinstance(manager.provider_for(backend), AntigravityRuntimeProvider)
    assert manager.list_models(backend) == ["gemini-test", "custom/model"]
    assert provider.effort_support(backend) == ["low", "medium", "high"]
    assert adapters.update_plan(backend) == ("self", ["agy", "update"])
    caps = provider.capabilities(backend)
    assert caps.session_reuse and caps.structured_events
    assert not caps.permission_control and not caps.user_interaction


def test_resume_context_events_and_cumulative_usage(runtime, tmp_path):
    provider, config, saved, events = runtime
    assert provider.start(config).success
    assert saved["id"] == "agy-conversation"
    config.turn_prompt = "SECOND"
    result = provider.start(config)
    assert result.success and result.output == "answer complete"
    launches = [json.loads(line) for line in (tmp_path / "launches").read_text().splitlines()]
    assert "--conversation" not in launches[0]
    assert launches[0][-1] == "COMMON\nRECENT FIRST"
    assert launches[1][launches[1].index("--conversation") + 1] == saved["id"]
    assert "COMMON" not in launches[1][-1] and "SECOND" in launches[1][-1]
    dirs = [launches[0][i + 1] for i, token in enumerate(launches[0]) if token == "--add-dir"]
    assert dirs == [config.workdir, config.allowed_dirs[0]]
    assert "--model" in launches[0] and "--effort" in launches[0]
    assert saved["turn_mode"] == "lean"
    assert {"text", "tool", "tool_result", "usage"} <= {kind for kind, _ in events}
    usage = [json.loads(text) for kind, text in events if kind == "usage"][-1]
    assert usage["schema"] == "usage/v1" and usage["duration_ms"] == 250
    assert usage["turn"] == {"input": 10, "output": 2, "reasoning": 1, "cache_read": 5, "total": 12}
    assert usage["total"] == {"input": 100, "output": 20, "total": 120}
    assert provider.instances(config.backend) == []


@pytest.mark.parametrize("scenario,detail", [
    ("error", "invalid model"), ("truncated", "missing result"),
    ("denied", "被拒绝"), ("empty", "未产生"),
    ("timeout_result", "print timeout"),
])
def test_failures_never_publish_partial_success(runtime, scenario, detail):
    provider, config, _, _ = runtime
    config.env["SCENARIO"] = scenario
    result = provider.start(config)
    assert not result.success and detail in result.summary and detail in result.output


@pytest.mark.parametrize("scenario", ["missing", "mismatch"])
def test_missing_conversation_fails_without_silent_retry(runtime, tmp_path, scenario):
    provider, config, saved, _ = runtime
    saved.update(id="old-conversation", context="v1")
    config.env["SCENARIO"] = scenario
    assert not provider.start(config).success
    assert saved["id"] == ""
    assert len((tmp_path / "launches").read_text().splitlines()) == 1


@pytest.mark.parametrize("field,value", [
    ("approval", "prompt"), ("approval", "deny"),
    ("filesystem", "read-only"), ("network", "deny"),
])
def test_unsupported_policy_does_not_launch(runtime, tmp_path, field, value):
    provider, config, _, _ = runtime
    setattr(config.runtime_policy.permissions, field, value)
    assert not provider.start(config).success
    assert not (tmp_path / "launches").exists()


def test_command_defaults_and_optional_parameters(runtime):
    provider, config, _, _ = runtime
    config.timeout = None
    config.backend.model = ""
    config.effort = ""
    command = provider._command(config, "hello")
    assert "--model" not in command and "--effort" not in command
    assert command[command.index("--print-timeout") + 1] == "2562047h"
    assert "--sandbox" in command
    config.runtime_policy.permissions.filesystem = "full-access"
    assert "--sandbox=false" in provider._command(config, "hello")


def test_stop_preserves_conversation_saved_before_completion(runtime):
    provider, config, saved, _ = runtime
    config.env["SCENARIO"] = "wait"
    results = []
    worker = threading.Thread(target=lambda: results.append(provider.start(config)))
    worker.start()
    deadline = time.monotonic() + 3
    while not saved and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert saved["id"] == "agy-conversation"
        assert provider.instances(config.backend)[0].transport == "antigravity-stream-json"
        assert provider.stop(config.backend, config.session_key) == 1
    finally:
        provider.shutdown()
        worker.join(timeout=6)
    assert not worker.is_alive() and not results[0].success
    assert saved["id"] == "agy-conversation"


def test_timeout_cleans_up_process(runtime):
    provider, config, _, _ = runtime
    config.env["SCENARIO"] = "wait"
    config.timeout = 0.1
    result = provider.start(config)
    assert not result.success and "超时" in result.summary
    assert not provider.instances(config.backend)


def test_tool_errors_are_visible_in_event_stream(runtime):
    provider, config, _, events = runtime
    config.env["SCENARIO"] = "tool_error"
    assert provider.start(config).success
    results = [json.loads(text) for kind, text in events if kind == "tool_result"]
    assert results[-1]["error"]["message"] == "sandbox unavailable"


def test_event_sink_failure_does_not_break_output_drain(runtime):
    provider, config, _, _ = runtime

    def fail(*args):
        raise RuntimeError("event storage unavailable")

    config.emit = fail
    result = provider.start(config)
    assert result.success and result.output == "answer complete"


def test_cancelled_queued_turn_does_not_launch(runtime, tmp_path):
    provider, config, _, _ = runtime
    cancelled = threading.Event()
    config.cancelled = cancelled.is_set
    results = []
    with adapters._named_session_lock(config.session_key):
        worker = threading.Thread(target=lambda: results.append(provider.start(config)))
        worker.start()
        cancelled.set()
    worker.join(timeout=3)
    assert not worker.is_alive() and not results[0].success
    assert not (tmp_path / "launches").exists()


@pytest.mark.parametrize("failure", [OSError("missing"), subprocess.TimeoutExpired("agy", 1)])
def test_model_discovery_failure_returns_empty(runtime, monkeypatch, failure):
    _, config, _, _ = runtime

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    assert discover_models(config.backend) == ([], {})
