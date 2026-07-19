"""Grok Build 后端的命令模板、自动检测与更新方式。"""

from missioncrew.runtime import adapters
from missioncrew.core.models import Backend


def test_grok_command_uses_headless_automation_mode():
    template = adapters.DEFAULT_COMMANDS["grok_build"]

    assert adapters.render_command(template, "修复登录问题", "grok-build") == [
        "grok", "-p", "修复登录问题", "--model", "grok-build",
        "--always-approve", "--no-auto-update",
    ]
    assert adapters.render_command(template, "修复登录问题", "") == [
        "grok", "-p", "修复登录问题", "--always-approve", "--no-auto-update",
    ]


def test_grok_detection_creates_routable_backend(monkeypatch):
    grok_path = "/home/u/.grok/bin/grok"
    monkeypatch.setattr(adapters.shutil, "which",
                        lambda binary: grok_path if binary == "grok" else None)
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
