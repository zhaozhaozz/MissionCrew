"""Runtime 统一抽象层的边界与执行策略。"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from missioncrew.core.models import (Backend, ExecutionConfig, RunResult,
                                     RuntimePermissions, RuntimePolicy)
from missioncrew.runtime import (RuntimeCapabilities, RuntimeManager,
                                 RuntimeExecutionInfo, RuntimeProvider)
from missioncrew.runtime import adapters
from missioncrew.runtime.base import system_temp_dirs


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


def test_effort_support_is_declared_per_provider():
    """档位声明随 provider 走:原生 provider 各自声明,内置执行器只管自己的
    adapter,manager 合并成 /api/traits 的全量目录;注册自定义 provider 即接管
    对应 adapter 的档位,与 provider_for 的路由规则一致。"""
    from missioncrew.runtime.claude import ClaudeRuntimeProvider
    from missioncrew.runtime.codex import CodexRuntimeProvider
    from missioncrew.runtime.pi import PiRuntimeProvider

    manager = RuntimeManager()
    # 原生 provider 的档位不再进 adapters 的静态表
    assert set(adapters.EFFORT_SUPPORT) == {"grok_build", "copilot", "kimi", "mock"}
    builtin = object.__new__(ClaudeRuntimeProvider)  # 只查声明,无需构造会话
    assert builtin.effort_catalog() == {
        "claude_code": ["low", "medium", "high", "xhigh", "max"]}
    assert CodexRuntimeProvider.effort_catalog(
        object.__new__(CodexRuntimeProvider))["codex"][0] == "minimal"
    assert PiRuntimeProvider.effort_catalog(
        object.__new__(PiRuntimeProvider))["pi"][0] == "off"

    # manager 合并后对外形状不变(adapter -> 档位)
    catalog = manager.effort_catalog()
    assert set(catalog) == {"claude_code", "codex", "pi", "grok_build",
                            "copilot", "kimi", "mock"}

    # effort_options 经 provider_for 路由;未声明档位的 provider 默认不支持
    class _NoEffortProvider(_RecordingProvider):
        pass

    class _CustomEffortProvider(_RecordingProvider):
        def effort_catalog(self):
            return {"custom": ["gentle", "fierce"]}

    manager.register("custom", _NoEffortProvider())
    backend = Backend(id="c", name="c", adapter="custom")
    assert manager.effort_options(backend) == []
    manager.register("custom", _CustomEffortProvider())
    assert manager.effort_options(backend) == ["gentle", "fierce"]
    assert manager.effort_catalog()["custom"] == ["gentle", "fierce"]


def test_manager_persists_runtime_usage_history(store, tmp_path):
    manager = RuntimeManager()
    manager.bind_usage_store(store)
    manager.register("recording", _RecordingProvider())
    config = ExecutionConfig(
        task_id="task-history", stage_name="chat",
        backend=Backend(id="runtime-history", name="Runtime", adapter="recording",
                        model="model-a"),
        prompt="work", workdir=str(tmp_path), project_id="project-a", role_id="lead",
        session_key="channel::role",
        effort="high",
    )

    assert manager.start(config).success

    history = store.list_runtime_usage()
    assert len(history) == 1
    expected = {
        "backend_id": "runtime-history", "adapter": "recording",
        "mode": "persistent", "transport": "test-rpc",
        "task_id": "task-history", "stage_name": "chat",
        "project_id": "project-a", "role_id": "lead",
        "session_key": "channel::role", "model": "model-a", "effort": "high",
        "status": "succeeded", "success": True,
    }
    assert {key: history[0][key] for key in expected} == expected
    assert history[0]["finished_at"] >= history[0]["started_at"]
    assert history[0]["duration_seconds"] >= 0


def test_cancelled_runtime_usage_is_recorded_as_interrupted(store, tmp_path):
    cancelled = False

    class CancellingProvider(_RecordingProvider):
        def start(self, config: ExecutionConfig) -> RunResult:
            nonlocal cancelled
            cancelled = True
            return RunResult(False, "执行已停止")

    manager = RuntimeManager()
    manager.bind_usage_store(store)
    manager.register("cancelling", CancellingProvider())
    config = ExecutionConfig(
        task_id="cancelled", stage_name="chat",
        backend=Backend(id="cancelled-runtime", name="Runtime",
                        adapter="cancelling"),
        prompt="work", workdir=str(tmp_path),
        cancelled=lambda: cancelled,
    )

    result = manager.start(config)

    assert not result.success
    history = store.list_runtime_usage()
    assert history[0]["status"] == "interrupted"
    assert history[0]["success"] is False


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


def test_existing_runtime_usage_table_gets_project_and_role_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE runtime_usage (
      id INTEGER PRIMARY KEY AUTOINCREMENT, backend_id TEXT NOT NULL,
      adapter TEXT NOT NULL, mode TEXT NOT NULL, transport TEXT NOT NULL,
      task_id TEXT DEFAULT '', stage_name TEXT DEFAULT '', session_key TEXT DEFAULT '',
      model TEXT DEFAULT '', effort TEXT DEFAULT '', workdir TEXT DEFAULT '',
      status TEXT NOT NULL DEFAULT 'running', success INTEGER, summary TEXT DEFAULT '',
      owner_pid INTEGER NOT NULL, started_at REAL NOT NULL, finished_at REAL
    )""")
    connection.commit()
    connection.close()

    from missioncrew.core.store import Store
    legacy = Store(path)
    columns = {row["name"] for row in legacy._query(
        "PRAGMA table_info(runtime_usage)")}
    assert {"project_id", "role_id"} <= columns


