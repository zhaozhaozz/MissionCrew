"""Issue 化 Task：编辑、状态简报与 Channel/Lead 派发。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
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
        assert task["status"] == "open"
        assert task["channel_ids"] == ["general", "webshop:delivery"]
        assert "stages" not in task and "task_type" not in task

        edited = client.patch(f"/api/tasks/{task['id']}", json={
            "snapshot_updated_at": task["updated_at"],
            "title": "重做结算体验",
            "summary": "先统一状态模型",
            "body": task["body"],
            "labels": ["checkout"],
            "channel_ids": ["webshop:delivery"],
            "status": "in_progress",
        })
        assert edited.status_code == 200, edited.text
        edited_task = edited.json()["task"]
        assert edited_task["title"] == "重做结算体验"
        assert edited_task["channel_ids"] == ["webshop:delivery"]

        stale = client.patch(f"/api/tasks/{task['id']}", json={
            "snapshot_updated_at": task["updated_at"],
            "title": "覆盖别人的修改",
        })
        assert stale.status_code == 409

        briefed = client.post(f"/api/tasks/{task['id']}/briefs", json={
            "status": "blocked",
            "content": "等待结算 API 字段冻结。",
        })
        assert briefed.status_code == 200
        payload = briefed.json()
        assert payload["task"]["status"] == "blocked"
        assert payload["briefs"][0]["content"] == "等待结算 API 字段冻结。"
        assert payload["task"]["updated_at"] > edited_task["updated_at"]


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
        overview = [
            task for task in client.get("/api/overview").json()["tasks"]
            if task["id"].startswith("t_recent-")
        ]
    assert [task["id"] for task in overview] == [
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


def test_processing_task_posts_to_each_bound_channel_and_triggers_lead(seeded):
    seeded.put_channel(Channel(
        id="webshop:delivery", name="交付", project_id="webshop"))
    task = create_task(
        seeded, "webshop", title="发布结算改版",
        summary="准备灰度发布", body="确认监控与回滚方案；@[dev] 只是正文引用。",
        channel_ids=["general", "webshop:delivery"],
    )
    chat = ChatEngine(seeded, max_workers=2)
    sent, brief = dispatch_task(
        seeded, chat, task, message="优先确认回滚开关。")
    chat.wait_idle()

    assert {item["channel_id"] for item in sent} == {
        "general", "webshop:delivery"}
    assert brief["status"] == "in_progress"
    assert seeded.get_task(task.id).status == "in_progress"
    for item in sent:
        message = seeded.get_message(item["message_id"])
        assert f"@lead 请处理 Task [{task.id}" in message["content"]
        assert "优先确认回滚开关" in message["content"]
        assert json.loads(message["mentions"]) == ["lead"]
        runs = seeded._query(
            "SELECT role_id FROM chat_runs WHERE trigger_message_id=?",
            (item["message_id"],),
        )
        assert [row["role_id"] for row in runs] == ["lead"]


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
    assert seeded.get_task(task.id).status == "open"


def test_task_auto_rule_dispatches_matching_new_task(seeded):
    project = seeded.get_project("webshop")
    project.task_auto_rules = [
        {"label": "sync", "role_ids": ["dev"],
         "prompt": "请分析并给出处理建议", "enabled": True},
        {"label": "ignored", "role_ids": [], "prompt": "", "enabled": True},
    ]
    seeded.put_project(project)
    chat = ChatEngine(seeded, max_workers=2)

    from missioncrew.collab.tasks import auto_process_task
    task = create_task(
        seeded, "webshop", title="外部同步任务", labels=["Sync"],
        channel_ids=["general"])
    result = auto_process_task(seeded, chat, task)
    chat.wait_idle()

    assert result and result["rule_label"] == "sync"
    assert seeded.get_task(task.id).status == "in_progress"
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
    assert seeded.get_task(plain.id).status == "open"


def test_task_api_create_applies_auto_rule(seeded):
    project = seeded.get_project("webshop")
    project.task_auto_rules = [
        {"label": "auto", "role_ids": [], "prompt": "按默认流程处理",
         "enabled": True}]
    seeded.put_project(project)
    with TestClient(create_app()) as client:
        created = client.post("/api/tasks", json={
            "project_id": "webshop", "title": "自动流转",
            "labels": ["auto"], "channel_ids": ["general"],
        }).json()
        assert created["auto_dispatch"]["rule_label"] == "auto"
        assert created["auto_dispatch"]["sent"]
        detail = client.get(f"/api/tasks/{created['id']}").json()
        assert detail["task"]["status"] == "in_progress"

        rules = client.get("/api/overview").json()["projects"][0].get(
            "task_auto_rules")
        assert rules and rules[0]["label"] == "auto"


def test_processing_task_rejects_disabled_orchestrator(seeded):
    lead = seeded.get_role("webshop", "lead")
    lead.enabled = False
    seeded.put_role(lead)
    task = create_task(
        seeded, "webshop", title="无法调度", channel_ids=["general"])
    chat = ChatEngine(seeded)

    with pytest.raises(ValueError, match="项目主控角色已停用"):
        dispatch_task(seeded, chat, task)
    assert seeded.get_task(task.id).status == "open"
    assert seeded.list_messages("general") == []
    assert seeded._query("SELECT * FROM chat_runs") == []


def test_task_edit_empty_channel_list_falls_back_to_general(seeded):
    task = create_task(
        seeded, "webshop", title="检查库存", channel_ids=["general"])
    updated = update_task(
        seeded, task, snapshot_updated_at=task.updated_at,
        changes={"channel_ids": []},
    )
    assert updated.channel_ids == ["general"]


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
    assert migrated.status == "in_progress"
    assert migrated.channel_ids == ["demo:general"]
    assert set(migrated.to_dict()) == {
        "id", "project_id", "title", "summary", "body", "labels",
        "channel_ids", "status", "archived", "archived_at",
        "created_at", "updated_at",
    }


def test_task_board_exposes_activity_order_and_archive_filter(seeded):
    with TestClient(create_app()) as client:
        html = client.get("/").text
        ui = client.get("/assets/js/ui.js").text
        tasks = client.get("/assets/js/tasks.js").text
        css = client.get("/assets/css/app.css").text

    assert 'id="task-filter"' in html and 'id="task-filter-summary"' in html
    assert all(label in html for label in (
        "活跃 Task", "全部 Task", "已归档 Task"))
    assert "taskActivity(right) - taskActivity(left)" in ui
    assert "visibleProjTasks" in tasks and "setTaskFilter" in tasks
    assert "function archiveTask" in tasks and "function restoreTask" in tasks
    assert "/archive" in tasks and "/restore" in tasks
    assert ".task-board-toolbar" in css and ".card.task-archived" in css
