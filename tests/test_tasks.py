"""Issue 化 Task：编辑、状态简报与 Channel/Lead 派发。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import board_sources
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.tasks import create_task, dispatch_task, update_task
from missioncrew.collab.workspace import write_task_files
from missioncrew.core.models import Channel, Task
from missioncrew.core.store import Store


def test_task_api_supports_issue_fields_updates_and_briefs(seeded):
    seeded.put_channel(Channel(
        id="webshop:delivery", name="交付", project_id="webshop"))
    with TestClient(create_app()) as client:
        created = client.post("/api/tasks", json={
            "project_id": "webshop",
            "title": "重做结算页",
            "summary": "统一移动端与桌面端结算体验",
            "body": "## 验收\n\n- 两端共用状态模型",
            "labels": ["checkout", "ui"],
            "channel_ids": ["general", "webshop:delivery"],
        })
        assert created.status_code == 200, created.text
        task = created.json()
        # 状态即标签:创建默认落 status: 待处理,排在标签首位
        assert task["labels"] == ["status: 待处理", "checkout", "ui"]
        assert "status" not in task
        assert task["channel_ids"] == ["general", "webshop:delivery"]
        assert "stages" not in task and "task_type" not in task

        edited = client.patch(f"/api/tasks/{task['id']}", json={
            "snapshot_updated_at": task["updated_at"],
            "title": "重做结算体验",
            "summary": "先统一状态模型",
            "body": task["body"],
            "labels": ["checkout"],
            "channel_ids": ["webshop:delivery"],
            "status": "处理中",
        })
        assert edited.status_code == 200, edited.text
        edited_task = edited.json()["task"]
        assert edited_task["title"] == "重做结算体验"
        assert edited_task["channel_ids"] == ["webshop:delivery"]
        assert edited_task["labels"] == ["status: 处理中", "checkout"]

        stale = client.patch(f"/api/tasks/{task['id']}", json={
            "snapshot_updated_at": task["updated_at"],
            "title": "覆盖别人的修改",
        })
        assert stale.status_code == 409

        briefed = client.post(f"/api/tasks/{task['id']}/briefs", json={
            "status": "已阻塞",
            "content": "等待结算 API 字段冻结。",
        })
        assert briefed.status_code == 200
        payload = briefed.json()
        assert "status: 已阻塞" in payload["task"]["labels"]
        assert payload["briefs"][0]["status"] == "已阻塞"
        assert payload["briefs"][0]["content"] == "等待结算 API 字段冻结。"
        assert payload["task"]["updated_at"] > edited_task["updated_at"]


def test_task_update_sets_single_properties_without_touching_others(seeded):
    with TestClient(create_app()) as client:
        task = client.post("/api/tasks", json={
            "project_id": "webshop", "title": "订单导出",
            "labels": ["export", "owner: 张三", "owner: 李四", "priority: 低"],
        }).json()

        edited = client.patch(f"/api/tasks/{task['id']}", json={
            "snapshot_updated_at": task["updated_at"],
            # 多值属性整组替换、单值清空、新属性追加;status 也是属性
            "properties": {"owner": ["王五"], "priority": None,
                           "area": "订单", "status": "处理中"},
        })
        assert edited.status_code == 200, edited.text
        task = edited.json()["task"]
        assert task["labels"] == [
            "status: 处理中", "export", "owner: 王五", "area: 订单"]

        for bad in ({"owner:x": "a"}, {"status": ["a", "b"]}, {"owner": 1},
                    ["owner"]):
            rejected = client.patch(f"/api/tasks/{task['id']}", json={
                "snapshot_updated_at": task["updated_at"], "properties": bad})
            assert rejected.status_code in (400, 422), bad


def test_tasks_sort_by_latest_edit_or_progress_for_existing_data(seeded):
    tasks = [
        Task(id="t_recent-created", project_id="webshop", title="最近创建",
             channel_ids=["general"], created_at=30.0, updated_at=30.0),
        Task(id="t_recent-edit", project_id="webshop", title="最近编辑",
             channel_ids=["general"], created_at=10.0, updated_at=40.0),
        Task(id="t_recent-progress", project_id="webshop", title="最近进展",
             channel_ids=["general"], created_at=5.0, updated_at=20.0),
    ]
    for task in tasks:
        seeded._put("tasks", task.id, task.to_dict())
    brief = seeded.add_task_brief(
        "t_recent-progress", "human", "human", "旧数据中的新进展", "open")
    seeded._execute(
        "UPDATE task_briefs SET created_at=50 WHERE id=?", (brief["id"],))

    ordered = [
        task for task in seeded.list_tasks("webshop")
        if task.id.startswith("t_recent-")
    ]
    assert [task.id for task in ordered] == [
        "t_recent-progress", "t_recent-edit", "t_recent-created"]
    assert ordered[0].updated_at == 50.0
    assert seeded.get_task("t_recent-progress").updated_at == 50.0

    with TestClient(create_app()) as client:
        listed = [
            task for task in client.get(
                "/api/projects/webshop/tasks?scope=all").json()["tasks"]
            if task["id"].startswith("t_recent-")
        ]
    assert [task["id"] for task in listed] == [
        "t_recent-progress", "t_recent-edit", "t_recent-created"]


def test_task_archive_hides_and_freezes_until_restored(seeded, tmp_path):
    with TestClient(create_app()) as client:
        task = client.post("/api/tasks", json={
            "project_id": "webshop",
            "title": "待归档任务",
            "channel_ids": ["general"],
        }).json()
        workspace = tmp_path / "tasks"
        write_task_files(seeded, "webshop", workspace)
        snapshot = workspace / f"{task['id']}.md"
        assert snapshot.is_file()

        archived = client.post(f"/api/tasks/{task['id']}/archive")
        assert archived.status_code == 200
        archived_task = archived.json()["task"]
        assert archived_task["archived"] is True
        assert archived_task["archived_at"] > 0
        assert archived_task["updated_at"] > task["updated_at"]
        assert task["id"] not in {
            item.id for item in seeded.list_tasks(
                "webshop", include_archived=False)}

        write_task_files(seeded, "webshop", workspace)
        assert not snapshot.exists()
        assert client.patch(f"/api/tasks/{task['id']}", json={
            "snapshot_updated_at": archived_task["updated_at"],
            "title": "归档后不应修改",
        }).status_code == 409
        assert client.post(f"/api/tasks/{task['id']}/briefs", json={
            "content": "归档后不应追加",
        }).status_code == 409
        assert client.post(
            f"/api/tasks/{task['id']}/process", json={"message": ""}
        ).status_code == 409

        restored = client.post(f"/api/tasks/{task['id']}/restore")
        assert restored.status_code == 200
        restored_task = restored.json()["task"]
        assert restored_task["archived"] is False
        assert restored_task["archived_at"] == 0
        assert restored_task["updated_at"] > archived_task["updated_at"]
        assert seeded.list_tasks("webshop")[0].id == task["id"]
        assert client.post(f"/api/tasks/{task['id']}/briefs", json={
            "content": "恢复后可以继续推进",
        }).status_code == 200


def test_processing_task_posts_only_to_first_bound_channel_and_triggers_lead(seeded):
    seeded.put_channel(Channel(
        id="webshop:delivery", name="交付", project_id="webshop"))
    task = create_task(
        seeded, "webshop", title="发布结算改版",
        summary="准备灰度发布", body="确认监控与回滚方案；@[dev] 只是正文引用。",
        channel_ids=["webshop:delivery", "general"],
    )
    chat = ChatEngine(seeded, max_workers=2)
    sent, brief = dispatch_task(
        seeded, chat, task, message="优先确认回滚开关。")
    chat.wait_idle()

    # 多个绑定频道也只发一条:未指定时取首个绑定频道
    assert [item["channel_id"] for item in sent] == ["webshop:delivery"]
    assert seeded.list_messages("general") == []
    assert brief["status"] == "处理中"
    assert "交付" in brief["content"]
    assert seeded.get_task(task.id).status == "处理中"
    message = seeded.get_message(sent[0]["message_id"])
    assert f"@lead 请处理 Task [{task.id}" in message["content"]
    assert "优先确认回滚开关" in message["content"]
    assert json.loads(message["mentions"]) == ["lead"]
    runs = seeded._query(
        "SELECT role_id FROM chat_runs WHERE trigger_message_id=?",
        (sent[0]["message_id"],),
    )
    assert [row["role_id"] for row in runs] == ["lead"]


def test_processing_task_to_selected_channel_binds_it(seeded):
    seeded.put_channel(Channel(
        id="webshop:delivery", name="交付", project_id="webshop"))
    seeded.put_channel(Channel(
        id="webshop:old", name="旧频道", project_id="webshop", archived=True))
    task = create_task(
        seeded, "webshop", title="补充回归用例", channel_ids=["general"])
    with TestClient(create_app()) as client:
        rejected = client.post(f"/api/tasks/{task.id}/process",
                               json={"channel_id": "webshop:old"})
        assert rejected.status_code == 409 and "已归档" in rejected.json()["detail"]
        assert seeded.get_task(task.id).channel_ids == ["general"]

        response = client.post(f"/api/tasks/{task.id}/process",
                               json={"message": "", "channel_id": "delivery"})
        assert response.status_code == 200
        body = response.json()
    assert [item["channel_id"] for item in body["sent"]] == ["webshop:delivery"]
    # 选中未绑定频道时追加绑定,原有绑定保留,但原频道不再收到消息
    assert body["task"]["channel_ids"] == ["general", "webshop:delivery"]
    assert seeded.list_messages("general") == []
    delivered = seeded.list_messages("webshop:delivery")
    assert delivered[0]["id"] == body["sent"][0]["message_id"]
    assert delivered[0]["author_type"] == "human"


def test_processing_task_with_mentions_dispatches_named_role_directly(seeded):
    task = create_task(
        seeded, "webshop", title="修复登录", channel_ids=["general"])
    chat = ChatEngine(seeded, max_workers=2)
    message = "@dev 优先处理,今天上线"
    sent, brief = dispatch_task(
        seeded, chat, task, message=message,
        mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    chat.wait_idle()

    stored = seeded.get_message(sent[0]["message_id"])
    assert stored["content"].startswith(message)
    assert f"请处理 Task [{task.id}" in stored["content"]
    assert json.loads(stored["mentions"]) == ["dev"]
    assert "@dev" in brief["content"]
    runs = seeded._query("SELECT role_id FROM chat_runs ORDER BY id")
    # 人类直接点名:只启动 dev,结果不再自动交回主控
    assert [row["role_id"] for row in runs] == ["dev"]


def test_processing_task_with_multiple_mentions_collapses_to_lead(seeded):
    task = create_task(
        seeded, "webshop", title="联调结算", channel_ids=["general"])
    chat = ChatEngine(seeded, max_workers=2)
    message = "@dev @tester 一起排查"
    sent, _ = dispatch_task(
        seeded, chat, task, message=message,
        mention_spans=[{"role_id": "dev", "start": 0, "end": 4},
                       {"role_id": "tester", "start": 5, "end": 12}])
    chat.wait_idle()
    runs = seeded._query(
        "SELECT role_id FROM chat_runs WHERE trigger_message_id=?",
        (sent[0]["message_id"],))
    assert [row["role_id"] for row in runs] == ["lead"]


def test_processing_task_rejects_disabled_mentioned_role(seeded):
    dev = seeded.get_role("webshop", "dev")
    dev.enabled = False
    seeded.put_role(dev)
    task = create_task(
        seeded, "webshop", title="不可派发", channel_ids=["general"])
    chat = ChatEngine(seeded)
    with pytest.raises(ValueError, match="角色已停用"):
        dispatch_task(
            seeded, chat, task, message="@dev 处理",
            mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    assert seeded.get_task(task.id).status == "待处理"


def test_task_auto_rule_dispatches_matching_new_task(seeded):
    project = seeded.get_project("webshop")
    # 旧规则 label 字段读取即迁移为同义表达式;新规则直接写标签表达式
    project.task_auto_rules = [
        {"label": "sync", "role_ids": ["dev"],
         "prompt": "请分析并给出处理建议", "enabled": True},
        {"query": "ignored", "role_ids": [], "prompt": "", "enabled": True},
    ]
    seeded.put_project(project)
    assert seeded.get_project("webshop").task_auto_rules[0].query == "sync"
    chat = ChatEngine(seeded, max_workers=2)

    from missioncrew.collab.tasks import auto_process_task
    task = create_task(
        seeded, "webshop", title="外部同步任务", labels=["Sync"],
        channel_ids=["general"])
    result = auto_process_task(seeded, chat, task)
    chat.wait_idle()

    assert result and result["rule_query"] == "sync"
    assert seeded.get_task(task.id).status == "处理中"
    stored = seeded.get_message(result["sent"][0]["message_id"])
    assert stored["author_type"] == "automation"
    assert stored["content"].startswith("@dev ")
    assert "请分析并给出处理建议" in stored["content"]
    runs = seeded._query("SELECT role_id FROM chat_runs ORDER BY id")
    assert [row["role_id"] for row in runs] == ["dev"]

    # 不命中或规则停用时不派发
    project.task_auto_rules[0]["enabled"] = False
    seeded.put_project(seeded.get_project("webshop"))
    plain = create_task(
        seeded, "webshop", title="普通任务", labels=["other"],
        channel_ids=["general"])
    assert auto_process_task(seeded, chat, plain) is None
    assert seeded.get_task(plain.id).status == "待处理"


def test_task_api_create_applies_auto_rule(seeded):
    project = seeded.get_project("webshop")
    project.task_auto_rules = [
        {"query": "auto & !status: 已完成", "role_ids": [],
         "prompt": "按默认流程处理", "enabled": True}]
    seeded.put_project(project)
    with TestClient(create_app()) as client:
        created = client.post("/api/tasks", json={
            "project_id": "webshop", "title": "自动流转",
            "labels": ["auto"], "channel_ids": ["general"],
        }).json()
        assert created["auto_dispatch"]["rule_query"] == "auto & !status: 已完成"
        assert created["auto_dispatch"]["sent"]
        detail = client.get(f"/api/tasks/{created['id']}").json()
        assert "status: 处理中" in detail["task"]["labels"]

        rules = client.get("/api/overview").json()["projects"][0].get(
            "task_auto_rules")
        assert rules and rules[0]["query"] == "auto & !status: 已完成"


def test_task_auto_rule_with_mentions_dispatches_like_composer(seeded):
    """新版规则带结构化提及:派发与人工 @ 完全同构,role_ids 从提及派生。"""
    project = seeded.get_project("webshop")
    project.task_auto_rules = [
        {"query": "alert", "prompt": "@dev 请先分析影响面",
         "mentions": [{"role_id": "dev", "start": 0, "end": 4}],
         "enabled": True}]
    seeded.put_project(project)
    rule = seeded.get_project("webshop").task_auto_rules[0]
    assert rule.role_ids == ["dev"]   # 派生视图与提及一致

    chat = ChatEngine(seeded, max_workers=2)
    from missioncrew.collab.tasks import auto_process_task
    task = create_task(
        seeded, "webshop", title="线上告警", labels=["alert"],
        channel_ids=["general"])
    result = auto_process_task(seeded, chat, task)
    chat.wait_idle()

    assert result and result["rule_query"] == "alert"
    stored = seeded.get_message(result["sent"][0]["message_id"])
    # 提及走 mention_spans 通道:提示词保持原文原位,不再额外加 @前缀
    assert stored["content"].startswith("@dev 请先分析影响面")
    assert stored["content"].count("@dev") == 1
    runs = seeded._query("SELECT role_id FROM chat_runs ORDER BY id")
    assert [row["role_id"] for row in runs] == ["dev"]


def test_project_task_board_filters_persist_and_normalize(seeded):
    with TestClient(create_app()) as client:
        saved = client.post("/api/projects", json={
            "id": "webshop", "name": "网店",
            "task_board_filters": [
                {"query": "Sync"},
                {"title": "指派给我", "query": "owner: 张三"},
            ],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["task_board_filters"] == [
            {"title": "Sync", "query": "Sync", "color": ""},
            {"title": "指派给我", "query": "owner: 张三", "color": ""},
        ]

        # 未传该字段时保留现值
        kept = client.post("/api/projects", json={
            "id": "webshop", "name": "网店"}).json()
        assert [f["query"] for f in kept["task_board_filters"]] == [
            "Sync", "owner: 张三"]

        # 表达式非法时整体拒绝
        bad = client.post("/api/projects", json={
            "id": "webshop", "name": "网店",
            "task_board_filters": [{"query": "(bug"}],
        })
        assert bad.status_code == 400


def test_legacy_task_label_boards_migrate_to_filters(seeded):
    project = seeded.get_project("webshop")
    raw = project.to_dict()
    raw.pop("task_board_filters", None)
    raw["task_label_boards"] = ["Sync", "alert"]
    from missioncrew.core.models import Project
    migrated = Project.from_dict(raw)
    assert migrated.task_board_filters == [
        {"title": "Sync", "query": "Sync", "color": ""},
        {"title": "alert", "query": "alert", "color": ""},
    ]


def test_processing_task_rejects_disabled_orchestrator(seeded):
    lead = seeded.get_role("webshop", "lead")
    lead.enabled = False
    seeded.put_role(lead)
    task = create_task(
        seeded, "webshop", title="无法调度", channel_ids=["general"])
    chat = ChatEngine(seeded)

    with pytest.raises(ValueError, match="项目主控角色已停用"):
        dispatch_task(seeded, chat, task)
    assert seeded.get_task(task.id).status == "待处理"
    assert seeded.list_messages("general") == []
    assert seeded._query("SELECT * FROM chat_runs") == []


def test_task_edit_empty_channel_list_clears_binding(seeded):
    task = create_task(
        seeded, "webshop", title="检查库存", channel_ids=["general"])
    updated = update_task(
        seeded, task, snapshot_updated_at=task.updated_at,
        changes={"channel_ids": []},
    )
    # 频道绑定可选:显式空列表即解除绑定;派发时按 general 回退
    assert updated.channel_ids == []
    chat = ChatEngine(seeded, max_workers=2)
    sent, _ = dispatch_task(seeded, chat, updated)
    chat.wait_idle()
    assert [item["channel_id"] for item in sent] == ["general"]


def test_task_delete_moves_task_and_briefs_to_recycle_bin_and_restores(seeded):
    with TestClient(create_app()) as client:
        created = client.post("/api/tasks", json={
            "project_id": "webshop",
            "title": "清理旧结算任务",
            "summary": "验证可恢复删除",
            "body": "保留正文与状态简报。",
            "labels": ["cleanup"],
            "channel_ids": ["general"],
        }).json()
        brief = client.post(f"/api/tasks/{created['id']}/briefs", json={
            "status": "in_progress",
            "content": "已确认删除范围。",
        }).json()["brief"]

        deleted = client.delete(f"/api/tasks/{created['id']}")
        assert deleted.status_code == 200, deleted.text
        item = deleted.json()["recycle_item"]
        assert deleted.json()["deleted"] is True
        assert item["resource_type"] == "task"
        assert item["resource_id"] == created["id"]
        assert client.get(f"/api/tasks/{created['id']}").status_code == 404
        assert seeded.list_task_briefs(created["id"]) == []

        restored = client.post(
            f"/api/projects/webshop/recycle-bin/{item['id']}/restore")
        assert restored.status_code == 200, restored.text
        detail = client.get(f"/api/tasks/{created['id']}").json()
        assert detail["task"]["title"] == created["title"]
        assert detail["task"]["body"] == "保留正文与状态简报。"
        assert detail["briefs"][0]["content"] == brief["content"]
        assert detail["briefs"][0]["created_at"] == brief["created_at"]
        assert client.get(
            "/api/projects/webshop/recycle-bin").json()["items"] == []


def test_legacy_staged_task_is_migrated_to_issue(store):
    store.put_channel(Channel(id="demo:general", name="General", project_id="demo"))
    legacy = {
        "id": "t_legacy", "project_id": "demo", "title": "旧任务",
        "description": "旧描述第一行\n\n更多内容", "task_type": "bug",
        "labels": ["legacy"], "risk": "high", "security_level": 1,
        "status": "awaiting_approval", "stage_index": 2,
        "stages": [{"name": "verify", "status": "awaiting_approval"}],
        "created_at": 10.0, "updated_at": 20.0,
    }
    store._put("tasks", legacy["id"], legacy)

    migrated = Store(store.path).get_task("t_legacy")
    assert migrated.summary == "旧描述第一行"
    assert migrated.body == legacy["description"]
    # 旧枚举状态迁成状态标签;频道绑定已可选,不再强制补默认频道
    assert migrated.labels == ["status: 处理中", "legacy"]
    assert migrated.status == "处理中"
    assert migrated.channel_ids == []
    assert set(migrated.to_dict()) == {
        "id", "project_id", "title", "source_id", "external_id",
        "summary", "body", "labels", "channel_ids", "url", "meta",
        "archived", "archived_at", "external_updated_at", "created_at", "updated_at",
    }


def test_legacy_board_source_cards_and_filters_migrate(store):
    """旧版数据源内嵌 cards 迁入 tasks 表;看板筛选列的裸状态词改写为状态标签。"""
    store._put("board_sources", "demo:gitcode", {
        "id": "demo:gitcode", "project_id": "demo", "name": "GitCode",
        "description": "", "columns": [
            {"key": "open", "title": "待处理", "color": ""},
            {"key": "done", "title": "已完成", "color": ""}],
        "cards": [{"id": "1", "title": "条目", "status": "done",
                   "labels": ["bug"], "updated_at": 5.0,
                   "url": "https://x/1", "meta": ["m"]}],
        "created_by_role_id": "", "created_at": 1.0, "updated_at": 2.0,
    })
    store._put("boards", "demo:issues", {
        "id": "demo:issues", "project_id": "demo", "kind": "taskboard",
        "source": "gitcode", "name": "issues", "description": "",
        "layout": [], "created_by_role_id": "",
        "created_at": 1.0, "updated_at": 1.0,
        "filters": [{"title": "待处理", "query": "待处理", "color": ""},
                    {"title": "缺陷", "query": "已完成 & bug", "color": ""}],
    })

    reopened = Store(store.path)
    tasks = reopened.list_tasks("demo", source_id="gitcode")
    assert len(tasks) == 1 and tasks[0].external_id == "1"
    assert tasks[0].labels == ["status: 已完成", "bug"]
    assert tasks[0].url == "https://x/1" and tasks[0].meta == ["m"]
    record = reopened.get_board_datasource("demo:gitcode")
    assert record.status_values == [
        {"value": "待处理", "color": ""}, {"value": "已完成", "color": ""}]
    board = reopened.get_board("demo:issues")
    assert [f["query"] for f in board.filters] == [
        "status: 待处理", "status: 已完成 & bug"]
    # 迁移可重入:再次打开不重复建任务
    assert len(Store(store.path).list_tasks("demo", source_id="gitcode")) == 1


WEB_JS = Path(__file__).resolve().parents[1] / "missioncrew" / "web" / "js"

# 在 node 里装载 markdown.js 与 router.js,替换掉 openTask/applyRoute 等页面依赖,
# 只观察资源链接点击走了"弹窗"还是"路由跳转"
_RESOURCE_LINK_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const calls = [];
const context = {
  location: { origin: "http://localhost", pathname: "/resources/webshop/channels/general" },
  window: { addEventListener() {} },
  localStorage: { getItem: () => null },
  history: { pushState: (_, __, url) => calls.push(["push", url]) },
  toast: message => calls.push(["toast", message]),
  overview: { projects: [{ id: "webshop" }, { id: "other" }] },
  currentProject: "webshop",
  currentTab: "chat",
};
vm.createContext(context);
for (const name of ["markdown.js", "router.js"])
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
context.applyRoute = async () => calls.push(["route"]);
context.openTask = async (id, updateRoute) => calls.push(["task", id, updateRoute]);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const results = cases.map(([tab, href, modifier]) => {
  calls.length = 0;
  context.currentTab = tab;
  let prevented = false;
  const event = { ctrlKey: modifier, metaKey: false, shiftKey: false, altKey: false,
                  preventDefault: () => { prevented = true; } };
  const returned = context.openMissionCrewResourceLink(event, href);
  return { returned, prevented, calls: calls.slice() };
});
process.stdout.write(JSON.stringify(results));
"""


