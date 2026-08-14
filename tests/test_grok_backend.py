"""Grok Build 后端的 ACP 命令、自动检测与更新方式。"""

from missioncrew.runtime import adapters
from missioncrew.runtime.manager import RuntimeManager
from missioncrew.core.models import Backend


def test_grok_command_uses_acp_stdio_mode():
    template = adapters.ACP_SERVE_COMMANDS["grok_build"]

    # 未配 effort 时 `--reasoning-effort` 整对丢弃,不能留下悬空标志
    assert adapters.render_command(
        template, "", "grok-build", workdir="/work/project") == [
        "grok", "--cwd", "/work/project", "agent", "--always-approve",
        "--no-leader", "stdio",
    ]
    assert isinstance(adapters.get_adapter("grok_build"), adapters.AcpAdapter)


def test_grok_effort_renders_into_serve_command():
    template = adapters.ACP_SERVE_COMMANDS["grok_build"]

    assert adapters.render_command(
        template, "", "grok-build", effort="high",
        workdir="/work/project") == [
        "grok", "--cwd", "/work/project", "agent",
        "--reasoning-effort", "high", "--always-approve",
        "--no-leader", "stdio",
    ]


def test_grok_exposes_cli_effort_levels():
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build")

    # 未知模型时回退 adapter 级档位:取自 `grok --reasoning-effort <非法值>`
    # 的报错清单(grok-4.6 全量),从低到高
    assert RuntimeManager().effort_options(backend) == [
        "low", "medium", "high", "xhigh"]


def test_effort_options_prefer_per_model_levels():
    """工具自报按模型档位时以它为准:grok-4.5 没有 xhigh,不能照抄 adapter 清单。"""
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build")
    manager = RuntimeManager()
    reported = {"grok-4.6": ["low", "medium", "high", "xhigh"],
                "grok-4.5": ["low", "medium", "high"]}

    assert manager.effort_options(backend, "grok-4.5", reported) == [
        "low", "medium", "high"]
    assert manager.effort_options(backend, "grok-4.6", reported) == [
        "low", "medium", "high", "xhigh"]
    # 模型留空(CLI 默认)或模型不在自报表里:回退 adapter 级档位
    assert manager.effort_options(backend, "", reported) == [
        "low", "medium", "high", "xhigh"]
    assert manager.effort_options(backend, "grok-9", reported) == [
        "low", "medium", "high", "xhigh"]


def test_discovered_efforts_sorted_low_to_high():
    """grok 按高到低返回,进下拉前统一成与 EFFORT_SUPPORT 一致的低到高。"""
    assert adapters.sort_efforts(["xhigh", "high", "medium", "low"]) == [
        "low", "medium", "high", "xhigh"]
    # 没见过的档位不丢弃,排在已知档位之后
    assert adapters.sort_efforts(["high", "turbo", "low"]) == [
        "low", "high", "turbo"]


def test_grok_model_discovery_retries_empty_or_fallback_once(monkeypatch):
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build")
    final = (["grok-4.6", "grok-4.5"], {
        "grok-4.6": ["low", "medium", "high", "xhigh"],
        "grok-4.5": ["low", "medium", "high"],
    })

    for first in (([], {}), (["grok-4.5"], {
            "grok-4.5": ["low", "medium", "high"]})):
        replies = iter((first, final))
        calls = []

        def probe(*args, **kwargs):
            calls.append((args, kwargs))
            return next(replies)

        monkeypatch.setattr(adapters, "_list_acp_model_catalog", probe)

        assert adapters.list_runtime_model_catalog(backend) == final
        assert len(calls) == 2
        assert all(call[1]["parse_efforts"] is not None for call in calls)


def test_grok_model_discovery_retries_at_most_once(monkeypatch):
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build")
    fallback = (["grok-4.5"], {
        "grok-4.5": ["low", "medium", "high"]})
    calls = []

    def probe(*args, **kwargs):
        calls.append((args, kwargs))
        return fallback

    monkeypatch.setattr(adapters, "_list_acp_model_catalog", probe)

    assert adapters.list_runtime_model_catalog(backend) == fallback
    assert len(calls) == 2


def test_grok_model_discovery_keeps_complete_first_result(monkeypatch):
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build")
    complete = (["grok-4.6", "grok-4.5"], {
        "grok-4.6": ["low", "medium", "high", "xhigh"],
        "grok-4.5": ["low", "medium", "high"]})
    calls = []

    def probe(*args, **kwargs):
        calls.append((args, kwargs))
        return complete

    monkeypatch.setattr(adapters, "_list_acp_model_catalog", probe)

    assert adapters.list_runtime_model_catalog(backend) == complete
    assert len(calls) == 1


def test_grok_detection_creates_routable_backend(monkeypatch, tmp_path):
    grok_path = "/home/u/.grok/bin/grok"
    monkeypatch.setattr(adapters.shutil, "which",
                        lambda binary: grok_path if binary == "grok" else None)
    # pi 走 vendored 检测,不受 which mock 影响;隔离平台目录避免误检本机安装
    monkeypatch.setenv("MISSIONCREW_HOME", str(tmp_path / "mc-home"))
    report = adapters.detect_report(with_version=False)
    grok = next(item for item in report if item["binary"] == "grok")
    found = adapters.detect_backends(report)

    assert grok["id"] == "grok" and grok["path"] == grok_path
    assert len(found) == 1
    backend = found[0]
    assert backend.id == "grok"
    assert backend.adapter == "grok_build"
    assert backend.model == "" and backend.models == []
    assert {"coding", "reasoning", "web_search", "sub_agents"} <= set(backend.capabilities)


def test_grok_uses_native_updater():
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build",
                      binary_path="/home/u/.grok/bin/grok")

    assert adapters.update_plan(backend) == ("self", ["grok", "update"])


def test_grok_reports_structured_acp_capabilities():
    backend = Backend(id="grok", name="Grok Build", adapter="grok_build")

    capabilities = RuntimeManager().capabilities(backend)

    assert capabilities.session_reuse
    assert capabilities.structured_events
    assert capabilities.permission_control
    assert not capabilities.interrupt
