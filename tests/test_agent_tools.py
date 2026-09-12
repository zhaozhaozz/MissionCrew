"""Agent Tool API：显式调用、身份作用域、错误回传与兼容入口。"""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew import agent_tool
from missioncrew.api import create_app
from missioncrew.collab.agent_tools import AgentToolError
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.documents import guideline_library_for, library_for
from missioncrew.core.models import Channel, RunResult
from missioncrew.runtime import runtime_manager


def _run_config(store, chat: ChatEngine, role_id: str):
    channel = store.get_channel("general")
    root_id = store.add_message(
        channel.id, "human", "human", f"@{role_id} 执行", [role_id])
    run_id = store.add_chat_run(channel.id, role_id, root_id, root_id, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(
        channel, store.get_role("webshop", role_id), store.get_backend("std-1"),
        root_id, run_id=run_id,
    )
    token_file = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"])
    return config, run_id, token_file.read_text(encoding="utf-8").strip()


def test_runtime_context_injects_scoped_tool_without_exposing_token(seeded):
    chat = ChatEngine(seeded)
    lead, lead_run, lead_token = _run_config(seeded, chat, "lead")
    dev, dev_run, dev_token = _run_config(seeded, chat, "dev")
    _lead_again, lead_run_again, next_lead_token = _run_config(
        seeded, chat, "lead")

    assert lead_run != dev_run and lead_token != dev_token
    assert lead_run_again != lead_run and next_lead_token != lead_token
    assert "# MissionCrew Agent Tool" in lead.common_prompt
    assert "message.publish" in lead.common_prompt
    assert "最终回复会由平台自动发布" in lead.common_prompt
    assert "不要再调用空 `mentions` 的 `message.publish`" in lead.common_prompt
    assert "派工仍必须使用 `message.publish`" in lead.common_prompt
    assert "返回非空 `dispatched` 时" in lead.common_prompt
    assert "不要使用 `sleep`" in lead.common_prompt
    assert "不要向仍在执行的角色再次 `message.publish` 追问" in lead.common_prompt
    assert "不能提供实时进度" in lead.common_prompt
    assert "channel.runs.list" in lead.common_prompt
    assert "channel.run.stop" in lead.common_prompt
    assert "活动 Run 状态不会自动写入你的上下文" in lead.common_prompt
    assert "不要为这条说明再调用一次 `message.publish`" in lead.common_prompt
    assert "document.publish" in dev.common_prompt
    assert "document.rename" in dev.common_prompt
    assert "message.publish" not in dev.common_prompt
    assert "channel.runs.list" not in dev.common_prompt
    assert "channel.run.stop" not in dev.common_prompt
    assert "返回非空 `dispatched` 时" not in dev.common_prompt
    assert "is_current_run" not in lead.common_prompt
    assert "missioncrew-action>" not in lead.common_prompt
    assert "不要传 `--run-id`" in lead.common_prompt     # 稳定规则进持久块,本轮输入不再重复
    assert "本轮上下文" not in lead.turn_prompt
    assert "MISSIONCREW_AGENT_RUN_ID" not in lead.env
    assert lead_token not in lead.prompt and dev_token not in dev.prompt
    assert Path(lead.env["MISSIONCREW_AGENT_TOKEN_FILE"]).stat().st_mode & 0o777 == 0o600

    stored = seeded._query(
        "SELECT token_hash, token_id, project_id, channel,role_id,run_id "
        "FROM agent_tokens")
    assert len(stored) == 3
    assert {row["run_id"] for row in stored} == {
        lead_run, dev_run, lead_run_again}
    assert all(row["token_hash"] not in {lead_token, dev_token} for row in stored)


def test_same_role_runs_rotate_bound_token_only_after_execution_lock(
        seeded, monkeypatch):
    chat = ChatEngine(seeded, max_workers=2)
    first_started = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    started_run_ids = []
    active = 0
    max_active = 0
    token_file = None

    def fake_start(config):
        nonlocal active, max_active, token_file
        token_file = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"])
        token = token_file.read_text(encoding="utf-8").strip()
        identity = chat.agent_tools.authenticate(token)
        with guard:
            started_run_ids.append(identity.run_id)
            active += 1
            max_active = max(max_active, active)
            first = len(started_run_ids) == 1
        if first:
            first_started.set()
            assert release_first.wait(timeout=5)
        with guard:
            active -= 1
        return RunResult(True, "ok", output="done")

    monkeypatch.setattr(runtime_manager, "start", fake_start)
    chat.post("general", "human", "@[dev] 第一轮")
    assert first_started.wait(timeout=5)
    chat.post("general", "human", "@[dev] 第二轮")
    time.sleep(0.1)
    assert len(started_run_ids) == 1
    release_first.set()
    chat.wait_idle()

    runs = seeded._query(
        "SELECT id FROM chat_runs WHERE role_id='dev' ORDER BY id")
    assert started_run_ids == [row["id"] for row in runs]
    assert max_active == 1
    assert token_file is not None and not token_file.exists()
    token_rows = seeded._query(
        "SELECT run_id,revoked_at FROM agent_tokens WHERE role_id='dev' ORDER BY created_at")
    assert [row["run_id"] for row in token_rows] == started_run_ids
    assert all(row["revoked_at"] is not None for row in token_rows)


def test_agent_tool_api_returns_structured_results_and_permission_errors(seeded):
    chat = ChatEngine(seeded)
    _dev_config, dev_run, dev_token = _run_config(seeded, chat, "dev")
    client = TestClient(create_app())
    headers = {"Authorization": f"Bearer {dev_token}"}

    capabilities = client.get("/api/agent/v1/actions", headers=headers)
    assert capabilities.status_code == 200
    assert set(capabilities.json()["result"]["actions"]) == {
        "document.publish", "document.rename",
        "task.brief", "task.create", "task.update"}

    published = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "document.publish",
        "request_id": "publish-1",
        "arguments": {
            "path": "reports/tool-result.bin",
            "content_base64": base64.b64encode(b"tool-result\x00").decode("ascii"),
        },
    })
    assert published.status_code == 200 and published.json()["ok"] is True
    assert library_for("webshop").read_bytes("reports/tool-result.bin") == b"tool-result\x00"
    assert published.json()["result"]["resource_url"] == \
        "/resources/webshop/documents/reports/tool-result.bin"

    conflict = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "document.publish", "run_id": dev_run,
        "request_id": "publish-2",
        "arguments": {"path": "reports/tool-result.bin", "content": "again"},
    })
    assert conflict.status_code == 409
    assert conflict.json()["error"] == {
        "code": "already_exists",
        "message": "文档已存在: reports/tool-result.bin；更新已有文档请传"
                   " overwrite=true(publish-file 加 --overwrite)",
        "retryable": False,
    }

    content_channel = client.post(
        "/api/projects/webshop/content-channel", json={
            "content_kind": "docs",
            "content_key": "reports/tool-result.bin",
            "label": "tool-result.bin",
        }).json()
    renamed = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "document.rename",
        "request_id": "rename-1",
        "arguments": {
            "source": "reports/tool-result.bin",
            "target": "archive/tool-result.bin",
        },
    })
    assert renamed.status_code == 200
    assert renamed.json()["result"]["resource_url"] == \
        "/resources/webshop/documents/archive/tool-result.bin"
    with pytest.raises(FileNotFoundError):
        library_for("webshop").read_bytes("reports/tool-result.bin")
    assert library_for("webshop").read_bytes(
        "archive/tool-result.bin") == b"tool-result\x00"
    history = library_for("webshop").history("archive/tool-result.bin")
    assert history[0]["revision"] == renamed.json()["result"]["revision"]
    assert history[0]["actor"] == "role:dev"
    rebound = seeded.get_channel(content_channel["id"])
    assert rebound is not None
    assert rebound.content_key == "archive/tool-result.bin"

    library_for("webshop").write(
        "reports/other.md", "other\n", actor="test",
        message="Prepare rename conflict")
    existing_target = client.post(
        "/api/agent/v1/actions", headers=headers, json={
            "action": "document.rename",
            "request_id": "rename-existing",
            "arguments": {
                "source": "reports/other.md",
                "target": "archive/tool-result.bin",
            },
        })
    assert existing_target.status_code == 409
    assert existing_target.json()["error"]["code"] == "already_exists"
    missing_source = client.post(
        "/api/agent/v1/actions", headers=headers, json={
            "action": "document.rename",
            "request_id": "rename-missing",
            "arguments": {"source": "missing.md", "target": "new.md"},
        })
    assert missing_source.status_code == 404
    assert missing_source.json()["error"]["code"] == "not_found"
    same_path = client.post(
        "/api/agent/v1/actions", headers=headers, json={
            "action": "document.rename",
            "request_id": "rename-same",
            "arguments": {"source": "archive/tool-result.bin",
                          "target": "archive/tool-result.bin"},
        })
    assert same_path.status_code == 400
    assert same_path.json()["error"]["code"] == "invalid_arguments"

    forbidden = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "message.publish", "run_id": dev_run,
        "request_id": "message-1",
        "arguments": {"channel": "general", "content": "越权消息", "mentions": []},
    })
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "permission_denied"

    unauthenticated = client.get("/api/agent/v1/actions")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "missing_token"
    audits = [row for row in seeded.list_audit(limit=50)
              if row["action"] == "agent_tool_called"]
    assert any("request=publish-1" in row["detail"] and "status=success" in row["detail"]
               for row in audits)
    assert any("request=publish-2" in row["detail"] and "code=already_exists" in row["detail"]
               for row in audits)
    assert any("request=rename-1" in row["detail"] and "status=success" in row["detail"]
               for row in audits)
    assert any("request=message-1" in row["detail"] and "status=failed" in row["detail"]
               for row in audits)

    seeded.delete_role("webshop", "dev")
    revoked = client.get("/api/agent/v1/actions", headers=headers)
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "invalid_token"


