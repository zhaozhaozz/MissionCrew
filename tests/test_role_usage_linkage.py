"""账户限额与角色启停的事件驱动联动。"""
from __future__ import annotations

import time

from missioncrew.core.models import Role
from missioncrew.runtime.role_usage_linkage import RoleUsageLinkage


def _usage(*windows: dict) -> dict:
    return {
        "generated_at": 100,
        "usage": [{
            "backend_id": "claude", "backend_name": "Claude",
            "adapter": "claude_code", "status": "ok",
            "windows": list(windows),
        }],
    }


def _window(key: str, used: float, reset: float = 200) -> dict:
    return {
        "key": key, "label": key, "used_percent": used,
        "resets_at": reset, "duration_minutes": 10080,
    }


def test_fable_weekly_limit_only_controls_fable_roles(store):
    store.put_role(Role(
        id="sonnet", project_id="p", runtime_id="claude", model="sonnet"))
    store.put_role(Role(
        id="fable", project_id="p", runtime_id="claude", model="claude-fable-5"))
    store.put_role(Role(
        id="manual", project_id="p", runtime_id="claude", model="sonnet",
        enabled=False))
    store.put_app_setting(RoleUsageLinkage.SETTING_KEY, True)
    linkage = RoleUsageLinkage(store, lambda refresh=True: {"usage": []})

    linkage.reconcile(_usage(
        _window("weekly", 100), _window("current-week-fable", 20)), now=100)

    assert store.get_role("p", "sonnet").enabled is False
    assert store.get_role("p", "fable").enabled is True
    assert store.get_role("p", "manual").enabled is False
    assert {(item["role_id"], tuple(item["window_keys"]))
            for item in store.list_role_usage_blocks()} == {
        ("sonnet", ("weekly",)),
    }

    linkage.reconcile(_usage(
        _window("weekly", 20), _window("current-week-fable", 100)), now=100)

    assert store.get_role("p", "sonnet").enabled is True
    assert store.get_role("p", "fable").enabled is False
    assert store.get_role("p", "manual").enabled is False


def test_claude_session_limit_controls_fable_and_other_models(store):
    for role_id, model in (("sonnet", "sonnet"), ("fabel", "fabel")):
        store.put_role(Role(
            id=role_id, project_id="p", runtime_id="claude", model=model))
    store.put_app_setting(RoleUsageLinkage.SETTING_KEY, True)
    linkage = RoleUsageLinkage(store, lambda refresh=True: {"usage": []})

    linkage.reconcile(_usage(
        _window("session", 100, 150), _window("weekly", 20)), now=100)

    assert all(not store.get_role("p", role_id).enabled
               for role_id in ("sonnet", "fabel"))


def test_switch_off_restores_only_automatically_disabled_roles(store):
    store.put_role(Role(
        id="auto", project_id="p", runtime_id="claude", model="sonnet"))
    store.put_role(Role(
        id="manual", project_id="p", runtime_id="claude", model="sonnet",
        enabled=False))
    store.put_app_setting(RoleUsageLinkage.SETTING_KEY, True)
    linkage = RoleUsageLinkage(store, lambda refresh=True: _usage(
        _window("weekly", 100)))
    linkage.reconcile(now=100)

    state = linkage.set_enabled(False)

    assert state["enabled"] is False and state["auto_disabled_count"] == 0
    assert store.get_role("p", "auto").enabled is True
    assert store.get_role("p", "manual").enabled is False


def test_reset_timer_restores_role_without_periodic_usage_polling(store):
    store.put_role(Role(
        id="timed", project_id="p", runtime_id="claude", model="sonnet"))
    store.put_app_setting(RoleUsageLinkage.SETTING_KEY, True)
    calls = []
    linkage = RoleUsageLinkage(
        store, lambda refresh=True: calls.append(refresh) or {"usage": []})
    store.auto_disable_role_for_usage(
        "p", "timed", "claude", ["weekly"], time.time() + 0.08)

    linkage.start()
    try:
        deadline = time.time() + 1.5
        while not store.get_role("p", "timed").enabled and time.time() < deadline:
            time.sleep(0.02)
    finally:
        linkage.stop()

    assert store.get_role("p", "timed").enabled is True
    assert calls == []


def test_manual_disable_clears_auto_restore_ownership(store):
    store.put_role(Role(
        id="role", project_id="p", runtime_id="claude", model="sonnet"))
    store.auto_disable_role_for_usage(
        "p", "role", "claude", ["weekly"], 200)

    stored = store.set_role_enabled_manually("p", "role", False)

    assert stored.enabled is False
    assert store.list_role_usage_blocks() == []
