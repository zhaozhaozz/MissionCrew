"""无主控模式:项目未选择主控时所有角色同权,人类点名多个角色各自启动,
角色显式派发的结果交回派发者,人类直接点名的结果留在频道。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.manual import render_manual
from missioncrew.collab.tasks import create_task, dispatch_task
from missioncrew.core.models import Project, RunResult
from missioncrew.runtime import runtime_manager


@pytest.fixture()
def peer(seeded):
    project = seeded.get_project("webshop")
    project.orchestrator_role_id = ""
    seeded.put_project(project)
    return seeded


@pytest.fixture()
def chat(peer):
    return ChatEngine(peer, max_workers=2)


def _msgs(store, channel="general"):
    return store.list_messages(channel)


def _runs(store):
    return [r["role_id"] for r in store._query("SELECT role_id FROM chat_runs ORDER BY id")]


def _span(content: str, role_id: str) -> dict:
    start = content.index(f"@{role_id}")
    return {"role_id": role_id, "start": start, "end": start + len(role_id) + 1}


def test_project_helpers_reflect_peer_mode(peer):
    project = peer.get_project("webshop")
    assert project.has_orchestrator is False
    assert project.controls_platform("dev") and project.controls_platform("scribe")
    normal = Project(id="p", name="p", orchestrator_role_id="lead")
    assert normal.has_orchestrator and normal.controls_platform("lead")
    assert not normal.controls_platform("dev")


def test_human_multi_mention_starts_every_named_role(chat, peer, monkeypatch):
    """人类同时 @ 两个角色:两个都启动,回复都留在频道,不拉起任何人。"""
    prompts = {}

    def _start(config):
        prompts[config.role_id] = config.prompt
        return RunResult(True, "done", output=f"{config.role_id} 的评估结果。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    content = "@dev 和 @expert 分别评估一下方案 A/B。"
    chat.post("general", "human", content,
              mention_spans=[_span(content, "dev"), _span(content, "expert")])
    chat.wait_idle()

    assert sorted(_runs(peer)) == ["dev", "expert"]
    replies = [m for m in _msgs(peer) if m["author_type"] == "agent"]
    assert sorted(m["author"] for m in replies) == ["dev", "expert"]
    assert all(json.loads(m["mentions"]) == [] for m in replies)
    # 每个角色都拿到名册与无主控流程,且互相看得见对方
    assert "本项目没有主控" in prompts["dev"]
    assert "角色名册" in prompts["dev"] and "@expert" in prompts["dev"]
    assert "@dev" in prompts["expert"]
    assert "平台只启动你" not in prompts["dev"]


def test_human_message_without_mention_starts_nobody(chat, peer):
    chat.post("general", "human", "大家看看这个问题。")
    chat.wait_idle()
    assert _runs(peer) == []
    assert [m["author_type"] for m in _msgs(peer)] == ["human"]


def test_any_role_can_dispatch_and_result_returns_to_dispatcher(
        chat, peer, monkeypatch):
    """dev 经 message.publish 派发 reviewer:reviewer 结果交回 dev;
    dev 验收后的最终回复不再弹回 reviewer。"""
    prompts = []

    def _start(config):
        prompts.append((config.role_id, config.prompt))
        return RunResult(True, "ok", output=f"{config.role_id} 完成。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    dispatch = chat.post("general", "dev", "@reviewer\n\n请复核金额计算。",
                         author_type="agent",
                         mention_spans=[{"role_id": "reviewer", "start": 0, "end": 9}])
    chat.wait_idle()

    assert _runs(peer) == ["reviewer", "dev"]
    msgs = _msgs(peer)
    reviewer = next(m for m in msgs if m["author"] == "reviewer")
    assert reviewer["reply_to"] == dispatch
    assert json.loads(reviewer["mentions"]) == ["dev"]
    dev_final = [m for m in msgs if m["author"] == "dev" and m["id"] != dispatch]
    assert len(dev_final) == 1
    assert json.loads(dev_final[0]["mentions"]) == []      # 不再弹回 reviewer
    # 触发说明区分派发与回报
    reviewer_prompt = next(p for r, p in prompts if r == "reviewer")
    dev_prompt = next(p for r, p in prompts if r == "dev")
    assert "角色 @dev 派发的任务" in reviewer_prompt
    assert "角色 @reviewer 的回报,请验收" in dev_prompt


def test_human_direct_dispatch_failure_and_wake_stay_in_channel(
        chat, peer, monkeypatch):
    """人类直接点名的角色失败:公开失败,不拉起别人;
    角色派发的执行失败:交回派发者。"""
    monkeypatch.setattr(
        runtime_manager, "start",
        lambda config: RunResult(False, "环境不可用"))
    chat.post("general", "human", "@dev 跑一下测试",
              mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    chat.wait_idle()
    assert _runs(peer) == ["dev"]
    assert any(m["author_type"] == "platform" and "执行失败" in m["content"]
               for m in _msgs(peer))

    chat.post("general", "expert", "@dev\n\n再试一次。", author_type="agent",
              mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    chat.wait_idle()
    assert _runs(peer) == ["dev", "dev", "expert"]


def test_runtime_wake_returns_to_dispatcher_in_peer_mode(chat, peer):
    """后台命令唤醒汇报挂回启动那轮的派发消息,由派发者验收。"""
    dispatch = peer.add_message(
        "general", "expert", "agent", "@dev\n\n部署到测试机", ["dev"],
        mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    peer.put_chat_session("general::dev", "general", "dev", "b1",
                          "claude_code", "/tmp", "n1", "v1")
    chat._process_runtime_wake({
        "session_key": "general::dev", "backend_id": "b1",
        "success": True, "output": "部署完成:服务健康。",
        "events": [("text", "部署完成:服务健康。")],
        "tasks": [{"task_id": "bg1", "description": "deploy.sh",
                   "status": "completed", "origin_trigger": dispatch}],
    })
    chat.wait_idle()

    reply = next(m for m in _msgs(peer)
                 if m["author"] == "dev" and m["author_type"] == "agent")
    assert reply["reply_to"] == dispatch
    assert json.loads(reply["mentions"]) == ["expert"]
    assert [r["role_id"] for r in peer.chat_runs_for_channel("general")] == ["dev", "expert"]


def test_every_role_gets_orchestrator_actions_and_full_history(chat, peer):
    project = peer.get_project("webshop")
    allowed = set(chat.agent_tools.allowed_actions(project, "scribe"))
    assert {"message.publish", "channel.create", "channel.list",
            "task.delete", "document.delete"} <= allowed
    assert allowed == set(chat.agent_tools.allowed_actions(project, "dev"))

    # 频道历史对每个角色都是全视图:其他角色名不脱敏
    peer.add_message("general", "reviewer", "agent", "@dev 已复核,金额正确。", [])
    channel = peer.get_channel("general")
    role = peer.get_role("webshop", "expert")
    path = chat._write_channel_history(channel, role, project)
    record = json.loads(Path(path).read_text(encoding="utf-8"))["messages"][-1]
    assert record["author"]["id"] == "reviewer"
    assert "@dev" in record["content"]


def test_message_tool_dispatch_works_for_non_lead_role(peer):
    chat = ChatEngine(peer)
    channel = peer.get_channel("general")
    root = peer.add_message(channel.id, "human", "human", "@dev 安排复核", ["dev"])
    run_id = peer.add_chat_run(channel.id, "dev", root, root, 0)
    peer.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, peer.get_role("webshop", "dev"),
                            peer.get_backend("std-1"), root, run_id=run_id)
    token = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip()
    identity = chat.agent_tools.authenticate(token)
    assert "message.publish" in chat.agent_tools.capabilities(identity)["actions"]

    result = chat.agent_tools.execute(identity, "message.publish", {
        "channel": "general", "content": "请复核。", "mentions": ["reviewer"],
    }, run_id, "peer-dispatch")
    chat.wait_idle()
    assert result["dispatched"] == ["reviewer"]
    assert "reviewer" in _runs(peer)
    assert "你的新 turn" in result["resume"]


def test_project_api_accepts_empty_orchestrator_and_frees_lead(seeded):
    client = TestClient(create_app())
    project = seeded.get_project("webshop").to_dict()
    project["orchestrator_role_id"] = ""
    saved = client.post("/api/projects", json=project)
    assert saved.status_code == 200
    assert saved.json()["orchestrator_role_id"] == ""
    assert seeded.get_project("webshop").orchestrator_role_id == ""
    # 无主控后原主控可以停用;不传字段时保留无主控
    assert client.post("/api/roles/lead/enabled", json={
        "project_id": "webshop", "enabled": False}).status_code == 200
    kept = client.post("/api/projects", json={"id": "webshop", "name": "WebShop"})
    assert kept.json()["orchestrator_role_id"] == ""
    # 任何角色都能以角色身份做主控级 API 操作
    created = client.post("/api/chat/channels", json={
        "id": "peer-ch", "name": "peer", "project_id": "webshop",
        "actor_role_id": "dev"})
    assert created.status_code == 200
    denied = client.post("/api/chat/channels", json={
        "id": "peer-ch2", "name": "peer2", "project_id": "webshop",
        "actor_role_id": "ghost"})
    assert denied.status_code == 403
    # 新项目也可以直接无主控创建
    fresh = client.post("/api/projects", json={
        "id": "flat", "name": "Flat", "orchestrator_role_id": ""})
    assert fresh.status_code == 200 and fresh.json()["orchestrator_role_id"] == ""


def test_page_context_requires_target_role_without_orchestrator(peer):
    client = TestClient(create_app())
    body = {"page_kind": "guidelines", "page_key": "api-style.md", "content": "# x\n"}
    assert client.post("/api/chat/general/page-context", json=body).status_code == 400
    stored = client.post("/api/chat/general/page-context",
                         json={**body, "role_id": "dev"})
    assert stored.status_code == 200
    assert "/dev/.missioncrew/" in Path(stored.json()["path"]).as_posix()


def test_task_dispatch_and_manual_in_peer_mode(peer):
    chat = ChatEngine(peer)
    project = peer.get_project("webshop")
    assert "仅主控" not in render_manual(project)
    assert "本项目没有主控" in render_manual(project)
    task = create_task(peer, "webshop", title="复核金额", channel_ids=["general"])
    with pytest.raises(ValueError, match="没有主控"):
        dispatch_task(peer, chat, task)
    sent, _ = dispatch_task(peer, chat, task, target_role_ids=["dev"])
    chat.wait_idle()
    assert sent and _runs(peer) == ["dev"]
