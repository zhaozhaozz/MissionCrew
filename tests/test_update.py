"""运行时更新:版本比较、更新方式解析、执行与 API。"""
import sys

import pytest
from fastapi.testclient import TestClient

from missioncrew import adapters
from missioncrew.models import Backend
from missioncrew.server import create_app


# ---- 版本比较 ----

def test_version_compare():
    assert adapters.is_newer("0.144.5", "0.144.4")
    assert adapters.is_newer("1.18.3", "1.17.20")     # 逐段数值比较,非字符串比较
    assert adapters.is_newer("2.0.0", "1.99.99")
    assert not adapters.is_newer("2.1.212", "2.1.212")
    assert not adapters.is_newer("", "1.0.0")
    assert not adapters.is_newer("1.0.0", "")          # 未知已装版本不提示更新


# ---- 更新方式解析 ----

def test_update_plan_self_update_for_non_npm_install():
    b = Backend(id="claude", name="c", adapter="claude_code",
                binary_path="/home/u/.local/bin/claude")   # 原生安装器,非 npm
    kind, cmd = adapters.update_plan(b)
    assert kind == "self" and cmd == ["claude", "update"]


def test_update_plan_npm_preferred_when_npm_managed(tmp_path):
    """npm 托管的安装优先 npm 更新(即使工具有自更新命令),渠道与版本检查一致。"""
    npm_bin = tmp_path / "lib" / "node_modules" / "opencode-ai" / "bin" / "opencode"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("")
    b = Backend(id="opencode", name="o", adapter="opencode", binary_path=str(npm_bin))
    kind, cmd = adapters.update_plan(b)
    assert kind == "npm" and cmd == ["npm", "install", "-g", "opencode-ai@latest"]
    # 同一工具装在别处(如自带安装脚本)则回落到自更新命令
    b2 = Backend(id="opencode", name="o", adapter="opencode",
                 binary_path="/home/u/.opencode/bin/opencode")
    assert adapters.update_plan(b2) == ("self", ["opencode", "upgrade"])


def test_update_plan_npm_requires_npm_managed_path(tmp_path):
    # 构造真实存在的 node_modules 路径(update_plan 会做 realpath)
    npm_bin = tmp_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("")
    b = Backend(id="codex", name="c", adapter="codex", binary_path=str(npm_bin))
    kind, cmd = adapters.update_plan(b)
    assert kind == "npm" and cmd == ["npm", "install", "-g", "@openai/codex@latest"]
    # 非 npm 管理的安装(如 VS Code 扩展内置)不允许 npm 更新
    b2 = Backend(id="codex", name="c", adapter="codex",
                 binary_path="/home/u/.vscode-server/data/copilotCli/codex")
    assert adapters.update_plan(b2) is None


def test_update_plan_unknown_adapter_not_updatable():
    assert adapters.update_plan(Backend(id="m", name="m", adapter="mock")) is None
    assert adapters.update_plan(Backend(id="k", name="k", adapter="kiro")) is None


def test_fetch_latest_only_for_known_npm_sources():
    assert adapters.fetch_latest_version("mock") == ""
    assert adapters.fetch_latest_version("kimi") == ""   # PyPI 同名包不可信,不查


# ---- 执行更新 ----

def test_run_update_executes_self_update(monkeypatch):
    monkeypatch.setitem(adapters.UPDATE_SPECS, "mock",
                        {"self_update": [sys.executable, "-c", "print('updated ok')"]})
    ok, log = adapters.run_update(Backend(id="m", name="m", adapter="mock"))
    assert ok and "updated ok" in log


def test_run_update_reports_failure(monkeypatch):
    monkeypatch.setitem(adapters.UPDATE_SPECS, "mock",
                        {"self_update": [sys.executable, "-c",
                                         "import sys; print('boom'); sys.exit(3)"]})
    ok, log = adapters.run_update(Backend(id="m", name="m", adapter="mock"))
    assert not ok and "boom" in log


def test_run_update_missing_command(monkeypatch):
    monkeypatch.setitem(adapters.UPDATE_SPECS, "mock",
                        {"self_update": ["/nonexistent/updater"]})
    ok, log = adapters.run_update(Backend(id="m", name="m", adapter="mock"))
    assert not ok and "不存在" in log


# ---- API ----

@pytest.fixture()
def client(seeded):
    return TestClient(create_app())


def test_check_updates_api(client, seeded, monkeypatch):
    seeded.put_backend(Backend(id="codex", name="codex", adapter="codex",
                               binary_path="/usr/lib/node_modules/@openai/codex/bin/x",
                               version="0.144.4"))
    monkeypatch.setattr(adapters, "fetch_latest_version",
                        lambda adapter, timeout=8: "0.144.5" if adapter == "codex" else "")
    rows = client.post("/api/backends/check_updates").json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["codex"]["update_available"] is True
    assert by_id["codex"]["latest"] == "0.144.5"
    # mock 后端没有更新规格,不出现在检查结果里
    assert "eco-1" not in by_id


def test_update_api_refreshes_version(client, seeded, monkeypatch):
    seeded.put_backend(Backend(id="codex", name="codex", adapter="codex",
                               binary_path="/usr/bin/codex", version="0.144.4"))
    monkeypatch.setattr(adapters, "run_update", lambda b, timeout=600: (True, "done"))
    monkeypatch.setattr("missioncrew.server.shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(adapters, "_cli_version", lambda binary: "0.144.5")
    r = client.post("/api/backends/codex/update").json()
    assert r["ok"] and r["old_version"] == "0.144.4" and r["version"] == "0.144.5"
    assert seeded.get_backend("codex").version == "0.144.5"   # 已入库


def test_update_api_unknown_backend_404(client):
    assert client.post("/api/backends/ghost/update").status_code == 404


def test_tools_endpoint_exposes_updatable(client, seeded):
    seeded.put_backend(Backend(id="kimi", name="kimi", adapter="kimi",
                               binary_path="/home/u/.kimi-code/bin/kimi"))
    rows = client.get("/api/backends/tools").json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["kimi"]["updatable"] is True      # 自更新命令
    assert by_id["eco-1"]["updatable"] is False    # mock 无更新规格