def test_task_resource_link_pops_up_details_without_leaving_page():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser router")
    cases = [
        ("chat", "/resources/webshop/tasks/T-7", False),
        ("board", "/resources/webshop/tasks/T-7", False),
        ("chat", "/resources/other/tasks/T-9", False),
        ("chat", "/resources/webshop/channels/dev", False),
        ("chat", "/resources/webshop/tasks/T-7", True),
    ]
    result = subprocess.run(
        [node, "-e", _RESOURCE_LINK_SCRIPT, str(WEB_JS)],
        input=json.dumps(cases), text=True, capture_output=True, check=True)
    in_chat, on_board, other_project, channel, modified = json.loads(result.stdout)

    # 本项目 Task:原地弹出详情,不 pushState、不走路由;只有看板页才把 Task 写进 URL
    assert in_chat == {"returned": False, "prevented": True,
                       "calls": [["task", "T-7", False]]}
    assert on_board["calls"] == [["task", "T-7", True]]
    # 其他项目的 Task 与非 Task 资源照旧跳转
    assert other_project["calls"] == [["push", "/resources/other/tasks/T-9"], ["route"]]
    assert channel["calls"] == [["push", "/resources/webshop/channels/dev"], ["route"]]
    # 带修饰键交还浏览器默认行为(新标签页打开完整页面)
    assert modified == {"returned": True, "prevented": False, "calls": []}


