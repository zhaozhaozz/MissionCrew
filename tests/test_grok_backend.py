"""Grok Build 后端的 ACP 命令、自动检测与更新方式。"""

from missioncrew.runtime import adapters
from missioncrew.runtime.manager import RuntimeManager
from missioncrew.core.models import Backend


def test_grok_command_uses_acp_stdio_mode():
    template = adapters.ACP_SERVE_COMMANDS["grok_build"]

    assert adapters.render_command(
        template, "", "grok-build", workdir="/work/project") == [
        "grok", "--cwd", "/work/project", "agent", "--always-approve",
        "--no-leader", "stdio",
    ]
    assert isinstance(adapters.get_adapter("grok_build"), adapters.AcpAdapter)


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