def test_orchestrator_guideline_tool_records_role_version(seeded):
    chat = ChatEngine(seeded)
    config, run_id, token = _run_config(seeded, chat, "lead")
    client = TestClient(create_app())

    response = client.post("/api/agent/v1/actions", headers={
        "Authorization": f"Bearer {token}",
    }, json={
        "action": "guideline.save", "run_id": run_id,
        "request_id": "guideline-save-1",
        "arguments": {
            "original_name": "task-validation",
            "markdown": "---\nname: task-validation\n"
                        "description: Agent Tool 版本验证\n---\n\n# 新版本\n",
            "enabled": True,
        },
    })

    assert response.status_code == 200
    revision = response.json()["result"]["revision"]
    assert len(revision) == 40
    history = guideline_library_for("webshop").history(
        "task-validation.md", limit=1)
    assert history[0]["revision"] == revision
    assert history[0]["actor"] == "role:lead"
    guideline_dir = Path(config.env["MISSIONCREW_GUIDELINES_DIR"])
    assert guideline_dir.is_symlink()
    assert (guideline_dir / "task-validation.md").read_text(encoding="utf-8") \
        .endswith("\n# 新版本\n")


def test_writable_agent_tools_append_conversation_receipts(seeded):
    chat = ChatEngine(seeded)
    _config, run_id, token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(token)
    before = len(seeded.list_messages("general"))

    channel = chat.agent_tools.execute(
        identity, "channel.create", {
            "id": "tool-created", "name": "工具创建", "purpose": "验证可见回执",
        }, run_id, "create-channel-receipt")
    document = chat.agent_tools.execute(
        identity, "document.publish", {
            "path": "reports/receipt.md", "content": "# 回执\n",
        }, run_id, "publish-document-receipt")
    chat.agent_tools.execute(
        identity, "recycle.list", {}, run_id, "read-recycle-no-receipt")
    with pytest.raises(AgentToolError):
        chat.agent_tools.execute(
            identity, "document.publish", {
                "path": "reports/receipt.md", "content": "重复",
            }, run_id, "failed-write-no-receipt")
    renamed = chat.agent_tools.execute(
        identity, "document.rename", {
            "source": "reports/receipt.md", "target": "archive/receipt.md",
        }, run_id, "rename-document-receipt")

    appended = seeded.list_messages("general")[before:]
    assert len(appended) == 3
    assert all(item["author_type"] == "platform" for item in appended)
    assert all(item["kind"] == "agent_tool" for item in appended)
    assert channel["summary"] in appended[0]["content"]
    assert document["summary"] in appended[1]["content"]
    assert renamed["summary"] in appended[2]["content"]
    assert "`channel.create`" in appended[0]["content"]
    assert "`document.publish`" in appended[1]["content"]
    assert "`document.rename`" in appended[2]["content"]
    assert all("@lead 使用 MissionCrew Tool" in item["content"]
               for item in appended)
    assert [json.loads(item["context"])["agent_tool"]["action"]
            for item in appended] == [
                "channel.create", "document.publish", "document.rename"]
    assert all(item["root_id"] == seeded.get_chat_run(run_id)["root_id"]
               for item in appended)
    assert len(seeded._query("SELECT * FROM chat_runs")) == 1