def test_saving_channels_never_binds_unbound_tasks(seeded):
    # 数据源同步来的任务没有频道;之后新建、修改、归档/恢复任何频道都不能把它挂上去
    board_sources.sync_source_tasks(
        seeded, "webshop", "gh", board_sources.validate_source_cards(
            [{"id": "gh-1", "title": "#1 同步来的 Issue", "updated_at": 1.0}]))
    synced = seeded.list_tasks("webshop", source_id="gh")[0]
    unbound = create_task(seeded, "webshop", title="人工解绑的任务")
    update_task(seeded, unbound, snapshot_updated_at=unbound.updated_at,
                changes={"channel_ids": []})
    assert synced.channel_ids == [] and seeded.get_task(unbound.id).channel_ids == []

    seeded.put_channel(Channel(id="webshop:review", name="review", project_id="webshop"))
    with TestClient(create_app()) as client:
        assert client.post("/api/chat/channels", json={
            "id": "release", "project_id": "webshop"}).status_code == 200
        assert client.patch("/api/chat/channels/webshop:review",
                            json={"purpose": "检视"}).status_code == 200
        assert client.post("/api/chat/channels/webshop:review/archive").status_code == 200
        assert client.post("/api/chat/channels/webshop:review/restore").status_code == 200

    assert seeded.get_task(synced.id).channel_ids == []
    assert seeded.get_task(unbound.id).channel_ids == []


