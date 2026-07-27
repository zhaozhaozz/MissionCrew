"""打印模式 Runtime 的原生会话参数翻译。"""
import threading
import time

from missioncrew.core.models import Backend, ExecutionConfig, RunResult
from missioncrew.runtime import adapters


def _cfg(session_id=""):
    return ExecutionConfig(
        task_id="chat", stage_name="chat",
        backend=Backend(id="runtime", name="runtime", adapter="claude_code"),
        prompt="公共\n最近对话\n当前任务", workdir="/workspace",
        session_key="channel::role", session_id=session_id,
        common_prompt="公共", turn_prompt="当前任务",
        recovery_prompt="最近对话\n当前任务", context_version="v1",
    )


def test_fixed_id_cli_creates_then_resumes_same_session():
    cfg = _cfg()
    prompt, session_id, reused, mode = adapters._prepare_cli_session(
        "claude_code", cfg)
    assert session_id and not reused and "最近对话" in prompt
    assert mode == "recovery"
    initial, _ = adapters._apply_cli_session_args(
        "claude_code", ["claude", "-p", prompt], session_id, reused)
    assert initial[-2:] == ["--session-id", session_id]

    cfg.session_id = session_id
    prompt, resumed_id, reused, mode = adapters._prepare_cli_session(
        "claude_code", cfg)
    assert reused and resumed_id == session_id and mode == "lean"
    assert prompt == (adapters.LEAN_TURN_TEMPLATE.format(context_version="v1")
                      + "当前任务")
    resumed, _ = adapters._apply_cli_session_args(
        "claude_code", ["claude", "-p", prompt], resumed_id, reused)
    assert resumed[-2:] == ["--resume", session_id]


def test_session_input_modes_cover_update_reinject_and_lean():
    reused = _cfg(session_id="s1")
    lean, mode = adapters._session_input(reused, recovery=False)
    assert mode == "lean" and "公共\n" not in lean and "当前任务" in lean

    changed = _cfg(session_id="s1")
    changed.context_changed = True
    full, mode = adapters._session_input(changed, recovery=False)
    assert mode == "update"
    assert full.startswith("公共\n") and "# MissionCrew 公共上下文更新" in full

    due = _cfg(session_id="s1")
    due.reinject_due = True
    full, mode = adapters._session_input(due, recovery=False)
    assert mode == "reinject"
    assert full.startswith("公共\n") and "# MissionCrew 公共上下文重注入" in full

    fresh = _cfg()
    full, mode = adapters._session_input(fresh, recovery=True)
    assert mode == "recovery" and full == "公共\n最近对话\n当前任务"


def test_captured_and_directory_session_arguments(tmp_path):
    opencode, structured = adapters._apply_cli_session_args(
        "opencode", ["opencode", "run", "任务"], "ses_123456", True)
    assert structured and opencode[-5:] == [
        "--format", "json", "--session", "ses_123456", "任务"]
    assert adapters._extract_session_id(
        '{"type":"start","sessionID":"ses_123456"}') == "ses_123456"

    session_dir = str(tmp_path / "sessions" / "x")
    pi, structured = adapters._apply_cli_session_args(
        "pi", ["pi", "-p", "任务"], f"pi-dir:{session_dir}", True)
    assert not structured
    assert pi[:4] == ["pi", "--session-dir", session_dir, "--continue"]


def test_every_default_print_runtime_has_a_session_strategy():
    assert adapters._CLI_SESSION_ADAPTERS == set(adapters.DEFAULT_COMMANDS)


def test_resume_arguments_for_all_print_runtime_strategies(tmp_path):
    fixed = {
        "claude_code": "--resume",
        "codebuddy": "--resume",
        "copilot": "--session-id",
    }
    for adapter_name, flag in fixed.items():
        command, structured = adapters._apply_cli_session_args(
            adapter_name, [adapter_name, "-p", "任务"], "session-123", True)
        assert not structured
        assert command[-2:] == [flag, "session-123"]

    codex, structured = adapters._apply_cli_session_args(
        "codex",
        ["codex", "exec", "--sandbox", "workspace-write", "-m", "gpt", "任务"],
        "session-123", True,
    )
    assert not structured
    assert codex == [
        "codex", "--sandbox", "workspace-write", "-m", "gpt",
        "exec", "resume", "session-123", "任务",
    ]

    cursor, structured = adapters._apply_cli_session_args(
        "cursor", ["cursor-agent", "-p", "任务"], "session-123", True)
    assert structured
    assert cursor[-4:] == ["--output-format", "json", "--resume", "session-123"]

    pi_dir = str(tmp_path / "pi")
    pi, structured = adapters._apply_cli_session_args(
        "pi", ["pi", "-p", "任务"], f"pi-dir:{pi_dir}", True)
    assert not structured and "--continue" in pi


def test_serialized_runtime_rechecks_cancellation_after_session_lock(monkeypatch):
    adapter = adapters.MockAdapter()
    first = _cfg()
    second = _cfg()
    first.prompt = second.prompt = "# 聊天协作请求\nwork"
    cancelled = threading.Event()
    second.cancelled = cancelled.is_set
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_chat(config):
        calls.append(config)
        entered.set()
        assert release.wait(5)
        return RunResult(True, "done", output="done")

    monkeypatch.setattr(adapter, "_chat", blocking_chat)
    results = {}
    first_worker = threading.Thread(
        target=lambda: results.setdefault("first", adapter.run(first)))
    second_worker = threading.Thread(
        target=lambda: results.setdefault("second", adapter.run(second)))
    first_worker.start()
    assert entered.wait(5)
    second_worker.start()
    time.sleep(0.05)  # 第二轮已在等待同一 session lock
    cancelled.set()
    release.set()
    first_worker.join(timeout=5)
    second_worker.join(timeout=5)

    assert not first_worker.is_alive() and not second_worker.is_alive()
    assert results["first"].success
    assert not results["second"].success
    assert results["second"].summary == "执行已停止"
    assert calls == [first]