def test_orchestrator_delete_tools_update_shared_views_in_same_run(seeded):
    chat = ChatEngine(seeded)
    config, run_id, token = _run_config(seeded, chat, "lead")
    client = TestClient(create_app())
    headers = {"Authorization": f"Bearer {token}"}

    def call(action: str, request_id: str, arguments: dict):
        response = client.post("/api/agent/v1/actions", headers=headers, json={
            "action": action, "run_id": run_id, "request_id": request_id,
            "arguments": arguments,
        })
        assert response.status_code == 200, response.text
        return response.json()["result"]

    guidelines = Path(config.env["MISSIONCREW_GUIDELINES_DIR"])
    skills = Path(config.env["MISSIONCREW_SKILLS_DIR"])
    documents = Path(config.env["MISSIONCREW_DOCUMENTS_DIR"])
    guideline_path = guidelines / "tool-managed.md"
    skill_path = skills / "tool-managed" / "SKILL.md"
    document_path = documents / "tool-managed" / "note.md"
    tasks = Path(config.env["MISSIONCREW_TASKS_DIR"])

    call("guideline.save", "save-guideline", {
        "markdown": "---\nname: tool-managed\ndescription: 工具一致性测试\n---\n\n正文\n",
        "enabled": True,
    })
    saved_skill = call("skill.save", "save-skill", {
        "id": "tool-managed",
        "markdown": "---\nname: tool-managed\ndescription: 工具一致性测试\n---\n\n说明\n",
        "enabled": True,
    })
    call("document.publish", "save-document", {
        "path": "tool-managed/note.md", "content": "正文\n",
    })
    task = call("task.create", "save-task", {
        "title": "工具管理任务",
        "body": "删除后应可恢复。",
        "channel_ids": ["general"],
    })["task"]
    task_path = tasks / f"{task['id']}.md"
    assert guideline_path.is_file() and "正文" in guideline_path.read_text()
    assert skill_path.is_file() and "说明" in skill_path.read_text()
    assert document_path.read_text() == "正文\n"
    assert task_path.is_file()
    assert len(saved_skill["revision"]) == 40

    guideline_result = call(
        "guideline.delete", "delete-guideline", {"name": "tool-managed"})
    skill_result = call("skill.delete", "delete-skill", {"id": "tool-managed"})
    document_result = call(
        "document.delete", "delete-document", {"path": "tool-managed/note.md"})
    task_result = call("task.delete", "delete-task", {"id": task["id"]})
    assert guideline_result["deleted"] is skill_result["deleted"] is True
    assert document_result["deleted"] is task_result["deleted"] is True
    assert len(guideline_result["revision"]) == 40
    assert len(skill_result["revision"]) == 40
    assert len(document_result["revision"]) == 40
    assert not guideline_path.exists()
    assert not skill_path.exists()
    assert not document_path.exists()
    assert not task_path.exists()

    recycled = call("recycle.list", "list-recycle-bin", {})
    by_type = {item["resource_type"]: item for item in recycled["items"]}
    assert set(by_type) == {"document", "guideline", "skill", "task"}
    call("recycle.restore", "restore-guideline", {
        "id": by_type["guideline"]["id"],
    })
    call("recycle.restore", "restore-document", {
        "id": by_type["document"]["id"],
    })
    call("recycle.restore", "restore-task", {
        "id": by_type["task"]["id"],
    })
    call("recycle.purge", "purge-skill", {
        "id": by_type["skill"]["id"],
    })
    assert guideline_path.is_file()
    assert document_path.is_file()
    assert task_path.is_file()
    assert not skill_path.exists()
    assert call("recycle.list", "list-recycle-bin-empty", {})["items"] == []