def test_deleting_channel_unbinds_tasks_without_rebinding(seeded):
    seeded.put_channel(Channel(id="webshop:release", name="发版", project_id="webshop"))
    both = create_task(seeded, "webshop", title="两个频道",
                       channel_ids=["general", "webshop:release"])
    only = create_task(seeded, "webshop", title="只绑发版",
                       channel_ids=["webshop:release"])

    seeded.delete_channel("webshop:release")

    assert seeded.get_task(both.id).channel_ids == ["general"]
    assert seeded.get_task(only.id).channel_ids == []   # 不改绑到 general,派发时再回退


def test_project_task_list_filters_by_channel(seeded):
    seeded.put_channel(Channel(
        id="webshop:delivery", name="交付", project_id="webshop"))
    with TestClient(create_app()) as client:
        def create(title, channels):
            return client.post("/api/tasks", json={
                "project_id": "webshop", "title": title,
                "channel_ids": channels}).json()["id"]

        both = create("两个频道都绑定", ["general", "webshop:delivery"])
        only_general = create("只绑 general", ["general"])
        archived = create("已归档", ["webshop:delivery"])
        assert client.post(f"/api/tasks/{archived}/archive").status_code == 200

        def listed(query):
            data = client.get(f"/api/projects/webshop/tasks?{query}").json()
            return {task["id"] for task in data["tasks"]}, data["counts"]

        # channel 只取绑定该频道的任务,与 scope 叠加;counts 也按同一频道口径
        ids, counts = listed("scope=active&channel=webshop:delivery")
        assert ids == {both}
        assert counts == {"all": 2, "archived": 1, "active": 1}
        assert listed("scope=all&channel=webshop:delivery")[0] == {both, archived}
        assert {both, only_general} <= listed("channel=general")[0]
        assert listed("channel=webshop:missing")[0] == set()


