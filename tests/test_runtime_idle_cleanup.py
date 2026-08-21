"""长驻 Runtime 会话的空闲回收(claude/codex/pi provider 与统一 reaper)。"""
from __future__ import annotations

import time

import pytest

from missioncrew.runtime.claude import (ClaudeRuntimeProvider, _ClaudeSession,
                                        _TurnSink)
from missioncrew.runtime.codex import CodexRuntimeProvider, _CodexSession
from missioncrew.runtime.manager import RuntimeManager
from missioncrew.runtime.pi import PiRuntimeProvider, _PiSession

PROVIDERS = [
    (ClaudeRuntimeProvider, _ClaudeSession),
    (CodexRuntimeProvider, _CodexSession),
    (PiRuntimeProvider, _PiSession),
]


def _provider_with_session(provider_cls, session_cls, tmp_path, *,
                           persistent: bool = True):
    provider = provider_cls(fallback=None)
    session = session_cls(["fake-cli"], "b1", "chan::role",
                          str(tmp_path), persistent=persistent)
    provider._sessions["chan::role"] = session
    return provider, session


@pytest.mark.parametrize("provider_cls,session_cls", PROVIDERS)
def test_idle_persistent_session_is_reclaimed(provider_cls, session_cls,
                                              tmp_path):
    provider, session = _provider_with_session(
        provider_cls, session_cls, tmp_path)
    session.last_activity = time.time() - 3600
    assert provider.cleanup_idle(time.time() - 1800) == 1
    assert not provider._sessions


@pytest.mark.parametrize("provider_cls,session_cls", PROVIDERS)
def test_recently_active_session_survives_cleanup(provider_cls, session_cls,
                                                  tmp_path):
    provider, session = _provider_with_session(
        provider_cls, session_cls, tmp_path)
    session.last_activity = time.time()
    assert provider.cleanup_idle(time.time() - 1800) == 0
    assert provider._sessions


def test_claude_background_work_blocks_idle_cleanup(tmp_path):
    cutoff = time.time() - 1800
    for busy in ("_background_tasks", "_wake_sink", "_pending_native_agents"):
        provider, session = _provider_with_session(
            ClaudeRuntimeProvider, _ClaudeSession, tmp_path)
        session.last_activity = time.time() - 3600
        if busy == "_background_tasks":
            session._background_tasks["task-1"] = {"description": "bash"}
        elif busy == "_wake_sink":
            session._wake_sink = _TurnSink()
        else:
            session._pending_native_agents.add("agent-1")
        assert provider.cleanup_idle(cutoff) == 0, busy
        assert provider._sessions, busy


def test_manager_cleanup_reaps_providers_and_acp_pool(tmp_path, monkeypatch):
    manager = RuntimeManager()
    claude_provider = manager._providers["claude_code"]
    session = _ClaudeSession(["fake-cli"], "b1", "chan::role",
                             str(tmp_path), persistent=True)
    session.last_activity = time.time() - 3600
    claude_provider._sessions["chan::role"] = session

    acp_swept = []
    from missioncrew.runtime import acp
    monkeypatch.setattr(acp, "cleanup_idle_sessions",
                        lambda: acp_swept.append(True))

    assert manager.cleanup_idle() == 1
    assert not claude_provider._sessions
    assert acp_swept


def test_manager_idle_reaper_runs_periodically_until_stopped():
    manager = RuntimeManager()
    manager.IDLE_REAPER_INTERVAL = 0.02
    calls = []
    manager.cleanup_idle = lambda *a, **k: calls.append(True)
    manager.start_idle_reaper()
    manager.start_idle_reaper()  # 幂等,不得再起第二个线程
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    manager.stop_idle_reaper()
    assert calls
    settled = len(calls)
    time.sleep(0.1)
    assert len(calls) <= settled + 1  # 停止后不再继续