def test_orchestrator_message_tool_uses_explicit_mentions_and_chain_context(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    _config, run_id, token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(token)

    result = chat.agent_tools.execute(
        identity, "message.publish", {
            "channel": "general",
            "content": "请执行明确分配的工作；正文里的 @reviewer 只是普通引用。",
            "mentions": ["dev"],
        }, run_id, "dispatch-1")
    chat.wait_idle()

    message = seeded.get_message(result["message_id"])
    assert message["content"].startswith("@dev\n\n请执行明确分配的工作")
    assert json.loads(message["mentions"]) == ["dev"]
    assert json.loads(message["mention_spans"]) == [
        {"role_id": "dev", "start": 0, "end": len("@dev")},
    ]
    assert message["root_id"] == seeded.get_chat_run(run_id)["root_id"]
    roles = [row["role_id"] for row in seeded._query(
        "SELECT role_id FROM chat_runs WHERE root_id=? ORDER BY id",
        (message["root_id"],))]
    assert "dev" in roles and "reviewer" not in roles
    assert result["dispatched"] == ["dev"]
    assert result["handoff"] == "end_turn"
    assert "不要向仍在执行的角色追问中间状态" in result["resume"]
    assert "自动启动你的新 turn" in result["resume"]
    assert "not_dispatched" not in result


def test_cross_channel_publish_leaves_receipt_in_source_channel(seeded):
    """跨频道派发的消息只存在于目标频道:源频道要留一条带目标链接与派发名单的
    回执,主控被停止后再唤起才能看出已派发;同频道发布仍不留回执。"""
    chat = ChatEngine(seeded, max_workers=2)
    _config, run_id, token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(token)
    seeded.put_channel(Channel(
        id="private", name="其他频道", project_id="webshop"))
    before = len(seeded.list_messages("general"))

    same = chat.agent_tools.execute(
        identity, "message.publish", {"channel": "general", "content": "同频道说明"},
        run_id, "publish-same-channel")
    assert same["channel_id"] == "general"
    assert [m["id"] for m in seeded.list_messages("general")[before:]] == [
        same["message_id"]]

    cross = chat.agent_tools.execute(
        identity, "message.publish", {
            "channel": "private", "content": "去部署", "mentions": ["dev"],
        }, run_id, "publish-cross-channel")
    chat.wait_idle()
    assert cross["channel_id"] == "private" and cross["dispatched"] == ["dev"]
    assert cross["summary"] == (
        "已在 [#其他频道](/resources/webshop/channels/private) 发布消息，已派发 @dev")
    assert seeded.get_message(cross["message_id"])["channel"] == "private"

    receipts = [m for m in seeded.list_messages("general")[before:]
                if m["kind"] == "agent_tool"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["author_type"] == "platform"
    assert receipt["content"] == (
        "@lead 使用 MissionCrew Tool · `message.publish`：" + cross["summary"])
    assert receipt["root_id"] == seeded.get_chat_run(run_id)["root_id"]
    context = receipt["context"]
    context = json.loads(context) if isinstance(context, str) else context
    assert context["agent_tool"] == {
        "action": "message.publish", "role_id": "lead", "run_id": run_id,
        "channel": "private", "message_id": cross["message_id"],
    }


def test_orchestrator_queries_and_stops_only_current_channel_runs(
        seeded, monkeypatch):
    chat = ChatEngine(seeded)
    _config, lead_run, lead_token = _run_config(seeded, chat, "lead")

    trigger = seeded.add_message("general", "human", "human", "并行任务", [])
    dev_run = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    queued_dev_run = seeded.add_chat_run(
        "general", "dev", trigger, trigger, 0)
    seeded.update_chat_run(dev_run, "running", backend_id="std-1",
                           model="model-a", effort="high")

    seeded.put_channel(Channel(
        id="private", name="其他频道", project_id="webshop"))
    private_trigger = seeded.add_message(
        "private", "human", "human", "私有任务", [])
    private_run = seeded.add_chat_run(
        "private", "expert", private_trigger, private_trigger, 0)
    seeded.update_chat_run(private_run, "running", backend_id="exp-1")

    stopped = []
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)
    client = TestClient(create_app())
    headers = {"Authorization": f"Bearer {lead_token}"}

    listed = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "channel.runs.list",
        "request_id": "list-current-channel-runs",
        "arguments": {},
    })
    assert listed.status_code == 200
    result = listed.json()["result"]
    assert result["channel_id"] == "general"
    assert [run["run_id"] for run in result["runs"]] == [
        lead_run, dev_run, queued_dev_run]
    assert private_run not in {run["run_id"] for run in result["runs"]}
    current = next(run for run in result["runs"] if run["run_id"] == lead_run)
    target = next(run for run in result["runs"] if run["run_id"] == dev_run)
    assert current["is_current_run"] is True and current["stoppable"] is False
    assert target == {
        "run_id": dev_run,
        "role_id": "dev",
        "role_name": "开发",
        "status": "running",
        "backend_id": "std-1",
        "model": "model-a",
        "effort": "high",
        "created_at": target["created_at"],
        "is_current_run": False,
        "stoppable": True,
    }

    # 排队项没有绑定 Runtime，只取消该 Run，不能误停同角色正在执行的实例。
    queued_stop = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "channel.run.stop",
        "request_id": "stop-queued-current-channel-run",
        "arguments": {"run_id": queued_dev_run},
    })
    assert queued_stop.status_code == 200
    assert queued_stop.json()["result"]["stopped_runtimes"] == 0
    assert stopped == []
    assert seeded.get_chat_run(dev_run)["status"] == "running"

    running_stop = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "channel.run.stop",
        "request_id": "stop-running-current-channel-run",
        "arguments": {"run_id": dev_run},
    })
    assert running_stop.status_code == 200
    assert running_stop.json()["result"]["role_id"] == "dev"
    assert stopped == [("std-1", "general::dev")]
    assert seeded.get_chat_run(dev_run)["status"] == "stopped"
    assert seeded.get_chat_run(lead_run)["status"] == "running"
    assert seeded.get_chat_run(private_run)["status"] == "running"

    self_stop = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "channel.run.stop", "request_id": "reject-self-stop",
        "arguments": {"run_id": lead_run},
    })
    assert self_stop.status_code == 409
    assert self_stop.json()["error"]["code"] == "cannot_stop_self"
    cross_channel = client.post(
        "/api/agent/v1/actions", headers=headers, json={
            "action": "channel.run.stop",
            "request_id": "reject-cross-channel-stop",
            "arguments": {"run_id": private_run},
        })
    assert cross_channel.status_code == 404
    assert cross_channel.json()["error"]["code"] == "run_not_found"
    repeated = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "channel.run.stop", "request_id": "reject-repeat-stop",
        "arguments": {"run_id": dev_run},
    })
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "run_inactive"

    audits = seeded.list_audit(limit=100)
    assert any(row["action"] == "chat_run_stopped"
               and row["actor"] == "role:lead"
               and f"run={dev_run}" in row["detail"] for row in audits)
    stop_messages = [message for message in seeded.list_messages("general")
                     if message["kind"] == "agent_stop"]
    assert len(stop_messages) == 2
    assert all("主控 @lead 已停止 @dev" in message["content"]
               for message in stop_messages)