# 频道关联 Task 按钮与弹层的渲染:装载真实的 tasks.js/channels.js,DOM 只留按钮与弹层两个节点
_CHANNEL_TASKS_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const element = () => {
  const classes = new Set(), attrs = {};
  return { hidden: true, innerHTML: "", title: "", style: {}, classes, attrs,
           classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name) },
           setAttribute: (name, value) => { attrs[name] = value; },
           getBoundingClientRect: () => ({ bottom: 40, right: 1180 }) };
};
const nodes = { "channel-tasks-btn": element(), "channel-tasks-pop": element() };
const store = {};
const context = {
  document: { getElementById: id => nodes[id] || null, addEventListener() {},
              querySelector: () => null },
  window: { innerWidth: 1280, innerHeight: 800, addEventListener() {} },
  localStorage: { getItem: key => key in store ? store[key] : null,
                  setItem: (key, value) => { store[key] = String(value); } },
  dlg: { addEventListener() {} },
  esc: value => String(value ?? "").replace(/[&<>"]/g,
    char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]),
  taskActivity: task => Number(task.updated_at || 0),
  projBoardSources: () => [],
  currentChan: "webshop:delivery", currentTab: "chat",
};
vm.createContext(context);
for (const name of ["tasks.js", "channels.js"])
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
const task = (id, title, status, updated_at) =>
  ({ id, title, summary: "", updated_at, labels: status ? [`status: ${status}`] : [] });
