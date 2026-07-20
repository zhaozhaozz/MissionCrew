"""角色配置:固定 runtime/model、定位/能力/偏好与管理 API。"""
import pytest
from fastapi.testclient import TestClient

from missioncrew.core import seed as seed_mod
from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import Backend, Role
from missioncrew.api import create_app


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


def test_global_role_templates_seed_new_projects_and_first_is_default(client, seeded):
    templates = client.get("/api/role-templates").json()
    assert [role["id"] for role in templates][:3] == ["lead", "dev", "reviewer"]
    assert client.get("/api/overview").json()["role_templates"] == templates

    coordinator = {
        "id": "coordinator", "name": "协调者", "runtime_id": "exp-1",
        "model": "ultra", "effort": "high", "capabilities": ["reasoning"],
        "description": "负责新项目调度", "preference": "先规划", "color": "#112233",
    }
    assert client.post("/api/role-templates", json=coordinator).status_code == 200
    ids = ["coordinator", *[role["id"] for role in templates]]
    assert client.post("/api/role-templates/reorder", json={"ids": ids}).status_code == 200

    created = client.post("/api/projects", json={"id": "templated", "name": "Templated"})
    assert created.status_code == 200
    assert created.json()["orchestrator_role_id"] == "coordinator"
    copied = seeded.get_role("templated", "coordinator")
    assert (copied.runtime_id, copied.model, copied.effort) == ("exp-1", "ultra", "high")
    assert copied.description == "负责新项目调度"
    assert [role.id for role in seeded.list_roles("templated")] == ids

    # 模板是新项目的快照来源，不会反向修改已有项目。
    coordinator["name"] = "新名称"
    assert client.post("/api/role-templates", json=coordinator).status_code == 200
    assert seeded.get_role("templated", "coordinator").name == "协调者"
    assert seeded.get_project("webshop").orchestrator_role_id == "lead"
    assert seeded.get_role("webshop", "coordinator") is None


def test_global_role_template_validation_and_delete_guard(client, seeded):
    bad = client.post("/api/role-templates", json={
        "id": "bad", "runtime_id": "missing", "capabilities": ["coding"],
    })
    assert bad.status_code == 400 and "runtime 不存在" in bad.json()["detail"]

    templates = seeded.list_role_templates()
    for role in templates[1:]:
        seeded.delete_role_template(role.id)
    only = seeded.list_role_templates()[0]
    denied = client.delete(f"/api/role-templates/{only.id}")
    assert denied.status_code == 409 and "至少保留一个" in denied.json()["detail"]


def test_new_project_rejects_unavailable_template_runtime_without_partial_write(client, seeded):
    template = seeded.list_role_templates()[0]
    backend = seeded.get_backend(template.runtime_id)
    backend.enabled = False
    seeded.put_backend(backend)
    response = client.post("/api/projects", json={"id": "blocked", "name": "Blocked"})
    assert response.status_code == 400
    assert f"@{template.id}" in response.json()["detail"] and "已停用" in response.json()["detail"]
    assert seeded.get_project("blocked") is None
    assert seeded.list_roles("blocked") == []


def test_role_api_rejects_unknown_trait_and_bad_id(client):
    p = {"project_id": "webshop", "runtime_id": "std-1", "model": "pro"}
    assert client.post("/api/roles", json={"id": "x", "capabilities": ["nope"], **p}).status_code == 400
    # 退役的职责类标签不再是合法能力选项
    assert client.post("/api/roles", json={"id": "x", "capabilities": ["review"], **p}).status_code == 400
    assert client.post("/api/roles", json={"id": "bad name", **p}).status_code == 400
    assert client.post("/api/roles", json={"id": "y", **{**p, "runtime_id": "ghost"}}).status_code == 400
    # runtime 是定义角色时的必选项:缺字段 422,显式传空 400
    assert client.post("/api/roles", json={"id": "no-rt", "project_id": "webshop"}).status_code == 422
    empty = client.post("/api/roles", json={"id": "no-rt", "project_id": "webshop", "runtime_id": ""})
    assert empty.status_code == 400 and "必须选择 runtime" in empty.json()["detail"]
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
    # 职责类标签不再进能力位:security 的含义只保留在偏好文本里,
    # 但绑定仍按职责倾向落在具备 security 能力位的后端上
    assert role.runtime_id == "rev-1" and role.model == "pro"
    assert role.capabilities == []
    assert "安全审查" in role.preference
    assert "pinned_backend" not in role.to_dict() and "min_tier" not in role.to_dict()