def test_channel_runs_list_can_peek_other_channel_but_stop_stays_local(
        seeded, monkeypatch):
    """主控跨频道派发后被停止再唤起时,要能看目标频道是否已有活动运行;
    但停止仍只对当前频道开放,跨频道查到的运行 stoppable 恒为 False。"""
    chat = ChatEngine(seeded)
    _config, lead_run, lead_token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(lead_token)
    seeded.put_channel(Channel(
        id="private", name="其他频道", project_id="webshop"))
    private_trigger = seeded.add_message(
        "private", "lead", "agent", "@expert 开工", ["expert"])
    private_run = seeded.add_chat_run(
        "private", "expert", private_trigger, private_trigger, 0)
    seeded.update_chat_run(private_run, "running", backend_id="exp-1")
    monkeypatch.setattr(runtime_manager, "stop",
                        lambda backend, session_key="": 1)

    current = chat.agent_tools.execute(
        identity, "channel.runs.list", {}, lead_run, "runs-current")
    assert current["channel_id"] == "general"
    assert current["is_current_channel"] is True
    assert [run["run_id"] for run in current["runs"]] == [lead_run]

    other = chat.agent_tools.execute(
        identity, "channel.runs.list", {"channel": "private"}, lead_run,
        "runs-other")
    assert other["channel_id"] == "private"
    assert other["is_current_channel"] is False
    assert "#其他频道" in other["summary"]
    assert [(run["run_id"], run["role_id"], run["stoppable"])
            for run in other["runs"]] == [(private_run, "expert", False)]

    with pytest.raises(AgentToolError) as missing:
        chat.agent_tools.execute(
            identity, "channel.runs.list", {"channel": "nowhere"}, lead_run,
            "runs-missing")
    assert missing.value.code == "channel_not_found"
    with pytest.raises(AgentToolError) as cross:
        chat.agent_tools.execute(
            identity, "channel.run.stop", {"run_id": private_run}, lead_run,
            "stop-cross-channel")
    assert cross.value.code == "run_not_found"
    assert seeded.get_chat_run(private_run)["status"] == "running"
    # 只读查询不留工具回执
    assert not [m for m in seeded.list_messages("general")
                if m["kind"] == "agent_tool"]