vm.runInContext(`channelTasks = { channelId: "webshop:delivery", tasks: ${JSON.stringify([
  task("t1", "旧的待处理", "待处理", 1), task("t2", "最新处理中", "处理中", 3),
  task("t3", "已经完成", "已完成", 2), task("t4", "<b>无状态</b>", "", 0)])} }`, context);
const snapshot = () => {
  const [button, pop] = [nodes["channel-tasks-btn"], nodes["channel-tasks-pop"]];
  return { button: { hidden: button.hidden, html: button.innerHTML, title: button.title,
                     active: button.classes.has("active"), expanded: button.attrs["aria-expanded"] },
           pop: { hidden: pop.hidden, html: pop.innerHTML, style: { ...pop.style } } };
};
const results = {};
const run = (code, key) => { vm.runInContext(code, context); results[key] = snapshot(); };
run("renderChannelTasks()", "closed");
let stopped = false;
context.clickEvent = { stopPropagation: () => { stopped = true; } };
run("toggleChannelTasksPopover(clickEvent)", "opened");
results.stopped = stopped;
run("setChannelTaskScope('all')", "all");
run("closeChannelTasksPopover()", "closedAgain");
results.saved = { ...store };
run("toggleChannelTasksPopover(); currentChan = 'general'; renderChannelTasks()", "otherChannel");
process.stdout.write(JSON.stringify(results));
"""


def test_channel_task_button_previews_count_and_pops_up_list():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser renderers")
    result = subprocess.run(
        [node, "-e", _CHANNEL_TASKS_SCRIPT, str(WEB_JS)],
        text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)

    # 标题行按钮只预览未完成数,弹层默认关闭
    closed = data["closed"]
    assert not closed["button"]["hidden"] and closed["button"]["html"] == "Task <b>3</b>"
    assert closed["button"]["title"] == "3 个未完成 · 共 4 个关联 Task"
    assert closed["button"]["expanded"] == "false" and closed["pop"]["hidden"]
    # 点击:不冒泡到标题行(那是展开/收起频道信息),弹层贴着按钮右下方打开
    opened = data["opened"]
    assert data["stopped"] and opened["button"]["active"] and opened["button"]["expanded"] == "true"
    assert not opened["pop"]["hidden"]
    assert opened["pop"]["style"] == {"top": "46px", "right": "100px", "maxHeight": "742px"}
    # 默认只列未完成的:按最近活动排序,状态用看板同款样式徽标,标题转义
    html = opened["pop"]["html"]
    assert "3 个未完成 · 共 4" in html
    assert html.index("最新处理中") < html.index("旧的待处理")
    assert '<span class="pill status-pill" style="--status-color:var(--accent)">处理中</span>' in html
    assert '<span class="pill status-pill" style="--status-color:var(--muted)">待处理</span>' in html
    assert "&lt;b&gt;无状态&lt;/b&gt;" in html and ">无状态</span>" in html
    assert "已经完成" not in html and "openTask(this.dataset.taskId, false)" in html
    # 切到全部后已完成的也列出,范围偏好按浏览器记住;关闭后弹层清空
    assert ('<span class="pill status-pill" style="--status-color:var(--ok)">已完成</span>'
            in data["all"]["pop"]["html"])
    assert data["saved"] == {"mc.channelTaskScope": "all"}
    assert data["closedAgain"]["pop"]["hidden"] and data["closedAgain"]["pop"]["html"] == ""
    assert not data["closedAgain"]["button"]["active"]
    # 切到没有关联 Task 的频道:按钮隐藏,打开着的弹层随之收起
    other = data["otherChannel"]
    assert other["button"]["hidden"] and other["pop"]["hidden"]


_STATUS_PILL_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const context = {
  localStorage: { getItem: () => null, setItem() {} },
  dlg: { addEventListener() {} },
  esc: value => String(value ?? "").replace(/[&<>"]/g,
    char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]),
  projBoardSources: () => [
    { id: "built-in", status_values: [{ value: "待处理", color: "var(--muted)" }] },
    { id: "pr-dev", status_values: [{ value: "正在开发", color: "#0969da" },
                                    { value: "已关闭", color: "" }] },
  ],
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1] + "/tasks.js", "utf8"), context);
process.stdout.write(JSON.stringify(vm.runInContext(`({
  custom: statusPillHtml("正在开发", "pr-dev"),
  noColor: statusPillHtml("已关闭", "pr-dev"),
  undeclared: statusPillHtml("处理中", "pr-dev"),
  builtin: statusPillHtml("待处理"),
  goneSource: statusPillHtml("待处理", "deleted-source"),
  options: taskStatusOptions("pr-dev"),
  fallbackOptions: taskStatusOptions("deleted-source"),
})`, context)));
"""


