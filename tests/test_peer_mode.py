"""无主控模式:项目未选择主控时所有角色都有主控级管理权限,但不能互相派发;
只有人类点名启动角色,点名多个角色各自启动,结果留在频道。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.agent_tools import AgentToolError
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
    assert not project.can_dispatch("dev") and not project.can_dispatch("lead")
    # 不能提及别人;只能在其他频道提及自己
    assert not project.can_mention("dev", "reviewer", cross_channel=True)
    assert project.can_mention("dev", "dev", cross_channel=True)
    assert not project.can_mention("dev", "dev")
    normal = Project(id="p", name="p", orchestrator_role_id="lead")
    assert normal.has_orchestrator and normal.controls_platform("lead")
    assert not normal.controls_platform("dev")
    assert normal.can_dispatch("lead") and not normal.can_dispatch("dev")


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
    # 每个角色都拿到名册与无主控流程,互相看得见对方,但被告知不能派发
    assert "This project has no orchestrator" in prompts["dev"]
    assert "roles cannot dispatch each other" in prompts["dev"]
    assert "dispatch work to other roles as needed" not in prompts["dev"]
    assert "Role Roster" in prompts["dev"] and "@expert" in prompts["dev"]
    assert "@dev" in prompts["expert"]
    assert "the platform starts only you" not in prompts["dev"]
    assert "Collaboration chain budget" not in prompts["dev"]


def test_human_message_without_mention_is_rejected(chat, peer):
    """无主控时不点名的消息没人处理:引擎与 API 都拒收,不落库也不触发。"""
    with pytest.raises(ValueError, match="至少一个角色"):
        chat.post("general", "human", "大家看看这个问题。")
    with pytest.raises(ValueError, match="至少一个角色"):
        chat.post("general", "human", "大家看看", mention_spans=[])
    response = TestClient(create_app()).post(
        "/api/chat/general/messages",
        json={"author": "human", "content": "大家看看", "mentions": []})
    assert response.status_code == 400
    assert "至少一个角色" in response.json()["detail"]
    chat.wait_idle()
    assert _runs(peer) == []
    assert _msgs(peer) == []
    # 自动化脚本不点名仍可只发消息
    chat.post("general", "nightly", "巡检完成。", author_type="automation")
    assert [m["author_type"] for m in _msgs(peer)] == ["automation"]


def test_agent_mention_spans_never_dispatch_in_peer_mode(chat, peer, monkeypatch):
    """角色消息即使带结构化提及范围也不触发任何角色,提及不落库。"""
    monkeypatch.setattr(runtime_manager, "start",
                        lambda config: RunResult(True, "ok", output="完成。"))
    chat.post("general", "dev", "@reviewer\n\n请复核金额计算。",
              author_type="agent",
              mention_spans=[{"role_id": "reviewer", "start": 0, "end": 9}])
    chat.wait_idle()

    assert _runs(peer) == []
    message = _msgs(peer)[-1]
    assert json.loads(message["mentions"]) == []
    # 正文 @[角色] 的旧派发语法也不再补"请改用 mentions"的平台提示
    chat.post("general", "dev", "@[reviewer] 看一下", author_type="agent")
    chat.wait_idle()
    assert _runs(peer) == []
    assert all(m["author_type"] != "platform" for m in _msgs(peer))


def test_human_direct_dispatch_failure_and_wake_stay_in_channel(
        chat, peer, monkeypatch):
    """人类直接点名的角色失败:公开失败,不拉起别人;角色消息点名别人也不触发。"""
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
    assert _runs(peer) == ["dev"]


def test_runtime_wake_stays_in_channel_in_peer_mode(chat, peer):
    """后台命令唤醒汇报挂回启动那轮的人类消息,不拉起任何角色。"""
    dispatch = peer.add_message(
        "general", "human", "human", "@dev 部署到测试机", ["dev"],
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
    assert json.loads(reply["mentions"]) == []
    assert [r["role_id"] for r in peer.chat_runs_for_channel("general")] == ["dev"]


def test_every_role_gets_orchestrator_actions_and_full_history(chat, peer):
    project = peer.get_project("webshop")
    allowed = set(chat.agent_tools.allowed_actions(project, "scribe"))
    assert {"message.publish", "channel.create", "channel.list", "guideline.save",
            "skill.save", "automation.save", "dashboard.save",
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


def test_message_tool_rejects_mentions_for_peer_roles(peer):
    """角色能用 message.publish 发消息、建频道、写准则,但 mentions 派发别人被拒。"""
    chat = ChatEngine(peer)
    channel = peer.get_channel("general")
    root = peer.add_message(channel.id, "human", "human", "@dev 安排复核", ["dev"])
    run_id = peer.add_chat_run(channel.id, "dev", root, root, 0)
    peer.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, peer.get_role("webshop", "dev"),
                            peer.get_backend("std-1"), root, run_id=run_id)
    token = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip()
    identity = chat.agent_tools.authenticate(token)
    publish = chat.agent_tools.capabilities(identity)["actions"]["message.publish"]
    assert "your own" in publish["arguments"]["mentions"]
    assert "cannot dispatch" in publish["description"]

    with pytest.raises(AgentToolError) as denied:
        chat.agent_tools.execute(identity, "message.publish", {
            "channel": "general", "content": "请复核。", "mentions": ["reviewer"],
        }, run_id, "peer-dispatch")
    assert denied.value.code == "permission_denied"
    assert "reviewer" not in _runs(peer)

    created = chat.agent_tools.execute(identity, "channel.create", {
        "id": "review", "name": "复核", "purpose": "金额复核"}, run_id, "peer-channel")
    assert created["channel"]["id"].endswith("review")
    posted = chat.agent_tools.execute(identity, "message.publish", {
        "channel": "review", "content": "复核清单见文档。"}, run_id, "peer-post")
    chat.wait_idle()
    assert "dispatched" not in posted
    assert _runs(peer) == ["dev"]   # 只有本轮自身,没有派生新运行


def test_peer_role_can_start_itself_in_another_channel(peer):
    """无主控时角色也能在其他频道提及自己另起会话,结果只留在那个频道。"""
    chat = ChatEngine(peer)
    channel = peer.get_channel("general")
    root = peer.add_message(channel.id, "human", "human", "@dev 去新频道做原型", ["dev"])
    run_id = peer.add_chat_run(channel.id, "dev", root, root, 0)
    peer.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, peer.get_role("webshop", "dev"),
                            peer.get_backend("std-1"), root, run_id=run_id)
    identity = chat.agent_tools.authenticate(
        Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip())

    def publish(arguments: dict, op: str) -> dict:
        return chat.agent_tools.execute(identity, "message.publish", arguments, run_id, op)

    with pytest.raises(AgentToolError) as same_channel:
        publish({"channel": "general", "content": "自己接着做", "mentions": ["dev"]}, "self-here")
    assert same_channel.value.code == "invalid_arguments"
    chat.agent_tools.execute(identity, "channel.create", {"id": "spike", "name": "spike"},
                             run_id, "peer-spike")
    with pytest.raises(AgentToolError) as with_other:
        publish({"channel": "spike", "content": "一起做", "mentions": ["dev", "reviewer"]},
                "self-and-other")
    assert with_other.value.code == "permission_denied"

    result = publish({"channel": "spike", "content": "在这里实现原型", "mentions": ["dev"]},
                     "self-spike")
    assert result["dispatched"] == ["dev"]
    assert "handoff" not in result and "separate session" in result["resume"]
    dev_turn = chat._assemble(
        peer.get_channel("webshop:spike"), peer.get_role("webshop", "dev"),
        peer.get_backend("std-1"), result["message_id"]).turn_prompt
    assert "brief you posted for yourself from another channel" in dev_turn

    chat.wait_idle()
    # 新会话只启动自己,回复留在新频道,不拉起任何人
    assert [r["role_id"] for r in peer.chat_runs_for_channel("webshop:spike")] == ["dev"]
    reply = _msgs(peer, "webshop:spike")[-1]
    assert reply["author"] == "dev" and reply["reply_to"] == result["message_id"]
    assert json.loads(reply["mentions"]) == []


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


def test_page_context_is_channel_shared_without_orchestrator(peer):
    # 无主控项目不需要指定接收角色:快照落在频道级目录,人类 @ 谁都能读到
    client = TestClient(create_app())
    body = {"page_kind": "guidelines", "page_key": "api-style.md", "content": "# x\n"}
    stored = client.post("/api/chat/general/page-context", json=body)
    assert stored.status_code == 200
    path = Path(stored.json()["path"])
    assert path.parent.parent.name == "page-context"
    assert ".missioncrew" not in path.as_posix()


def test_task_dispatch_and_manual_in_peer_mode(peer):
    chat = ChatEngine(peer)
    project = peer.get_project("webshop")
    manual = render_manual(project)
    assert "orchestrator only" not in manual
    assert "This project has no orchestrator" in manual
    assert "`message.publish`\n  rejects any other role in `mentions`" in manual
    assert "your own id starts a separate session of you" in manual
    assert "Collaboration Chain Budget" not in manual
    task = create_task(peer, "webshop", title="复核金额", channel_ids=["general"])
    with pytest.raises(ValueError, match="没有主控"):
        dispatch_task(peer, chat, task)
    sent, _ = dispatch_task(peer, chat, task, target_role_ids=["dev"])
    chat.wait_idle()
    assert sent and _runs(peer) == ["dev"]


def test_manual_only_role_stays_outside_peer_dispatch(chat, peer, monkeypatch):
    """无主控项目里仅人工点名的角色:别人的名册里没有它,它自己也只执行不派发。"""
    expert = peer.get_role("webshop", "expert")
    expert.manual_only = True
    peer.put_role(expert)
    prompts = {}

    def _start(config):
        prompts[config.role_id] = config.prompt
        return RunResult(True, "done", output=f"{config.role_id} 完成。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    content = "@dev 和 @expert 分别评估。"
    chat.post("general", "human", content,
              mention_spans=[_span(content, "dev"), _span(content, "expert")])
    chat.wait_idle()

    assert sorted(_runs(peer)) == ["dev", "expert"]
    assert "Role Roster" in prompts["dev"] and "@expert" not in prompts["dev"]
    assert "[other executor]" in prompts["dev"]
    assert "This project has no orchestrator" not in prompts["expert"]
    assert "## Role Roster" not in prompts["expert"]
    assert "You are a human-mention-only role" in prompts["expert"]

    # 它经 message.publish 点名别人不派发
    body = "@dev 帮我看看"
    chat.post("general", "expert", body, author_type="agent",
              mention_spans=[_span(body, "dev")])
    chat.wait_idle()
    assert sorted(_runs(peer)) == ["dev", "expert"]
    assert json.loads(_msgs(peer)[-1]["mentions"]) == []