def test_channel_history_reads_current_and_other_channels(seeded):
    """channel.history 按需读取频道最近消息:默认当前频道,可指定其他频道、
    限制条数与向前翻页;记录格式与 channel-history.json 一致;执行角色无权。"""
    chat = ChatEngine(seeded)
    _config, lead_run, lead_token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(lead_token)
    assert "channel.history" in chat.agent_tools.capabilities(identity)["actions"]
    seeded.put_channel(Channel(
        id="private", name="其他频道", project_id="webshop"))
    first = seeded.add_message(
        "private", "lead", "agent", "@dev 部署 Jenkins", ["dev"])
    seeded.add_message("private", "platform", "platform",
                       "@dev 使用 MissionCrew Tool · `task.brief`：已追加",
                       [], kind="agent_tool")
    last = seeded.add_message(
        "private", "dev", "agent", "部署完成,向 @lead 汇报", ["lead"])

    current = chat.agent_tools.execute(
        identity, "channel.history", {}, lead_run, "history-current")
    assert current["channel_id"] == "general"
    assert current["is_current_channel"] is True
    assert current["messages"][-1]["content"] == "@lead 执行"

    other = chat.agent_tools.execute(
        identity, "channel.history", {"channel": "private"}, lead_run,
        "history-other")
    assert other["channel_name"] == "其他频道"
    assert other["is_current_channel"] is False
    assert other["message_count"] == 3
    assert other["resource_url"] == "/resources/webshop/channels/private"
    assert [m["id"] for m in other["messages"]] == [first, first + 1, last]
    assert other["messages"][1]["kind"] == "agent_tool"
    tail = other["messages"][-1]
    assert tail["author"] == {"id": "dev", "type": "agent"}
    assert tail["mentions"] == ["lead"]
    assert tail["thread"]["root_id"] == last

    limited = chat.agent_tools.execute(
        identity, "channel.history", {"channel": "private", "limit": 1},
        lead_run, "history-limit")
    assert [m["id"] for m in limited["messages"]] == [last]
    paged = chat.agent_tools.execute(
        identity, "channel.history",
        {"channel": "private", "limit": 1, "before_id": last},
        lead_run, "history-page")
    assert [m["id"] for m in paged["messages"]] == [first + 1]
    assert f"id < {last}" in paged["summary"]

    for bad in ({"limit": -1}, {"limit": True}, {"before_id": "x"},
                {"channel": "nowhere"}):
        with pytest.raises(AgentToolError) as caught:
            chat.agent_tools.execute(
                identity, "channel.history", bad, lead_run, "history-bad")
        assert caught.value.code in {"invalid_arguments", "channel_not_found"}
    # 只读查询不留工具回执
    assert not [m for m in seeded.list_messages("general")
                if m["kind"] == "agent_tool"]

    _dev, dev_run, dev_token = _run_config(seeded, chat, "dev")
    dev_identity = chat.agent_tools.authenticate(dev_token)
    assert "channel.history" not in chat.agent_tools.capabilities(dev_identity)["actions"]
    with pytest.raises(AgentToolError) as denied:
        chat.agent_tools.execute(
            dev_identity, "channel.history", {}, dev_run, "history-denied")
    assert denied.value.code == "permission_denied"


