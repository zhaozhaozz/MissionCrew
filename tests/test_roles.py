"""角色配置:偏好推导、固定组合、管理 API。"""
import pytest
from fastapi.testclient import TestClient

from missioncrew.chat import ChatEngine
from missioncrew.models import Role
from missioncrew.server import create_app


# ---- 偏好标签 -> 路由约束 ----

def test_traits_derive_constraints():
    r = Role(id="x", traits=["deep", "multimodal", "low-cost"])
    caps, min_tier, max_tier = r.effective_constraints()
    assert "multimodal" in caps
    assert min_tier == "expert"      # deep
    assert max_tier == "standard"    # low-cost


def test_explicit_constraints_merge_with_traits():
    r = Role(id="x", required_capabilities=["coding"], traits=["security"],
             min_tier="standard")
    caps, min_tier, _ = r.effective_constraints()
    assert {"coding", "security", "review"} <= set(caps)
    assert min_tier == "standard"


def test_deep_trait_routes_to_expert_backend(seeded):
    seeded.put_role(Role(id="digger", project_id="webshop", name="攻坚", traits=["deep"]))
    chat = ChatEngine(seeded, max_workers=2)
    chat.post("general", "human", "@digger 分析一下。")
    chat.wait_idle()
    runs = seeded._query("SELECT * FROM chat_runs WHERE role_id='digger'")
    assert runs and runs[0]["backend_id"] == "exp-1"


def test_lowcost_trait_never_uses_expert(seeded):
    seeded.put_role(Role(id="cheap", project_id="webshop", name="省钱", traits=["low-cost"]))
    chat = ChatEngine(seeded, max_workers=2)
    chat.post("general", "human", "@cheap 干点活。")
    chat.wait_idle()
    runs = seeded._query("SELECT * FROM chat_runs WHERE role_id='cheap'")
    assert runs and runs[0]["backend_id"] != "exp-1"
    b = seeded.get_backend(runs[0]["backend_id"])
    assert b.tier in ("economy", "standard")


def test_pinned_backend_and_model(seeded):
    seeded.put_role(Role(id="fixed", project_id="webshop", name="固定", pinned_backend="std-1",
                         pinned_model="custom-model"))
    chat = ChatEngine(seeded, max_workers=2)
    channel = seeded.get_channel("general")
    role = seeded.get_role("webshop", "fixed")
    backend, reason = chat._pick_backend(channel, role)
    assert backend.id == "std-1"
    assert backend.model == "custom-model"      # 本次执行用固定模型
    assert seeded.get_backend("std-1").model == "pro"  # 注册表不被改写
    assert "固定组合" in reason


def test_pinned_backend_disabled_reports_unavailable(seeded):
    b = seeded.get_backend("std-1")
    b.enabled = False
    seeded.put_backend(b)
    seeded.put_role(Role(id="fixed", project_id="webshop", pinned_backend="std-1"))
    chat = ChatEngine(seeded, max_workers=2)
    backend, reason = chat._pick_backend(seeded.get_channel("general"),
                                         seeded.get_role("webshop", "fixed"))
    assert backend is None and "不可用" in reason


# ---- 管理 API ----

@pytest.fixture()
def client(seeded):
    return TestClient(create_app())


def test_role_crud_api(client):
    body = {"id": "writer", "project_id": "webshop", "name": "写手",
            "description": "自由文本人格", "traits": ["docs", "fast"], "color": "#123456"}
    assert client.post("/api/roles", json=body).status_code == 200
    roles = {r["id"]: r for r in client.get("/api/roles").json()}
    assert roles["writer"]["traits"] == ["docs", "fast"]
    assert roles["writer"]["description"] == "自由文本人格"
    assert client.delete("/api/roles/writer?project_id=webshop").status_code == 200
    assert "writer" not in {r["id"] for r in client.get("/api/roles").json()}


def test_role_api_rejects_unknown_trait_and_bad_id(client):
    p = {"project_id": "webshop"}
    assert client.post("/api/roles", json={"id": "x", "traits": ["nope"], **p}).status_code == 400
    assert client.post("/api/roles", json={"id": "bad name", **p}).status_code == 400
    assert client.post("/api/roles", json={"id": "y", "pinned_backend": "ghost", **p}).status_code == 400
    # 角色必须归属已存在的项目
    assert client.post("/api/roles", json={"id": "z", "project_id": "ghost"}).status_code == 400


