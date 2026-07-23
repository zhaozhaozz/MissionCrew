"""Issue 化 Task：编辑、状态简报与 Channel/Lead 派发。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.tasks import create_task, dispatch_task, update_task
from missioncrew.core.models import Channel
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


def test_task_edit_empty_channel_list_falls_back_to_general(seeded):
    task = create_task(
        seeded, "webshop", title="检查库存", channel_ids=["general"])
    updated = update_task(
        seeded, task, snapshot_updated_at=task.updated_at,
        changes={"channel_ids": []},
    )
    assert updated.channel_ids == ["general"]


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
        "channel_ids", "status", "created_at", "updated_at",
    }