def test_status_pill_color_comes_from_task_source():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser renderers")
    result = subprocess.run(
        [node, "-e", _STATUS_PILL_SCRIPT, str(WEB_JS)],
        text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)

    # 自定义状态与内置状态同一规则:颜色取任务所属数据源的声明
    assert data["custom"] == (
        '<span class="pill status-pill" style="--status-color:#0969da">正在开发</span>')
    assert data["builtin"] == (
        '<span class="pill status-pill" style="--status-color:var(--muted)">待处理</span>')
    # 声明了没给颜色的与看板列一样用灰色;该源没声明的状态、源已删除时不着色
    assert "--status-color:var(--muted)" in data["noColor"]
    assert data["undeclared"] == '<span class="pill">处理中</span>'
    assert data["goneSource"] == '<span class="pill">待处理</span>'
    assert data["options"] == ["正在开发", "已关闭"]
    assert data["fallbackOptions"] == ["待处理", "处理中", "已阻塞", "已完成"]


# 「交给角色处理」在详情弹窗底部就地渲染与派发:装载真实 tasks.js,输入框/提及/接口用桩替换
_TASK_DISPATCH_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const element = () => ({ hidden: true, innerHTML: "", disabled: false, value: "",
  replaceChildren() { this.innerHTML = ""; this.cleared = true; } });
const nodes = { "dlg-dispatch": element() };
const calls = { api: [], bound: [], opened: [], toasts: [] };
const context = {
  document: { getElementById: id => nodes[id] || null },
  localStorage: { getItem: () => null, setItem() {} },
  dlg: { addEventListener() {} },
  esc: value => String(value ?? "").replace(/[&<>"]/g,
    char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]),
  projBoardSources: () => [],
  projChannels: () => [{ id: "general", name: "general" },
                       { id: "webshop:delivery", name: "delivery" }],
  channelIsGeneral: channel => channel.id === "general",
  peerModeProject: () => false,
  activateComposer() {},
  bindComposerEvents: (box, picker) => calls.bound.push([box, picker]),
  composerPayload: () => ({ content: "@dev 请看", mentions: [{ role_id: "dev", start: 0, end: 4 }] }),
  api: async (method, url, body) => {
    calls.api.push({ method, url, body, sendDisabled: nodes["task-dispatch-send"].disabled });
  },
  loadOverview: async () => {},
  toast: text => calls.toasts.push(text),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1] + "/tasks.js", "utf8"), context);
context.openTask = async (id, route) => calls.opened.push([id, route]);
const task = (labels, extra = {}) =>
  ({ id: "t1", archived: false, labels, channel_ids: ["webshop:delivery"], ...extra });
