"""角色配置:固定 runtime/model、定位/能力/偏好与管理 API。"""
import pytest
from fastapi.testclient import TestClient

from missioncrew import seed as seed_mod
from missioncrew.chat import ChatEngine
from missioncrew.models import Backend, Role
from missioncrew.server import create_app


# ---- 角色元数据 + 固定执行组合 ----

def test_abilities_fixed_and_preference_free_text():
    r = Role(id="x", runtime_id="std-1", model="pro",
             capabilities=["coding", "multimodal"], preference="前端,偏好 React")
    assert r.ability_labels() == ["代码执行", "图像/视觉输入"]   # 能力是固定选项
    assert r.preference == "前端,偏好 React"                     # 偏好是自由文本
    # 旧版 traits 标签自动迁移为偏好文本
    legacy = Role.from_dict({"id": "y", "project_id": "p", "runtime_id": "std-1",
                             "model": "", "traits": ["deep", "quality"]})
    assert legacy.preference == "深度攻坚、高质量"


def test_default_roles_are_bound_once_to_runtime_and_model(seeded):
    roles = seeded.list_roles("webshop")
    assert roles and all(r.runtime_id for r in roles)
    assert seeded.get_role("webshop", "expert").runtime_id == "exp-1"
    assert seeded.get_role("webshop", "tester").runtime_id == "eco-1"


def test_fixed_runtime_and_model(seeded):
    seeded.put_role(Role(id="fixed", project_id="webshop", name="固定",
                         runtime_id="std-1", model="custom-model"))
    chat = ChatEngine(seeded, max_workers=2)
    channel = seeded.get_channel("general")
    role = seeded.get_role("webshop", "fixed")
    backend, reason = chat._pick_backend(channel, role)
    assert backend.id == "std-1"
    assert backend.model == "custom-model"      # 本次执行用固定模型
    assert seeded.get_backend("std-1").model == "pro"  # 注册表不被改写
    assert "固定组合" in reason


def test_preferences_do_not_reroute_fixed_runtime(seeded):
    seeded.put_role(Role(id="cheap-expert", project_id="webshop", runtime_id="exp-1",
                         model="ultra", preference="快速低成本"))
    chat = ChatEngine(seeded, max_workers=2)
    backend, _ = chat._pick_backend(seeded.get_channel("general"),
                                    seeded.get_role("webshop", "cheap-expert"))
    assert backend.id == "exp-1" and backend.model == "ultra"


def test_fixed_runtime_disabled_reports_unavailable(seeded):
    b = seeded.get_backend("std-1")
    b.enabled = False
    seeded.put_backend(b)
    seeded.put_role(Role(id="fixed", project_id="webshop", runtime_id="std-1", model="pro"))
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
            "runtime_id": "std-1", "model": "pro", "capabilities": ["coding"],
            "description": "自由文本人格", "preference": "文档写作", "color": "#123456"}
    assert client.post("/api/roles", json=body).status_code == 200
    roles = {r["id"]: r for r in client.get("/api/roles").json()}
    assert roles["writer"]["preference"] == "文档写作"
    assert roles["writer"]["runtime_id"] == "std-1"
    assert roles["writer"]["model"] == "pro"
    assert roles["writer"]["description"] == "自由文本人格"
    assert client.delete("/api/roles/writer?project_id=webshop").status_code == 200
    assert "writer" not in {r["id"] for r in client.get("/api/roles").json()}


def test_role_api_rejects_unknown_trait_and_bad_id(client):
    p = {"project_id": "webshop", "runtime_id": "std-1", "model": "pro"}
    assert client.post("/api/roles", json={"id": "x", "capabilities": ["nope"], **p}).status_code == 400
    assert client.post("/api/roles", json={"id": "bad name", **p}).status_code == 400
    assert client.post("/api/roles", json={"id": "y", **{**p, "runtime_id": "ghost"}}).status_code == 400
    # 不填 runtime 是合法的:按能力自动路由的角色
    auto = client.post("/api/roles", json={"id": "auto-role", "project_id": "webshop"})
    assert auto.status_code == 200 and auto.json()["runtime_id"] == ""
    # 角色必须归属已存在的项目
    assert client.post("/api/roles", json={"id": "z", **{**p, "project_id": "ghost"}}).status_code == 400