def test_retired_ability_tags_migrate_into_preference():
    """v0.5:代码评审/安全审查/多 Agent 编排从能力词表退役,读取即迁移。"""
    role = Role.from_dict({
        "id": "old", "project_id": "p", "runtime_id": "std-1",
        "capabilities": ["coding", "review", "security", "sub_agents"],
        "preference": "严谨",
    })
    assert role.capabilities == ["coding"]
    assert role.preference == "严谨、代码评审、安全审查、多 Agent 编排"
    # 已有同名片段不重复;子串命中(如否定表述)不算已含,宁重勿丢
    same = Role.from_dict({"id": "x", "project_id": "p", "runtime_id": "std-1",
                           "capabilities": ["review"], "preference": "代码评审,严谨"})
    assert same.capabilities == [] and same.preference == "代码评审,严谨"
    neg = Role.from_dict({"id": "y", "project_id": "p", "runtime_id": "std-1",
                          "capabilities": ["review"], "preference": "不做代码评审"})
    assert neg.preference == "不做代码评审、代码评审"


def test_seed_binding_keeps_duty_affinity(seeded):
    """评审/安全角色的职责在偏好文本里,绑定仍应选 review/security 后端。"""
    assert seeded.get_role("webshop", "reviewer").runtime_id == "rev-1"
    assert seeded.get_role("webshop", "secure").runtime_id == "rev-1"


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


def test_enabling_first_runtime_initializes_global_role_templates(store):
    store.put_backend(Backend(id="paused", name="Paused", adapter="mock", enabled=False))
    empty_client = TestClient(create_app())
    assert empty_client.get("/api/role-templates").json() == []
    assert empty_client.post("/api/backends", json={"id": "paused", "enabled": True}).status_code == 200
    templates = empty_client.get("/api/role-templates").json()
    assert templates[0]["id"] == "lead"
    assert all(role["runtime_id"] == "paused" for role in templates)


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
    assert agents == {"dev", "lead"}


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
    # 职责/runtime 特性不属于角色能力词表(后端能力位是独立词表,不受影响)
    assert not {"review", "security", "sub_agents"} & set(d["abilities"])
    assert d["tiers"] == ["economy", "standard", "expert"]


# ---- 运行时页(仿 Multica):工具矩阵 + 状态,不含档位/成本配置 ----

def test_detect_report_lists_all_supported_tools():
    from missioncrew.runtime.adapters import KNOWN_CLIS, detect_report
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


def test_shared_dialog_headers_do_not_duplicate_bottom_cancel_actions(client):
    html = client.get("/").text
    # Form dialogs and alert/confirm/prompt dialogs already render their close/cancel
    # action in the footer; their headers should contain only the title.
    assert '<div class="dlg-head"><strong id="fdlg-title"></strong></div>' in html
    assert '<div class="dlg-head"><strong id="udlg-title"></strong></div>' in html
    assert 'onclick="fdlg.close()">取消</button></div>' not in html
    assert 'onclick="_udlgClose(null)">关闭</button></div>' not in html


def test_tools_endpoint_merges_registration_state(client):
    rows = client.get("/api/backends/tools").json()
    by_id = {r["id"]: r for r in rows}
    # 内置工具矩阵全部列出;种子里的 mock 工具作为已注册项附加
    assert "claude" in by_id and "eco-1" in by_id
    assert by_id["eco-1"]["registered"] is True
    assert [r["installed"] for r in rows] == sorted(
        (r["installed"] for r in rows), reverse=True)
    for r in rows:  # 运行时页不暴露档位/成本/能力
        assert "tier" not in r and "cost_per_run" not in r and "capabilities" not in r


def test_global_settings_exposes_new_project_role_templates(client):
    html = client.get("/").text
    js = client.get("/assets/js/settings-runtime.js").text
    assert 'id="global-role-table"' in html
    assert "第一项是新项目的默认主控" in html
    assert "/api/role-templates/reorder" in js
    assert "editGlobalRoleTemplate" in js


