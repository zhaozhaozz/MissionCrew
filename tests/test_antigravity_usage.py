"""Antigravity /usage 的额度解析、无推理探测和模型分池联动。"""
import copy
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from missioncrew.core.models import Backend, Role
from missioncrew.runtime.manager import RuntimeManager
from missioncrew.runtime.role_usage_linkage import RoleUsageLinkage
from missioncrew.runtime.usage import parse_antigravity_usage, probe_antigravity_usage


@pytest.fixture
def payload():
    return {
        "status": "SUCCESS", "conversation_id": "", "num_turns": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "response": "ignored human-readable text",
        "account": {"email": "private@example.test", "token": "fixture-only-secret"},
        "command": {"name": "usage", "data": {"groups": [
            {"name": "Gemini Models", "buckets": [{
                "id": "gemini-weekly", "window": "weekly",
                "remaining_fraction": 0.9282600283622742,
                "reset_time": "2026-09-23T07:42:32Z",
            }]},
            {"name": "Claude and GPT models", "buckets": [{
                "id": "3p-weekly", "window": "weekly", "remaining_fraction": 1,
                "reset_time": "2026-09-23T08:23:18Z",
            }]},
        ]}},
    }


def backend(binary=""):
    return Backend(id="antigravity", name="Antigravity", adapter="antigravity",
                   binary_path=binary)


def fake_cli(tmp_path, stdout, *, stderr="", exit_code=0, sleep=0, first=None):
    """first=(stdout, exit_code, sleep) 只作用于首次启动,模拟瞬时失败后重试成功。"""
    path = tmp_path / "agy"
    path.write_text(f"#!{sys.executable}\n" + f'''
import os, sys, time
from pathlib import Path
assert sys.argv[1:] == ["-p", "/usage", "--output-format", "json"]
assert "CLAUDECODE" not in os.environ
launches = Path({str(tmp_path / 'launches')!r})
count = len(launches.read_text().splitlines()) if launches.exists() else 0
with launches.open("a") as f:
    f.write(str(os.getpid()) + "\\n")
stdout, exit_code, sleep = {stdout!r}, {exit_code!r}, {sleep!r}
if count == 0 and {first!r} is not None:
    stdout, exit_code, sleep = {first!r}
time.sleep(sleep)
print(stdout)
print({stderr!r}, file=sys.stderr)
raise SystemExit(exit_code)
''')
    path.chmod(0o755)
    return backend(str(path))


def launches(tmp_path):
    return [int(pid) for pid in (tmp_path / "launches").read_text().split()]


def test_parser_converts_remaining_fractions_and_keeps_pools_separate(payload):
    snapshot = parse_antigravity_usage(backend(), payload)
    assert snapshot.status == "ok"
    assert snapshot.source == "antigravity_usage_command"
    assert [(w.key, w.used_percent, w.duration_minutes) for w in snapshot.windows] == [
        ("gemini-weekly", 7.17, 10080), ("3p-weekly", 0, 10080)]
    assert snapshot.windows[0].label == "Gemini Models · 本周"
    assert snapshot.windows[0].resets_at == datetime(
        2026, 9, 23, 7, 42, 32, tzinfo=timezone.utc).timestamp()
    result = snapshot.to_dict()
    assert result["windows"][0]["remaining_percent"] == 92.83
    assert "private@example.test" not in json.dumps(result)
    assert "fixture-only-secret" not in json.dumps(result)


def add_five_hour_buckets(payload):
    for group in payload["command"]["data"]["groups"]:
        bucket = copy.deepcopy(group["buckets"][0])
        bucket.update(id=bucket["id"].replace("weekly", "5h"), window="5h",
                      remaining_fraction=0.75, reset_time="2026-09-17T06:59:55Z")
        group["buckets"].append(bucket)


def test_five_hour_limits_are_labeled_and_precede_weekly_limits_per_group(payload):
    add_five_hour_buckets(payload)
    windows = parse_antigravity_usage(backend(), payload).windows
    assert [(w.key, w.label, w.duration_minutes) for w in windows] == [
        ("gemini-5h", "Gemini Models · 5 小时", 300),
        ("gemini-weekly", "Gemini Models · 本周", 10080),
        ("3p-5h", "Claude and GPT models · 5 小时", 300),
        ("3p-weekly", "Claude and GPT models · 本周", 10080),
    ]
    assert windows[0].used_percent == 25
    assert windows[0].resets_at == datetime(
        2026, 9, 17, 6, 59, 55, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("remaining", [None, True, "0.5", -0.1, 1.1, float("nan"), float("inf")])
def test_invalid_fraction_is_not_interpreted_as_zero_usage(payload, remaining):
    payload["command"]["data"]["groups"] = payload["command"]["data"]["groups"][:1]
    payload["command"]["data"]["groups"][0]["buckets"][0]["remaining_fraction"] = remaining
    snapshot = parse_antigravity_usage(backend(), payload)
    assert snapshot.status == "unavailable" and not snapshot.windows


def test_positive_balance_does_not_round_to_exhausted(payload):
    bucket = payload["command"]["data"]["groups"][0]["buckets"][0]
    bucket["remaining_fraction"] = 0.000001
    assert parse_antigravity_usage(backend(), payload).windows[0].used_percent < 100
    bucket["remaining_fraction"] = 0
    assert parse_antigravity_usage(backend(), payload).windows[0].used_percent == 100


@pytest.mark.parametrize("invalid", [
    {}, {"status": "ERROR"},
    {"status": "SUCCESS", "usage": {"input_tokens": 123, "output_tokens": 50}},
    {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [None, {}]}}},
])
def test_missing_quota_is_unavailable(invalid):
    assert parse_antigravity_usage(backend(), invalid).status == "unavailable"