def test_legacy_auto_routed_role_is_migrated_once(seeded):
    seeded._put("roles", "webshop:legacy", {
        "id": "legacy", "project_id": "webshop", "name": "旧角色",
        "required_capabilities": [], "traits": ["security"],
        "pinned_backend": None, "pinned_model": None,
        "min_tier": "standard", "max_tier": "expert",
    })
    assert seed_mod.ensure_role_bindings(seeded) == 1
    role = seeded.get_role("webshop", "legacy")
    assert role.runtime_id == "rev-1" and role.model == "pro"
    assert role.capabilities == ["review", "security"]
    assert "pinned_backend" not in role.to_dict() and "min_tier" not in role.to_dict()


def test_project_api_with_rules_yaml(client):
    body = {"id": "proj2", "name": "新项目", "charter": "范围",
            "rules_yaml": '- match: {task_type: bug}\n  require_evidence: [reproduction]\n'}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 200
    assert r.json()["rules"][0]["require_evidence"] == ["reproduction"]
    # 非法 YAML 返回 400
    bad = client.post("/api/projects", json={"id": "p3", "rules_yaml": "match: {"})
    assert bad.status_code == 400


def test_new_project_requires_an_enabled_runtime(store):
    empty_client = TestClient(create_app())
    response = empty_client.post("/api/projects", json={"id": "empty", "name": "Empty"})
    assert response.status_code == 400
    assert "runtime" in response.json()["detail"]


def test_runtime_and_model_in_use_cannot_be_removed(client, seeded):
    assert client.delete("/api/backends/std-1").status_code == 409

    seeded.put_backend(Backend(
        id="ladder", name="ladder", adapter="mock",
        models=[{"name": "small", "tier": "economy", "cost": 1},
                {"name": "large", "tier": "expert", "cost": 10}],
    ))
    seeded.put_role(Role(id="ladder-user", project_id="webshop",
                         runtime_id="ladder", model="large"))
    response = client.post("/api/backends", json={
        "id": "ladder", "models": [{"name": "small", "tier": "economy", "cost": 1}],
    })
    assert response.status_code == 400
    assert "ladder-user" in response.json()["detail"]


# ---- 项目第一层级:隔离与初始化 ----

def test_new_project_seeds_roles_and_channel(client, seeded):
    client.post("/api/projects", json={"id": "alpha", "name": "Alpha"})
    role_ids = {r.id for r in seeded.list_roles("alpha")}
    assert {"lead", "dev", "reviewer"} <= role_ids
    assert all(r.runtime_id for r in seeded.list_roles("alpha"))
    chans = seeded.list_channels("alpha")
    assert len(chans) == 1 and chans[0].id == "alpha:general"


def test_roles_isolated_between_projects(client, seeded):
    """A 项目的角色在 B 项目的频道里 @ 不到:项目之间互不相干。"""
    client.post("/api/projects", json={"id": "alpha", "name": "Alpha"})
    seeded.put_role(Role(id="only-a", project_id="alpha", name="A专属",
                         runtime_id="std-1", model="pro"))
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
    assert d["abilities"]["multimodal"] == "图像/视觉输入"   # 能力固定选项词表
    assert d["tiers"] == ["economy", "standard", "expert"]


# ---- 运行时页(仿 Multica):工具矩阵 + 状态,不含档位/成本配置 ----

def test_detect_report_lists_all_supported_tools():
    from missioncrew.adapters import KNOWN_CLIS, detect_report
    report = detect_report(with_version=False)
    assert {i["binary"] for i in report} == {b for b, *_ in KNOWN_CLIS}
    for i in report:
        assert isinstance(i["installed"], bool)
        if i["installed"]:
            assert i["path"]


def test_spa_fallback_serves_page_for_clean_urls(client):
    """干净 URL:非 API 路径返回页面,由前端路由还原;API 未知路径仍 404。"""
    r = client.get("/default/settings")
    assert r.status_code == 200 and "MissionCrew" in r.text
    assert client.get("/webshop/chat/general").status_code == 200
    assert client.get("/api/nonexistent").status_code == 404