def test_project_api_with_rules_yaml(client):
    body = {"id": "proj2", "name": "新项目", "charter": "范围",
            "rules_yaml": '- match: {task_type: bug}\n  require_evidence: [reproduction]\n'}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 200
    assert r.json()["rules"][0]["require_evidence"] == ["reproduction"]
    # 非法 YAML 返回 400
    bad = client.post("/api/projects", json={"id": "p3", "rules_yaml": "match: {"})
    assert bad.status_code == 400


# ---- 项目第一层级:隔离与初始化 ----

def test_new_project_seeds_roles_and_channel(client, seeded):
    client.post("/api/projects", json={"id": "alpha", "name": "Alpha"})
    role_ids = {r.id for r in seeded.list_roles("alpha")}
    assert {"lead", "dev", "reviewer"} <= role_ids
    chans = seeded.list_channels("alpha")
    assert len(chans) == 1 and chans[0].id == "alpha:general"


def test_roles_isolated_between_projects(client, seeded):
    """A 项目的角色在 B 项目的频道里 @ 不到:项目之间互不相干。"""
    client.post("/api/projects", json={"id": "alpha", "name": "Alpha"})
    seeded.put_role(Role(id="only-a", project_id="alpha", name="A专属"))
    chat = ChatEngine(seeded, max_workers=2)
    # webshop 的 general 频道里 @alpha 的专属角色:不触发
    chat.post("general", "human", "@only-a 在吗?@dev 你也看看。")
    chat.wait_idle()
    agents = {m["author"] for m in seeded.list_messages("general")
              if m["author_type"] == "agent"}
    assert agents == {"dev"}


def test_delete_project_cascades_roles_and_channels(client, seeded):
    client.post("/api/projects", json={"id": "alpha", "name": "Alpha"})
    assert seeded.list_roles("alpha")
    client.delete("/api/projects/alpha")
    assert not seeded.list_roles("alpha")
    assert not seeded.list_channels("alpha")


def test_channel_id_namespaced_by_project(client, seeded):
    r = client.post("/api/chat/channels",
                    json={"id": "repo", "project_id": "webshop"})
    assert r.status_code == 200
    assert r.json()["id"] == "webshop:repo"
    # 不给项目或项目不存在:拒绝
    assert client.post("/api/chat/channels", json={"id": "x"}).status_code == 400
    assert client.post("/api/chat/channels",
                       json={"id": "x", "project_id": "ghost"}).status_code == 400


def test_backend_update_api(client, seeded):
    r = client.post("/api/backends", json={"id": "eco-1", "enabled": False, "tier": "standard"})
    assert r.status_code == 200
    b = seeded.get_backend("eco-1")
    assert b.enabled is False and b.tier == "standard"
    assert client.post("/api/backends", json={"id": "eco-1", "tier": "ultra"}).status_code == 400


def test_traits_endpoint(client):
    d = client.get("/api/traits").json()
    assert "low-cost" in d["traits"] and d["tiers"] == ["economy", "standard", "expert"]


# ---- 运行时页(仿 Multica):工具矩阵 + 状态,不含档位/成本配置 ----

def test_detect_report_lists_all_supported_tools():
    from missioncrew.adapters import KNOWN_CLIS, detect_report
    report = detect_report(with_version=False)
    assert {i["binary"] for i in report} == {b for b, *_ in KNOWN_CLIS}
    for i in report:
        assert isinstance(i["installed"], bool)
        if i["installed"]:
            assert i["path"]


def test_tools_endpoint_merges_registration_state(client):
    rows = client.get("/api/backends/tools").json()
    by_id = {r["id"]: r for r in rows}
    # 内置工具矩阵全部列出;种子里的 mock 工具作为已注册项附加
    assert "claude" in by_id and "eco-1" in by_id
    assert by_id["eco-1"]["registered"] is True
    for r in rows:  # 运行时页不暴露档位/成本/能力
        assert "tier" not in r and "cost_per_run" not in r and "capabilities" not in r