def test_non_orchestrator_cannot_query_or_stop_channel_runs(seeded):
    chat = ChatEngine(seeded)
    _config, dev_run, dev_token = _run_config(seeded, chat, "dev")
    identity = chat.agent_tools.authenticate(dev_token)

    assert "channel.runs.list" not in chat.agent_tools.capabilities(identity)["actions"]
    for action, arguments in (
            ("channel.runs.list", {}),
            ("channel.run.stop", {"run_id": dev_run})):
        with pytest.raises(AgentToolError) as caught:
            chat.agent_tools.execute(
                identity, action, arguments, dev_run,
                f"deny-{action.replace('.', '-')}")
        assert caught.value.code == "permission_denied"


def test_message_tool_reports_budget_dropped_dispatch(seeded):
    """协作链预算耗尽时,派发被兜底丢弃必须反映在工具返回值里。"""
    chat = ChatEngine(seeded)
    _config, run_id, token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(token)
    project = seeded.get_project("webshop")
    project.max_chain_runs = 1          # 链上已有 lead 自己这 1 次执行
    seeded.put_project(project)

    result = chat.agent_tools.execute(
        identity, "message.publish", {
            "channel": "general", "content": "继续。", "mentions": ["dev"],
        }, run_id, "dispatch-over-budget")
    chat.wait_idle()

    assert result["dispatched"] == []
    assert result["not_dispatched"] == ["dev"]
    assert "handoff" not in result and "resume" not in result
    assert "未启动" in result["summary"]
    assert not any(r["role_id"] == "dev" for r in
                   seeded._query("SELECT role_id FROM chat_runs"))


def test_message_tool_rejects_disabled_role_before_publishing(seeded):
    chat = ChatEngine(seeded)
    _config, run_id, token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(token)
    role = seeded.get_role("webshop", "dev")
    role.enabled = False
    seeded.put_role(role)
    before = len(seeded.list_messages("general"))

    with pytest.raises(AgentToolError) as excinfo:
        chat.agent_tools.execute(
            identity, "message.publish", {
                "channel": "general", "content": "请执行。", "mentions": ["dev"],
            }, run_id, "dispatch-disabled")
    assert excinfo.value.code == "role_not_found"
    assert excinfo.value.status_code == 404
    assert "停用" not in excinfo.value.message
    assert len(seeded.list_messages("general")) == before
    assert not any(row["role_id"] == "dev" for row in
                   seeded._query("SELECT role_id FROM chat_runs"))


def test_message_tool_rejected_after_run_stopped(seeded):
    """停止与派发原子互斥:发起 run 停止后,迟到的 message.publish 不落库不调度。"""
    chat = ChatEngine(seeded)
    _config, run_id, token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(token)
    before = len(seeded.list_messages("general"))
    seeded.update_chat_run(run_id, "stopped")

    with pytest.raises(AgentToolError) as excinfo:
        chat.agent_tools.execute(
            identity, "message.publish", {
                "channel": "general", "content": "迟到的派发。",
                "mentions": ["dev"],
            }, run_id, "dispatch-after-stop")
    assert excinfo.value.code == "run_inactive"
    assert len(seeded.list_messages("general")) == before
    assert not any(r["role_id"] == "dev" for r in
                   seeded._query("SELECT role_id FROM chat_runs"))


def test_agent_tool_task_update_uses_optimistic_version_and_run_scope(seeded):
    chat = ChatEngine(seeded)
    _config, run_id, token = _run_config(seeded, chat, "dev")
    client = TestClient(create_app())
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.create", "run_id": run_id, "request_id": "task-create",
        "arguments": {
            "title": "补齐接口测试", "summary": "验证角色工具调用",
            "body": "覆盖权限与错误返回", "labels": ["agent-tool"],
            "channel_ids": ["general"],
        },
    })
    assert created.status_code == 200
    task = created.json()["result"]["task"]

    updated = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.update", "run_id": run_id, "request_id": "task-update",
        "arguments": {
            "id": task["id"], "snapshot_updated_at": task["updated_at"],
            "title": "补齐接口与权限测试",
        },
    })
    assert updated.status_code == 200
    assert updated.json()["result"]["task"]["title"] == "补齐接口与权限测试"
    task_file = Path(_config.env["MISSIONCREW_TASKS_DIR"]) / f"{task['id']}.md"
    assert "title: 补齐接口与权限测试" in task_file.read_text(encoding="utf-8")

    briefed = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.brief", "run_id": run_id, "request_id": "task-brief",
        "arguments": {
            "id": task["id"], "status": "in_progress",
            "content": "已补齐首轮接口用例。",
        },
    })
    assert briefed.status_code == 200
    assert briefed.json()["result"]["brief"]["author"] == "dev"

    stale = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.update", "run_id": run_id, "request_id": "task-stale",
        "arguments": {
            "id": task["id"], "snapshot_updated_at": task["updated_at"],
            "title": "覆盖新版本",
        },
    })
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    other_channel = Channel(
        id="webshop:other", name="其他", project_id="webshop")
    seeded.put_channel(other_channel)
    other_root = seeded.add_message(
        other_channel.id, "human", "human", "@dev 检查", ["dev"])
    other_run = seeded.add_chat_run(
        other_channel.id, "dev", other_root, other_root, 0)
    seeded.update_chat_run(other_run, "running", backend_id="std-1")
    mismatch = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.create", "run_id": other_run, "request_id": "wrong-run",
        "arguments": {"title": "不应创建"},
    })
    assert mismatch.status_code == 403
    assert mismatch.json()["error"]["code"] == "run_mismatch"

    malformed = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.create", "run_id": 0, "arguments": {},
    })
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_request"

    typo = client.post("/api/agent/v1/actions", headers=headers, json={
        "action": "task.create", "run_id": run_id, "request_id": "task-typo",
        "arguments": {"title": "不应静默忽略", "lables": ["typo"]},
    })
    assert typo.status_code == 400
    assert typo.json()["error"]["code"] == "invalid_arguments"
    assert "lables" in typo.json()["error"]["message"]