def test_tools_endpoint_merges_registration_state(client):
    rows = client.get("/api/backends/tools").json()
    by_id = {r["id"]: r for r in rows}
    # 内置工具矩阵全部列出;种子里的 mock 工具作为已注册项附加
    assert "claude" in by_id and "eco-1" in by_id
    assert by_id["eco-1"]["registered"] is True
    for r in rows:  # 运行时页不暴露档位/成本/能力
        assert "tier" not in r and "cost_per_run" not in r and "capabilities" not in r


# ---- 模型清单来自 runtime(仿 Multica 动态发现) ----

def test_backend_models_endpoint_merges_ladder_and_runtime(client, seeded, monkeypatch):
    from missioncrew import adapters
    seeded.put_backend(Backend(
        id="laddered", name="laddered", adapter="mock",
        models=[{"name": "small", "tier": "economy", "cost": 1}]))
    monkeypatch.setattr(adapters, "list_runtime_models",
                        lambda b, timeout=25: ["dyn/alpha", "dyn/beta"])
    d = client.get("/api/backends/laddered/models").json()
    assert {m["name"] for m in d["configured"]} == {"small"}   # 配置阶梯保留
    assert d["discovered"] == ["dyn/alpha", "dyn/beta"]        # runtime 动态目录
    assert client.get("/api/backends/ghost/models").status_code == 404


def test_save_role_accepts_runtime_discovered_model(client, seeded, monkeypatch):
    from missioncrew import adapters
    seeded.put_backend(Backend(
        id="laddered", name="laddered", adapter="mock",
        models=[{"name": "small", "tier": "economy", "cost": 1}]))
    monkeypatch.setattr(adapters, "list_runtime_models",
                        lambda b, timeout=25: ["dyn/alpha"])
    ok = client.post("/api/roles", json={
        "id": "dyn-user", "project_id": "webshop", "runtime_id": "laddered",
        "model": "dyn/alpha"})
    assert ok.status_code == 200                              # 阶梯外但 runtime 提供
    bad = client.post("/api/roles", json={
        "id": "bad-user", "project_id": "webshop", "runtime_id": "laddered",
        "model": "nonexistent-model"})
    assert bad.status_code == 400                             # 两个目录都没有:拒绝


# ---- 按能力自动路由 + 主控改绑 runtime ----

def test_auto_routed_role_matches_ability(seeded):
    seeded.put_role(Role(id="viz", project_id="webshop", name="视觉",
                         capabilities=["multimodal"]))   # 未固定 runtime
    chat = ChatEngine(seeded, max_workers=2)
    backend, reason = chat._pick_backend(seeded.get_channel("general"),
                                         seeded.get_role("webshop", "viz"))
    assert backend.id == "vis-1"                 # 唯一具备 multimodal 的 runtime
    assert "自动路由" in reason
    # 无 runtime 满足能力时给出明确说明
    seeded.put_role(Role(id="talker", project_id="webshop", capabilities=["audio"]))
    backend2, reason2 = chat._pick_backend(seeded.get_channel("general"),
                                           seeded.get_role("webshop", "talker"))
    assert backend2 is None and "audio" in reason2


def test_auto_routed_role_prefers_cheapest_unit(seeded):
    seeded.put_role(Role(id="anyone", project_id="webshop", capabilities=["coding"]))
    chat = ChatEngine(seeded, max_workers=2)
    backend, _ = chat._pick_backend(seeded.get_channel("general"),
                                    seeded.get_role("webshop", "anyone"))
    assert backend.tier == "economy"             # 满足能力的最低档位


def test_orchestrator_can_rebind_role_runtime(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    reply = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"set_role_runtime","role":"dev",'
        '"runtime":"exp-1","model":"ultra"}</missioncrew-action>',
        root_id=1, depth=0)
    role = seeded.get_role("webshop", "dev")
    assert role.runtime_id == "exp-1" and role.model == "ultra"
    assert "已把 @dev 绑定到 exp-1+ultra" in reply
    # 缺少角色所需能力的 runtime:拒绝并说明
    reply2 = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"set_role_runtime","role":"vision",'
        '"runtime":"eco-1"}</missioncrew-action>', root_id=1, depth=0)
    assert "缺少角色所需能力" in reply2
    # 改回自动路由
    chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"set_role_runtime","role":"dev",'
        '"runtime":""}</missioncrew-action>', root_id=1, depth=0)
    assert seeded.get_role("webshop", "dev").runtime_id == ""