def test_project_role_form_can_import_global_template(client):
    html = client.get("/").text
    js = client.get("/assets/js/roles.js").text
    assert "新增角色时可从全局角色模板导入" in html
    assert "从全局角色模板导入（可选）" in js
    assert "importGlobalRoleTemplate" in js
    assert "globalRoleTemplates().find" in js
    assert "项目中已存在角色" in js


# ---- 模型清单来自 runtime(仿 Multica 动态发现) ----

def test_backend_models_endpoint_merges_ladder_and_runtime(client, seeded, monkeypatch):
    from missioncrew.runtime import adapters
    seeded.put_backend(Backend(
        id="laddered", name="laddered", adapter="mock",
        models=[{"name": "small", "tier": "economy", "cost": 1}]))
    monkeypatch.setattr(adapters, "list_runtime_models",
                        lambda b, timeout=25: ["dyn/alpha", "dyn/beta"])
    d = client.get("/api/backends/laddered/models").json()
    assert {m["name"] for m in d["configured"]} == {"small"}   # 配置阶梯保留
    assert d["discovered"] == ["dyn/alpha", "dyn/beta"]        # runtime 动态目录
    assert client.get("/api/backends/ghost/models").status_code == 404


def test_claude_catalog_lists_concrete_model_ids():
    """claude 无枚举命令,静态目录须包含别名和具体型号(对齐 Multica)。"""
    from missioncrew.runtime import adapters
    models = adapters.list_runtime_models(
        Backend(id="c", name="c", adapter="claude_code"))
    assert models[:3] == ["haiku", "sonnet", "opus"]          # 稳定别名在前
    assert {"claude-sonnet-5", "claude-fable-5", "claude-opus-4-8"} <= set(models)


def test_save_role_accepts_runtime_discovered_model(client, seeded, monkeypatch):
    from missioncrew.runtime import adapters
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
    # 空模型 = CLI 默认,即使配置阶梯非空且不含空名条目也总是合法
    cli_default = client.post("/api/roles", json={
        "id": "cli-default", "project_id": "webshop", "runtime_id": "laddered"})
    assert cli_default.status_code == 200
    assert cli_default.json()["model"] == ""


def test_save_role_rejects_disabled_runtime(client, seeded):
    seeded.put_backend(Backend(id="paused", name="停用中", adapter="mock",
                               enabled=False))
    r = client.post("/api/roles", json={
        "id": "on-paused", "project_id": "webshop", "runtime_id": "paused"})
    assert r.status_code == 400 and "已停用" in r.json()["detail"]


# ---- 角色排序:项目内手工顺序,影响设置页/侧栏/名册 ----

def test_seeded_roles_follow_template_order_not_alphabetical(seeded):
    assert [r.id for r in seeded.list_roles("webshop")][:3] == ["lead", "dev", "reviewer"]


def test_reorder_roles_api(client, seeded):
    ids = [r.id for r in seeded.list_roles("webshop")]
    ids.append(ids.pop(0))       # 第一个角色移到末尾
    assert client.post("/api/roles/reorder",
                       json={"project_id": "webshop", "ids": ids}).status_code == 200
    assert [x.id for x in seeded.list_roles("webshop")] == ids
    # 不完整或含未知 id:整体拒绝,顺序不变
    bad = client.post("/api/roles/reorder", json={"project_id": "webshop", "ids": ids[:-1]})
    ghost = client.post("/api/roles/reorder",
                        json={"project_id": "webshop", "ids": [*ids[:-1], "ghost"]})
    assert bad.status_code == 400 and ghost.status_code == 400
    assert [x.id for x in seeded.list_roles("webshop")] == ids


def test_new_role_appended_and_edit_keeps_position(client, seeded):
    p = {"project_id": "webshop", "runtime_id": "std-1", "model": "pro"}
    client.post("/api/roles", json={"id": "newbie", **p})
    assert seeded.list_roles("webshop")[-1].id == "newbie"
    # 编辑已有角色(请求不带 sort_order)不改变它的位置
    first = seeded.list_roles("webshop")[0]
    client.post("/api/roles", json={"id": first.id, "name": "改名", **p})
    roles = seeded.list_roles("webshop")
    assert roles[0].id == first.id and roles[0].name == "改名"