def test_agent_tool_cli_unknown_command_hints_documents_dir(capsys):
    with pytest.raises(SystemExit) as excinfo:
        agent_tool.main(["read-document", "--path", "reports/x.md"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "未知子命令: read-document" in err
    assert "$MISSIONCREW_DOCUMENTS_DIR" in err
    assert "publish-file" in err          # 同时列出可用子命令


def test_agent_tool_cli_encodes_file_and_preserves_structured_error(
        tmp_path, monkeypatch, capsys):
    source = tmp_path / "evidence.bin"
    source.write_bytes(b"evidence\x00")
    captured = {}

    def fake_request(method, payload=None):
        captured.update(method=method, payload=payload)
        return 409, {"ok": False, "error": {
            "code": "already_exists", "message": "目标已存在", "retryable": False}}

    monkeypatch.setattr(agent_tool, "_request_json", fake_request)
    exit_code = agent_tool.main([
        "publish-file", "--source", str(source),
        "--path", "reports/evidence.bin",
    ])
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1 and output["error"]["code"] == "already_exists"
    assert captured["method"] == "POST"
    assert captured["payload"]["action"] == "document.publish"
    assert "run_id" not in captured["payload"]
    encoded = captured["payload"]["arguments"]["content_base64"]
    assert base64.b64decode(encoded) == b"evidence\x00"


def test_channel_list_action_is_orchestrator_only_and_covers_archived(seeded):
    """频道清单不再预先写进上下文:主控用 channel.list 按需查(含归档),执行角色无权。"""
    chat = ChatEngine(seeded)
    seeded.put_channel(Channel(id="webshop:old-topic", name="old-topic", project_id="webshop",
                               purpose="已收尾", archived=True))
    seeded.put_channel(Channel(id="webshop:hotfix", name="hotfix", project_id="webshop",
                               purpose="修复结算页", workdir="/nonexistent/worktree"))
    _config, lead_run, lead_token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(lead_token)
    assert "channel.list" in chat.agent_tools.capabilities(identity)["actions"]

    active = chat.agent_tools.execute(identity, "channel.list", {}, lead_run, "list-active")
    ids = {row["id"]: row for row in active["channels"]}
    assert active["scope"] == "active" and "old-topic" not in ids
    assert ids["general"]["is_current"] is True
    assert ids["hotfix"]["workdir_missing"] is True and ids["hotfix"]["archived"] is False
    assert ids["hotfix"]["resource_url"] == "/resources/webshop/channels/hotfix"

    archived = chat.agent_tools.execute(
        identity, "channel.list", {"scope": "archived"}, lead_run, "list-archived")
    assert [row["id"] for row in archived["channels"]] == ["old-topic"]
    everything = chat.agent_tools.execute(
        identity, "channel.list", {"scope": "all"}, lead_run, "list-all")
    assert {"general", "old-topic", "hotfix"} <= {row["id"] for row in everything["channels"]}
    with pytest.raises(AgentToolError) as bad:
        chat.agent_tools.execute(identity, "channel.list", {"scope": "later"}, lead_run, "list-bad")
    assert bad.value.code == "invalid_arguments"

    _dev, dev_run, dev_token = _run_config(seeded, chat, "dev")
    dev_identity = chat.agent_tools.authenticate(dev_token)
    assert "channel.list" not in chat.agent_tools.capabilities(dev_identity)["actions"]
    with pytest.raises(AgentToolError) as denied:
        chat.agent_tools.execute(dev_identity, "channel.list", {}, dev_run, "list-denied")
    assert denied.value.code == "permission_denied"


def test_message_publish_reports_chain_budget(seeded):
    """主控看不到预算余量:message.publish 的返回值带本条协作链已用次数与上限。"""
    chat = ChatEngine(seeded)
    _config, lead_run, lead_token = _run_config(seeded, chat, "lead")
    identity = chat.agent_tools.authenticate(lead_token)
    result = chat.agent_tools.execute(
        identity, "message.publish", {"channel": "general", "content": "进度说明"},
        lead_run, "publish-budget")
    budget = result["chain_budget"]
    assert budget["limit"] == seeded.get_project("webshop").max_chain_runs
    assert budget["used"] >= 1