def test_duplicate_buckets_are_not_displayed_twice(payload):
    groups = payload["command"]["data"]["groups"]
    groups.append(copy.deepcopy(groups[0]))
    assert len(parse_antigravity_usage(backend(), payload).windows) == 2


@pytest.mark.parametrize("reset", ["invalid", float("nan"), float("inf")])
def test_invalid_reset_is_omitted_without_discarding_quota(payload, reset):
    payload["command"]["data"]["groups"][0]["buckets"][0]["reset_time"] = reset
    result = parse_antigravity_usage(backend(), payload)
    assert result.status == "ok" and result.windows[0].resets_at is None


def test_probe_and_manager_expose_and_cache_usage(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("CLAUDECODE", "outer-host")
    runtime = fake_cli(tmp_path, json.dumps(payload))
    manager = RuntimeManager()
    assert manager.capabilities(runtime).account_usage
    first = manager.account_usage([runtime])
    assert first["summary"] == {"supported": 1, "available": 1, "unavailable": 0}
    assert first["usage"][0]["windows"][0]["key"] == "gemini-weekly"
    assert manager.account_usage([runtime])["usage"] == first["usage"]
    assert len((tmp_path / "launches").read_text().splitlines()) == 1
    manager.account_usage([runtime], refresh=True)
    assert len((tmp_path / "launches").read_text().splitlines()) == 2


@pytest.mark.parametrize("stdout,stderr,exit_code,status", [
    ('{"status":"ERROR","error":"authentication required for private@example.test"}', "", 1, "auth_required"),
    ("", "Please sign in: fixture-only-secret", 1, "auth_required"),
    ('{"status":"ERROR","error":"fixture-only-secret"}', "", 0, "unavailable"),
    ("not JSON: fixture-only-secret", "", 0, "unavailable"),
    ("[]", "", 0, "unavailable"),
])
def test_probe_failure_messages_do_not_expose_upstream_data(tmp_path, stdout, stderr, exit_code, status):
    runtime = fake_cli(tmp_path, stdout, stderr=stderr, exit_code=exit_code)
    result = probe_antigravity_usage(runtime).to_dict()
    assert result["status"] == status
    assert "fixture-only-secret" not in json.dumps(result)
    assert "private@example.test" not in json.dumps(result)


def test_probe_timeout_reaps_process_and_missing_binary_is_unavailable(tmp_path):
    runtime = fake_cli(tmp_path, "", sleep=30)
    assert probe_antigravity_usage(runtime, timeout=0.2).status == "unavailable"
    pids = launches(tmp_path)
    assert len(pids) == 2  # 超时算瞬时失败,重试一次后放弃;两次进程都被回收
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert probe_antigravity_usage(backend(str(tmp_path / "absent"))).status == "unavailable"


@pytest.mark.parametrize("first", [
    ('{"status":"ERROR","error":"upstream hiccup"}', 1, 0),  # 首轮报错
    ("", 0, 30),  # 首轮卡住直到超时被杀
])
def test_transient_failure_is_retried_with_a_fresh_process(tmp_path, payload, first):
    runtime = fake_cli(tmp_path, json.dumps(payload), first=first)
    assert probe_antigravity_usage(runtime, timeout=1).status == "ok"
    assert len(launches(tmp_path)) == 2


def test_login_failure_is_not_retried(tmp_path):
    runtime = fake_cli(tmp_path, "", stderr="Please sign in", exit_code=1)
    assert probe_antigravity_usage(runtime).status == "auth_required"
    assert len(launches(tmp_path)) == 1


def test_concurrent_requests_share_one_probe(tmp_path, payload):
    runtime = fake_cli(tmp_path, json.dumps(payload), sleep=0.6)
    manager = RuntimeManager()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(
            lambda _: manager.account_usage([runtime], refresh=True), range(3)))
    assert [item["usage"][0]["status"] for item in results] == ["ok"] * 3
    assert len(launches(tmp_path)) == 1
    # 探测结束后的刷新才会重新拉起进程
    manager.account_usage([runtime], refresh=True)
    assert len(launches(tmp_path)) == 2


@pytest.mark.parametrize("exhausted,blocked", [
    ("gemini-weekly", {"gemini"}), ("3p-weekly", {"claude", "gpt"}),
    ("gemini-5h", {"gemini"}), ("3p-5h", {"claude", "gpt"}),
])
def test_quota_linkage_only_stops_roles_in_exhausted_pool(store, payload, exhausted, blocked):
    add_five_hour_buckets(payload)
    models = {"gemini": "gemini-3.8-flash-high", "claude": "claude-sonnet-4-6",
              "gpt": "gpt-oss-120b-medium", "default": "", "custom": "custom/model"}
    for name, model in models.items():
        store.put_role(Role(id=name, project_id="p", runtime_id="antigravity",
                            model=model, usage_linkage_enabled=True))
    for group in payload["command"]["data"]["groups"]:
        for bucket in group["buckets"]:
            bucket["remaining_fraction"] = 0 if bucket["id"] == exhausted else 1
    linkage = RoleUsageLinkage(store, lambda refresh=True: {})
    linkage.reconcile({"usage": [parse_antigravity_usage(backend(), payload).to_dict()]}, now=100)
    assert {name for name in models if not store.get_role("p", name).enabled} == blocked