def test_legacy_backend_command_is_removed_from_storage(tmp_path):
    from missioncrew.core.store import Store

    path = tmp_path / "legacy-backend.sqlite3"
    legacy = Store(path)
    legacy._put("backends", "legacy", {
        "id": "legacy",
        "name": "Legacy",
        "adapter": "codex",
        "command": ["custom-codex", "{prompt}"],
    })

    migrated = Store(path)

    assert migrated.get_backend("legacy") == Backend(
        id="legacy", name="Legacy", adapter="codex")
    assert "command" not in migrated._get("backends", "legacy")
    assert migrated._migrate_backend_commands() == 0


@pytest.mark.parametrize(("adapter", "mode", "transport"), [
    ("claude_code", "persistent", "claude-stream-json"),
    ("codex", "persistent", "codex-app-server"),
    ("grok_build", "persistent", "acp-stdio"),
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
    # workspace-write 默认追加系统临时目录,但显式只读路径不得被提升为可写
    writable = json.loads(config.env["MISSIONCREW_WRITABLE_DIRS"])
    assert writable == system_temp_dirs()
    assert str(tmp_path) not in writable


def test_prepare_appends_temp_and_private_dirs_for_workspace_write(
        tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    manager = RuntimeManager()
    provider = _RecordingProvider()
    manager.register("codex", provider)
    backend = Backend(id="codex", name="Codex", adapter="codex")
    workdir = tmp_path / "repo"
    workdir.mkdir()
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="work",
        workdir=str(workdir),
        runtime_policy=RuntimePolicy(
            readable_paths=[str(workdir)], writable_paths=[str(workdir)]),
    )
    manager.start(config)
    writable = json.loads(config.env["MISSIONCREW_WRITABLE_DIRS"])
    assert writable[0] == str(workdir)
    for temp_dir in system_temp_dirs():
        assert temp_dir in writable
    assert str(codex_home) in writable
    readable = json.loads(config.env["MISSIONCREW_READABLE_DIRS"])
    assert str(codex_home) in readable
    # 业务层查询接口与 _prepare 使用同一声明
    assert manager.private_dirs(backend) == [str(codex_home)]


def test_prepare_keeps_read_only_policy_untouched(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    manager = RuntimeManager()
    manager.register("codex", _RecordingProvider())
    backend = Backend(id="codex", name="Codex", adapter="codex")
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="read",
        workdir=str(tmp_path),
        runtime_policy=RuntimePolicy(
            readable_paths=[str(tmp_path)],
            permissions=RuntimePermissions(filesystem="read-only")),
    )
    manager.start(config)
    assert json.loads(config.env["MISSIONCREW_WRITABLE_DIRS"]) == []
    assert json.loads(config.env["MISSIONCREW_READABLE_DIRS"]) == [str(tmp_path)]


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
        workdir=str(tmp_path), project_id="project-a", role_id="dev",
        session_key="channel::role")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True)
    try:
        adapters._track_process(config, process)
        manager = RuntimeManager()
        status = manager.status([backend])
        assert status["summary"]["one_shot"] == 1
        assert status["backends"][0]["state"] == "running"
        assert status["backends"][0]["projects"] == ["project-a"]
        assert status["backends"][0]["roles"] == ["dev"]
        instance = status["instances"][0]
        assert instance["transport"] == "cli-command"
        assert instance["mode"] == "one_shot"
        assert instance["pid"] == process.pid
        assert (instance["project_id"], instance["role_id"]) == ("project-a", "dev")
        assert manager.stop(backend, "channel::role") == 1
        assert process.poll() is not None
        assert manager.status([backend])["summary"]["live_instances"] == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_host_isolated_environ_strips_host_vars_but_keeps_infra():
    """宿主注入的变量不进 Agent 环境;PATH、代理和平台变量必须留下。"""
    stripped = {
        "VSCODE_IPC_HOOK_CLI": "/run/vscode.sock",
        "VSCODE_GIT_ASKPASS_MAIN": "/x/askpass.js",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDECODE": "1",                 # 无下划线,前缀匹配不到
        "GIT_ASKPASS": "/x/vscode/askpass.sh",   # 名字里看不出宿主
        "PYTHONSTARTUP": "/x/vscode/pythonrc.py",
        "SSH_AUTH_SOCK": "/run/agent.sock",
        "TERM_PROGRAM": "vscode",
    }
    kept = {
        "PATH": "/usr/bin:/home/u/.kimi-code/bin",   # 探测 CLI 要靠它
        "HTTP_PROXY": "http://127.0.0.1:7890",       # 剥掉会让下载走直连
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "localhost",
        "HOME": "/home/u",
        "MISSIONCREW_WORKSPACE": "/w",               # 平台自己注入的
        "MISSIONCREW_CLAUDE_SANDBOX": "on",          # CLAUDE 在中间,不该被误伤
    }
    result = adapters.host_isolated_environ({**stripped, **kept})
    assert result == kept


def test_runtime_env_isolates_host_vars_and_keeps_platform_env(tmp_path, monkeypatch):
    """派发层构造的子进程环境同样不含宿主变量,cfg.env 仍然生效。"""
    monkeypatch.setenv("VSCODE_NONCE", "n")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    backend = Backend(id="runtime", name="Runtime", adapter="recording")
    config = ExecutionConfig(
        task_id="task", stage_name="chat", backend=backend, prompt="work",
        workdir=str(tmp_path), env={"MISSIONCREW_WORKSPACE": "/w"})
    env = adapters._runtime_env(config, "claude_code")
    assert "VSCODE_NONCE" not in env and "CLAUDECODE" not in env
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["MISSIONCREW_WORKSPACE"] == "/w"
    assert env["PWD"] == str(tmp_path)
