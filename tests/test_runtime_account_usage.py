"""Runtime 账户限额的统一模型、解析与缓存。"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone

from missioncrew.core.models import Backend, ExecutionConfig, RunResult
from missioncrew.runtime import (RuntimeCapabilities, RuntimeManager,
                                 RuntimeProvider, RuntimeUsageSnapshot,
                                 RuntimeUsageWindow)
from missioncrew.runtime.codex import parse_codex_usage
from missioncrew.runtime.usage import (parse_claude_usage, parse_grok_usage,
                                       parse_kimi_usage, probe_claude_usage)


def _backend(adapter: str) -> Backend:
    return Backend(id=adapter, name=adapter.title(), adapter=adapter)


def test_claude_usage_parser_normalizes_session_and_weekly_windows():
    snapshot = parse_claude_usage(
        _backend("claude_code"),
        """Current session: 65% used · resets Jul 30, 11:19am (UTC)
Current week (all models): 53% used · resets Aug 3, 11am (UTC)
Current week (Fable): 78% used · resets Aug 3, 11am (UTC)""",
        now=datetime(2026, 7, 30, 8, tzinfo=timezone.utc),
    )

    assert snapshot.status == "ok"
    assert [(window.key, window.used_percent) for window in snapshot.windows] == [
        ("session", 65), ("weekly", 53), ("current-week-fable", 78)]
    assert snapshot.windows[0].duration_minutes == 300
    assert snapshot.windows[1].resets_at == datetime(
        2026, 8, 3, 11, tzinfo=timezone.utc).timestamp()


def test_codex_usage_parser_preserves_multiple_limit_ids():
    snapshot = parse_codex_usage(_backend("codex"), {
        "planType": "pro",
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": 35, "windowDurationMins": 300,
                    "resetsAt": 1893456000,
                },
                "secondary": {
                    "usedPercent": 52, "windowDurationMins": 10080,
                    "resetsAt": 1893974400,
                },
            },
            "codex_spark": {
                "primary": {
                    "usedPercent": 7, "windowDurationMins": 300,
                    "resetsAt": 1893456000,
                },
            },
        },
        "credits": {"hasCredits": True, "balance": 9},
    })

    assert snapshot.status == "ok" and snapshot.plan == "pro"
    assert [window.key for window in snapshot.windows] == [
        "codex-primary", "codex-secondary", "codex_spark-primary"]
    assert snapshot.windows[1].label == "本周 · Codex"
    assert snapshot.metrics[0].value == "9"


def test_kimi_usage_parser_maps_weekly_and_five_hour_limits():
    snapshot = parse_kimi_usage(_backend("kimi"), {
        "user": {"membership": {"level": "LEVEL_INTERMEDIATE"}},
        "usage": {
            "limit": "100", "used": "93", "remaining": "7",
            "resetTime": "2026-07-31T07:51:02Z",
        },
        "limits": [{
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "limit": "100", "remaining": "100",
                "resetTime": "2026-07-30T10:51:02Z",
            },
        }],
        "parallel": {"limit": "20"},
    })

    assert snapshot.status == "ok"
    assert snapshot.plan == "LEVEL_INTERMEDIATE"
    assert [(window.label, window.used_percent, window.duration_minutes)
            for window in snapshot.windows] == [
        ("5 小时", 0, 300), ("本周", 93, 10080)]
    assert snapshot.metrics[0].to_dict() == {"label": "并发上限", "value": "20"}


def test_grok_usage_parser_maps_credit_period_and_product_breakdown():
    snapshot = parse_grok_usage(_backend("grok_build"), {
        "config": {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-07-23T15:46:06Z",
                "end": "2026-07-30T15:46:06Z",
            },
            "creditUsagePercent": 100,
            "prepaidBalance": {"val": 0},
            "onDemandUsed": {"val": 3},
            "onDemandCap": {"val": 10},
            "productUsage": [
                {"product": "GrokBuild", "usagePercent": 99},
                {"product": "GrokChat", "usagePercent": 1},
            ],
        },
    })

    assert snapshot.status == "ok"
    assert snapshot.windows[0].used_percent == 100
    assert snapshot.windows[0].duration_minutes == 10080
    assert {metric.label: metric.value for metric in snapshot.metrics} == {
        "预付余额": "0", "按需已用": "3", "按需上限": "10",
        "GrokBuild": "99%", "GrokChat": "1%",
    }


def test_grok_usage_parser_treats_omitted_default_percent_as_zero():
    snapshot = parse_grok_usage(_backend("grok_build"), {
        "config": {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-07-30T15:46:06Z",
                "end": "2026-08-06T15:46:06Z",
            },
            "onDemandCap": {"val": 0},
            "onDemandUsed": {"val": 0},
            "prepaidBalance": {"val": 0},
        },
        "subscriptionTier": "SuperGrok",
    })

    assert snapshot.status == "ok"
    assert snapshot.plan == "SuperGrok"
    assert snapshot.windows[0].label == "本周"
    assert snapshot.windows[0].used_percent == 0
    assert snapshot.windows[0].duration_minutes == 10080


def test_grok_usage_parser_does_not_invent_zero_without_period():
    snapshot = parse_grok_usage(_backend("grok_build"), {
        "config": {"prepaidBalance": {"val": 0}},
        "subscriptionTier": "SuperGrok",
    })

    assert snapshot.status == "unavailable"
    assert not snapshot.windows


class _UsageProvider(RuntimeProvider):
    def __init__(self):
        self.calls = 0

    def start(self, config: ExecutionConfig) -> RunResult:
        return RunResult(True, "ok")

    def stop(self, backend: Backend, session_key: str = "") -> int:
        return 0

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(account_usage=True)

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return []

    def account_usage(
            self, backend: Backend, timeout: int = 15) -> RuntimeUsageSnapshot:
        self.calls += 1
        return RuntimeUsageSnapshot(
            backend_id=backend.id, backend_name=backend.name,
            adapter=backend.adapter, status="ok", source="test",
            windows=(RuntimeUsageWindow("weekly", "本周", 25),),
        )


def test_manager_caches_usage_and_force_refreshes():
    manager = RuntimeManager()
    provider = _UsageProvider()
    manager.register("usage-test", provider)
    backend = _backend("usage-test")

    first = manager.account_usage([backend])
    second = manager.account_usage([backend])
    refreshed = manager.account_usage([backend], refresh=True)

    assert first["usage"][0]["windows"][0]["remaining_percent"] == 75
    assert second["summary"] == {"supported": 1, "available": 1, "unavailable": 0}
    assert refreshed["usage"][0]["status"] == "ok"
    assert provider.calls == 2


def test_manager_requests_one_usage_refresh_after_execution(tmp_path):
    manager = RuntimeManager()
    provider = _UsageProvider()
    manager.register("usage-test", provider)
    backend = _backend("usage-test")
    refreshes = []
    manager.set_usage_refresh_handler(lambda backend_id: refreshes.append(backend_id))

    result = manager.start(ExecutionConfig(
        task_id="chat:1", stage_name="chat", backend=backend,
        prompt="test", workdir=str(tmp_path), project_id="p", role_id="r",
    ))

    assert result.success is True
    assert refreshes == ["usage-test"]


class _BarrierProvider(_UsageProvider):
    """所有后端必须同时在探测中才放行;探测池并发不足会让 Barrier 超时。"""

    def __init__(self, parties: int):
        super().__init__()
        self.barrier = threading.Barrier(parties, timeout=3)

    def account_usage(
            self, backend: Backend, timeout: int = 15) -> RuntimeUsageSnapshot:
        self.barrier.wait()
        return super().account_usage(backend, timeout)


def test_manager_probes_every_backend_at_once():
    backends = [Backend(id=f"b{i}", name=f"B{i}", adapter="usage-test")
                for i in range(6)]
    manager = RuntimeManager()
    manager.register("usage-test", _BarrierProvider(len(backends)))

    result = manager.account_usage(backends)

    assert result["summary"] == {"supported": 6, "available": 6, "unavailable": 0}


def test_claude_probe_skips_global_mcp_servers(tmp_path):
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(tmp_path / 'argv')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'result': 'Current session: 40% used'}))\n")
    script.chmod(0o755)

    snapshot = probe_claude_usage(_backend("claude_code"), [str(script)])

    argv = json.loads((tmp_path / "argv").read_text())
    assert argv[:4] == ["-p", "/usage", "--output-format", "json"]
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert snapshot.status == "ok" and snapshot.windows[0].used_percent == 40