(async () => {
  const panel = nodes["dlg-dispatch"];
  const results = {};
  context.renderTaskDispatchPanel(task(["status: 待处理"]));
  results.open = { hidden: panel.hidden, html: panel.innerHTML };
  // 渲染出的输入框、频道选择与发送按钮换成可观察的桩节点
  Object.assign(nodes, { "task-dispatch-input": element(), "task-dispatch-send": element(),
                         "task-dispatch-channel": { value: "general" } });
  vm.runInContext("currentTaskId = 't1'", context);
  await Promise.all([context.submitProcessTask("t1"), context.submitProcessTask("t1")]);
  results.inputCleared = !!nodes["task-dispatch-input"].cleared;
  results.sendDisabledAfter = nodes["task-dispatch-send"].disabled;
  vm.runInContext("currentTaskId = null", context);
  await context.submitProcessTask("t1");
  for (const [key, labels, extra] of [["done", ["status: 已完成"], {}],
                                      ["archived", ["status: 待处理"], { archived: true }]]) {
    context.renderTaskDispatchPanel(task(labels, extra));
    results[key] = { hidden: panel.hidden, html: panel.innerHTML };
  }
  process.stdout.write(JSON.stringify({ ...results, calls }));
})();
"""


def test_task_detail_dispatches_from_inline_panel_without_extra_dialog():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser renderers")
    result = subprocess.run(
        [node, "-e", _TASK_DISPATCH_SCRIPT, str(WEB_JS)],
        text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)
    calls = data["calls"]

    # 未完成的 Task 在详情底部直接给出频道选择与输入框,默认选首个绑定频道
    assert not data["open"]["hidden"]
    html = data["open"]["html"]
    assert 'id="task-dispatch-channel"' in html and 'id="task-dispatch-input"' in html
    assert "selected>#delivery · 已绑定</option>" in html
    assert calls["bound"] == [["task-dispatch-input", "task-dispatch-picker"]]
    # 发送按钮在请求期间禁用:连按两次只派发一次,完成后清空输入、刷新详情;
    # 两条请求分别来自连按的那一对和弹窗关闭后的又一次发送
    expected = {"method": "POST", "url": "/api/tasks/t1/process", "sendDisabled": True,
                "body": {"message": "@dev 请看", "channel_id": "general",
                         "mentions": [{"role_id": "dev", "start": 0, "end": 4}]}}
    assert calls["api"] == [expected, expected]
    assert data["inputCleared"] and not data["sendDisabledAfter"]
    # 第二条派发发生在弹窗关闭后:不再重新弹出详情
    assert calls["opened"] == [["t1", False]]
    assert calls["toasts"][0] == "Task 已在 #general 派发给 @dev"
    # 已完成、已归档的 Task 不显示派发区
    for key in ("done", "archived"):
        assert data[key] == {"hidden": True, "html": ""}


def test_task_board_exposes_activity_order_and_archive_filter(seeded):
    with TestClient(create_app()) as client:
        html = client.get("/").text
        ui = client.get("/assets/js/ui.js").text
        tasks = client.get("/assets/js/tasks.js").text
        boards = client.get("/assets/js/boards.js").text
        css = client.get("/assets/css/app.css").text

    # 显示范围选择器在两块任务看板共用的工具条里,不再是内置看板独有的静态控件
    assert 'id="task-filter"' not in html and 'id="board"' in html
    assert 'class="tb-scope"' in boards
    assert "setTaskboardOption(this, 'scope', this.value)" in boards
    assert all(label in boards for label in (
        "活跃 Task", "全部 Task", "已归档 Task"))
    assert "taskActivity(right) - taskActivity(left)" in ui
    assert "function setTaskFilter" in tasks and "renderCustomBoards(true)" in tasks
    assert "function archiveTask" in tasks and "function restoreTask" in tasks
    assert "/archive" in tasks and "/restore" in tasks
    assert ".tb-filter-bar" in css and ".taskboard-host" in css and ".card.task-archived" in css


def test_external_update_time_accepts_unix_seconds_and_zoned_iso_only():
    clean = board_sources.clean_timestamp
    assert clean(None, "t") == clean("", "t") == clean(0, "t") == 0.0
    assert clean(1760860800, "t") == clean("1760860800", "t") == 1760860800.0
    assert clean("2026-10-19T08:00:00Z", "t") == clean("2026-10-19T16:00:00+08:00", "t")
    # 不带时区会差出数小时、毫秒会变成遥远的未来:都直接报错
    for bad, message in [("2026-10-19 16:00:00", "缺少时区"), ("昨天", "不是合法时间"),
                         (1760860800000, "不是毫秒"), (True, "Unix 秒"), (-1, "超出范围")]:
        with pytest.raises(ValueError, match=message):
            clean(bad, "t")


def test_sync_stores_card_time_as_external_update_time_apart_from_system_time(seeded):
    def sync(*cards):
        return board_sources.sync_source_tasks(
            seeded, "webshop", "gh", board_sources.validate_source_cards(list(cards)))

    sync({"id": "gh-1", "title": "Issue", "updated_at": 1000})
    task = seeded.list_tasks("webshop", source_id="gh")[0]
    # 卡片时间不再被写库时间覆盖;系统更新时间由 MissionCrew 自己记录
    assert task.external_updated_at == 1000.0 and task.updated_at > 1000.0
    system = task.updated_at
    assert sync({"id": "gh-1", "updated_at": 1000})["updated"] == 0
    assert seeded.get_task(task.id).updated_at == system
    # 只有外部时间变化也要写入;卡片不带 updated_at 时保留原值
    assert sync({"id": "gh-1", "updated_at": "1970-01-01T00:33:20+00:00"})["updated"] == 1
    assert sync({"id": "gh-1"})["updated"] == 0
    assert seeded.get_task(task.id).external_updated_at == 2000.0


def test_external_update_time_is_settable_but_system_time_is_not(seeded):
    task = create_task(seeded, "webshop", title="外部 Issue",
                       external_updated_at="1970-01-01T00:16:40Z")
    assert task.external_updated_at == 1000.0
    with pytest.raises(ValueError, match="缺少时区"):
        update_task(seeded, task, snapshot_updated_at=task.updated_at,
                    changes={"external_updated_at": "2026-10-19 08:00"})
    update_task(seeded, task, snapshot_updated_at=task.updated_at,
                changes={"external_updated_at": None})
    assert seeded.get_task(task.id).external_updated_at == 0.0
    with TestClient(create_app()) as client:
        created = client.post("/api/tasks", json={
            "project_id": "webshop", "title": "REST", "external_updated_at": 1000,
            "updated_at": 5})
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["external_updated_at"] == 1000.0 and body["updated_at"] > 5
