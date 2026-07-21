"""Runtime 统一抽象层的边界与执行策略。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from missioncrew.core.models import (Backend, ExecutionConfig, RunResult,
                                     RuntimePermissions, RuntimePolicy)
from missioncrew.runtime import (RuntimeCapabilities, RuntimeManager,
                                 RuntimeExecutionInfo, RuntimeProvider)
from missioncrew.runtime import adapters


class _RecordingProvider(RuntimeProvider):
    def __init__(self):
        self.started = None
        self.stopped = None

    def start(self, config: ExecutionConfig) -> RunResult:
        self.started = config
        return RunResult(True, "ok", output="done")

    def stop(self, backend: Backend, session_key: str = "") -> int:
        self.stopped = (backend.id, session_key)
        return 2

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(session_reuse=True)

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return ["model-a", "model-b"]

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        return RuntimeExecutionInfo(mode="persistent", transport="test-rpc")


def test_manager_injects_skills_paths_and_permissions(tmp_path):
    manager = RuntimeManager()
    provider = _RecordingProvider()
    manager.register("recording", provider)
    workdir = tmp_path / "repo"
    docs = tmp_path / "docs"
    skills = tmp_path / "skills"
    for path in (workdir, docs, skills):
        path.mkdir()
    backend = Backend(id="runtime", name="Runtime", adapter="recording")
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="work",
        workdir=str(workdir),
        runtime_policy=RuntimePolicy(
            readable_paths=[str(docs)], writable_paths=[str(workdir)],
            skill_paths=[str(skills)],
            permissions=RuntimePermissions(
                approval="prompt", filesystem="read-only", network="deny"),
        ),
    )

    result = manager.start(config)

    assert result.success and provider.started is config
    assert config.allowed_dirs == [str(docs), str(workdir), str(skills)]
    assert json.loads(config.env["MISSIONCREW_READABLE_DIRS"]) == [
        str(docs), str(skills)]
    assert json.loads(config.env["MISSIONCREW_WRITABLE_DIRS"]) == [str(workdir)]
    assert json.loads(config.env["MISSIONCREW_SKILL_DIRS"]) == [str(skills)]
    assert config.env["MISSIONCREW_SKILLS_DIR"] == str(skills)
    assert json.loads(config.env["MISSIONCREW_RUNTIME_PERMISSIONS"]) == {
        "approval": "prompt", "filesystem": "read-only", "network": "deny"}
    assert manager.supports_session(backend)
    assert manager.list_models(backend) == ["model-a", "model-b"]
    assert manager.stop(backend, "channel::role") == 2
    assert provider.stopped == ("runtime", "channel::role")


def test_manager_persists_runtime_usage_history(store, tmp_path):
    manager = RuntimeManager()
    manager.bind_usage_store(store)
    manager.register("recording", _RecordingProvider())
    config = ExecutionConfig(
        task_id="task-history", stage_name="chat",
        backend=Backend(id="runtime-history", name="Runtime", adapter="recording",
                        model="model-a"),
        prompt="work", workdir=str(tmp_path), session_key="channel::role",
        effort="high",
    )

    assert manager.start(config).success

    history = store.list_runtime_usage()
    assert len(history) == 1
    expected = {
        "backend_id": "runtime-history", "adapter": "recording",
        "mode": "persistent", "transport": "test-rpc",
        "task_id": "task-history", "stage_name": "chat",
        "session_key": "channel::role", "model": "model-a", "effort": "high",
        "status": "succeeded", "success": True,
    }
    assert {key: history[0][key] for key in expected} == expected
    assert history[0]["finished_at"] >= history[0]["started_at"]
    assert history[0]["duration_seconds"] >= 0


def test_runtime_usage_reconciles_dead_owner_as_interrupted(store, monkeypatch):
    store.start_runtime_usage(
        backend_id="runtime", adapter="custom", mode="one_shot",
        transport="cli-command")

    def missing_process(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr("missioncrew.core.store.os.kill", missing_process)
    history = store.list_runtime_usage()
    assert history[0]["status"] == "interrupted"
    assert history[0]["success"] is False
    assert history[0]["finished_at"] is not None


@pytest.mark.parametrize(("adapter", "mode", "transport"), [
    ("claude_code", "persistent", "claude-stream-json"),
    ("codex", "persistent", "codex-app-server"),
    ("kimi", "persistent", "acp-stdio"),
    ("opencode", "one_shot", "cli-command"),
])
def test_builtin_providers_describe_usage_history_mode(
        tmp_path, adapter, mode, transport):
    manager = RuntimeManager()
    backend = Backend(id=adapter, name=adapter, adapter=adapter)
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="work",
        workdir=str(tmp_path), session_key="channel::role",
    )
    info = manager.provider_for(backend).execution_info(config)
    assert (info.mode, info.transport) == (mode, transport)


def test_permission_policy_is_validated_and_translated_for_codex(tmp_path):
    with pytest.raises(ValueError, match="filesystem"):
        RuntimePermissions(filesystem="host-root")

    backend = Backend(id="codex", name="Codex", adapter="codex")
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="work",
        workdir=str(tmp_path),
        runtime_policy=RuntimePolicy(permissions=RuntimePermissions(
            approval="prompt", filesystem="read-only")),
    )
    command = adapters.render_command(
        adapters.DEFAULT_COMMANDS["codex"], "work", "", allowed_dirs=[])
    translated = adapters._apply_permission_policy(command, "codex", config)
    assert translated[translated.index("--sandbox") + 1] == "read-only"


def test_explicit_readable_paths_are_not_promoted_to_writable(tmp_path):
    manager = RuntimeManager()
    provider = _RecordingProvider()
    manager.register("recording", provider)
    backend = Backend(id="runtime", name="Runtime", adapter="recording")
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="read",
        workdir=str(tmp_path),
        runtime_policy=RuntimePolicy(readable_paths=[str(tmp_path)]),
    )
    manager.start(config)
    assert json.loads(config.env["MISSIONCREW_READABLE_DIRS"]) == [str(tmp_path)]
    assert json.loads(config.env["MISSIONCREW_WRITABLE_DIRS"]) == []


def test_application_layers_do_not_import_raw_runtime_executors():
    root = Path(__file__).parents[1] / "missioncrew"
    violations = []
    for path in root.rglob("*.py"):
        if path.is_relative_to(root / "runtime"):
            continue
        text = path.read_text(encoding="utf-8")
        if ("runtime import adapters" in text
                or "runtime.adapters" in text
                or "get_adapter(" in text
                or "ACP_SERVE_COMMANDS" in text):
            violations.append(str(path.relative_to(root)))
    assert violations == []


def test_builtin_cli_stop_terminates_tracked_process(tmp_path):
    backend = Backend(id="cli-stop", name="CLI", adapter="custom")
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="work",
        workdir=str(tmp_path), session_key="channel::role")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True)
    try:
        adapters._track_process(config, process)
        manager = RuntimeManager()
        status = manager.status([backend])
        assert status["summary"]["one_shot"] == 1
        assert status["backends"][0]["state"] == "running"
        instance = status["instances"][0]
        assert instance["transport"] == "cli-command"
        assert instance["mode"] == "one_shot"
        assert instance["pid"] == process.pid
        assert manager.stop(backend, "channel::role") == 1
        assert process.poll() is not None
        assert manager.status([backend])["summary"]["live_instances"] == 0
    finally:
        if process.poll() is None:
            process.kill()