# ---- effort(推理力度):仅支持的 runtime 可配,并注入本次执行 ----

def test_role_effort_saved_only_for_supporting_runtime(client, seeded):
    p = {"project_id": "webshop", "runtime_id": "std-1", "model": "pro"}
    # 种子后端是 mock 适配器,支持 low/medium/high
    ok = client.post("/api/roles", json={"id": "deep", "effort": "high", **p})
    assert ok.status_code == 200 and ok.json()["effort"] == "high"
    bad = client.post("/api/roles", json={"id": "deep2", "effort": "extreme", **p})
    assert bad.status_code == 400 and "effort" in bad.json()["detail"]
    # 不支持 effort 的适配器:非空拒绝,空值(CLI 默认)放行
    seeded.put_backend(Backend(id="plain", name="plain", adapter="opencode"))
    deny = client.post("/api/roles", json={
        "id": "on-plain", "project_id": "webshop", "runtime_id": "plain",
        "effort": "high"})
    assert deny.status_code == 400 and "不支持 effort" in deny.json()["detail"]
    empty = client.post("/api/roles", json={
        "id": "on-plain", "project_id": "webshop", "runtime_id": "plain"})
    assert empty.status_code == 200 and empty.json()["effort"] == ""


def test_role_effort_flows_into_execution_config(seeded):
    seeded.put_role(Role(id="deep", project_id="webshop", name="深想",
                         runtime_id="std-1", model="pro", effort="high"))
    chat = ChatEngine(seeded, max_workers=2)
    channel = seeded.get_channel("general")
    role = seeded.get_role("webshop", "deep")
    backend, reason = chat._pick_backend(channel, role)
    assert "effort=high" in reason
    cfg = chat._assemble(channel, role, backend, msg_id=0)
    assert cfg.effort == "high"
    assert "/effort=high" in cfg.prompt          # 固定执行组合对角色可见


def test_effort_rendered_into_cli_commands():
    from missioncrew.runtime import adapters
    claude = adapters.render_command(adapters.DEFAULT_COMMANDS["claude_code"],
                                     "work", "opus", "/docs", effort="high")
    assert claude[claude.index("--effort") + 1] == "high"
    codex = adapters.render_command(adapters.DEFAULT_COMMANDS["codex"],
                                    "work", "gpt-test", "/docs", effort="xhigh")
    assert codex[codex.index("-c") + 1] == "model_reasoning_effort=xhigh"
    # 空 effort:占位符连同紧邻标志一起移除,回到 CLI 默认
    plain = adapters.render_command(adapters.DEFAULT_COMMANDS["claude_code"],
                                    "work", "opus", "/docs")
    assert "--effort" not in plain and all("{effort}" not in t for t in plain)
    plain_codex = adapters.render_command(adapters.DEFAULT_COMMANDS["codex"],
                                          "work", "", "")
    assert "-c" not in plain_codex and plain_codex[-1] == "work"


def test_traits_endpoint_exposes_effort_options(client):
    d = client.get("/api/traits").json()
    assert d["effort_options"]["claude_code"] == ["low", "medium", "high", "xhigh", "max"]
    assert "mock" in d["effort_options"]


# ---- 执行组合在定义时固定,不做运行时路由 ----

def test_unbound_role_gets_clear_error(seeded):
    seeded.put_role(Role(id="ghost-rt", project_id="webshop", name="幽灵",
                         capabilities=["multimodal"]))   # 异常状态:未绑定 runtime
    chat = ChatEngine(seeded, max_workers=2)
    backend, reason = chat._pick_backend(seeded.get_channel("general"),
                                         seeded.get_role("webshop", "ghost-rt"))
    assert backend is None and "未绑定 runtime" in reason


def test_set_role_runtime_action_no_longer_supported(seeded):
    """主控只在预定义角色中选人,不再有改绑 runtime 的控制动作。"""
    chat = ChatEngine(seeded, max_workers=2)
    before = seeded.get_role("webshop", "dev")
    reply = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"set_role_runtime","role":"dev",'
        '"runtime":"exp-1","model":"ultra"}</missioncrew-action>',
        root_id=1, depth=0)
    assert "不支持的动作" in reply
    after = seeded.get_role("webshop", "dev")
    assert (after.runtime_id, after.model) == (before.runtime_id, before.model)
