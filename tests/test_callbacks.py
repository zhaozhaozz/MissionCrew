"""角色登记的一次性回调:登记校验、三类触发条件、回到原频道启动原角色、取消与过期。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import callbacks as callbacks_mod
from missioncrew.collab.agent_tools import AgentToolError
from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import Channel, Task


@pytest.fixture
def chat(seeded):
    engine = ChatEngine(seeded, max_workers=2)
    yield engine
    engine.shutdown()


def _start_run(store, chat: ChatEngine, role_id: str, author_type="human",
               author="human"):
    """模拟一轮正在执行的运行,返回其 Agent Tool 身份与运行 id。"""
    channel = store.get_channel("general")
    trigger = store.add_message(channel.id, author, author_type,
                                f"@{role_id} 处理", [role_id])
    run_id = store.add_chat_run(channel.id, role_id, trigger, trigger, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, store.get_role("webshop", role_id),
                            store.get_backend("std-1"), trigger, run_id=run_id)
    token = open(config.env["MISSIONCREW_AGENT_TOKEN_FILE"], encoding="utf-8").read().strip()
    return chat.agent_tools.authenticate(token), run_id, config


def _call(chat, identity, action, arguments):
    return chat.agent_tools.execute(identity, action, arguments, run_id=None,
                                    request_id=f"req-{time.time_ns()}")


def _finish(store, run_id):
    store.update_chat_run(run_id, "done")


def _callback_messages(store, channel="general"):
    return [m for m in store.list_messages(channel) if m["kind"] == "callback"]


def _add_channel(store, short_id="ops"):
    channel = Channel(id=f"webshop:{short_id}", name=short_id, project_id="webshop")
    store.put_channel(channel)
    return channel


def test_timer_callback_starts_role_again_in_its_channel(chat, seeded):
    identity, run_id, config = _start_run(seeded, chat, "dev")
    assert "callback.create" in config.common_prompt
    assert "register `callback.create` and end the turn" in config.common_prompt
    result = _call(chat, identity, "callback.create",
                   {"note": "额度恢复后接着跑测试并提交", "delay_seconds": 120})
    callback = result["callback"]
    assert callback["channel"] == "general" and callback["role_id"] == "dev"
    assert callback["watch"] is None and callback["status"] == "pending"
    # 登记留下回执,人能在频道里看见
    receipts = [m for m in seeded.list_messages("general") if m["kind"] == "agent_tool"]
    assert "callback.create" in receipts[-1]["content"]
    assert f"#{callback['id']}" in receipts[-1]["content"]
    _finish(seeded, run_id)

    chat.callbacks.tick(now=time.time() + 60)
    assert _callback_messages(seeded) == []
    chat.callbacks.tick(now=time.time() + 121)
    chat.wait_idle()

    [fired] = _callback_messages(seeded)
    assert fired["author_type"] == "platform"
    assert fired["content"].startswith(f"@dev 你登记的回调 #{callback['id']} 已触发")
    assert "额度恢复后接着跑测试并提交" in fired["content"]
    row = seeded.get_chat_callback(callback["id"])
    assert row["status"] == "fired" and row["message_id"] == fired["id"]
    # 回调消息照常启动 dev:新协作链,运行有自己的令牌
    [run] = [r for r in seeded.chat_runs_for_trigger(fired["id"])]
    assert run["role_id"] == "dev" and run["root_id"] == fired["id"]
    assert run["status"] == "done"
    # 登记那轮是人类直接点名:回调这轮的结果同样留在频道,不交回主控
    assert not [r for r in seeded.chat_runs_for_channel("general")
                if r["role_id"] == "lead"]
    # 只触发一次
    chat.callbacks.tick(now=time.time() + 600)
    assert len(_callback_messages(seeded)) == 1


def test_callback_trigger_is_labelled_for_the_role(chat, seeded):
    identity, run_id, _config = _start_run(seeded, chat, "dev")
    _call(chat, identity, "callback.create", {"note": "回来看看", "delay_seconds": 60})
    _finish(seeded, run_id)
    chat.callbacks.tick(now=time.time() + 61)
    chat.wait_idle()
    [fired] = _callback_messages(seeded)
    config = chat._assemble(seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
                            seeded.get_backend("std-1"), fired["id"])
    assert "a callback you registered with `callback.create` fired" in config.turn_prompt


def test_executor_callback_result_returns_to_orchestrator_when_lead_dispatched(chat, seeded):
    """登记那轮由主控派发:回调这轮的结果按常规交回主控验收。"""
    identity, run_id, _config = _start_run(
        seeded, chat, "dev", author_type="agent", author="lead")
    _call(chat, identity, "callback.create", {"note": "复查", "delay_seconds": 60})
    _finish(seeded, run_id)
    chat.callbacks.tick(now=time.time() + 61)
    chat.wait_idle()
    assert [r for r in seeded.chat_runs_for_channel("general") if r["role_id"] == "lead"]


def test_channel_watch_fires_on_message_from_others_only(chat, seeded):
    ops = _add_channel(seeded)
    identity, run_id, _config = _start_run(seeded, chat, "dev")
    result = _call(chat, identity, "callback.create",
                   {"note": "ops 有人回复后继续", "channel": "ops"})
    callback = result["callback"]
    assert callback["watch"] == {"kind": "channel", "target": "ops"}
    assert callback["expires_at"] > time.time() + 6 * 86400   # 只盯更新默认等 7 天
    _finish(seeded, run_id)

    # 自己的消息与平台消息不算更新
    seeded.add_message(ops.id, "dev", "agent", "我先发一句", [])
    seeded.add_message(ops.id, "platform", "platform", "回执", [], kind="agent_tool")
    chat.callbacks.tick()
    assert _callback_messages(seeded) == []

    seeded.add_message(ops.id, "human", "human", "部署好了，可以继续", [])
    chat.callbacks.tick()
    chat.wait_idle()
    [fired] = _callback_messages(seeded)
    assert "频道 #ops 有新消息（human：部署好了，可以继续）" in fired["content"]
    assert seeded.get_chat_callback(callback["id"])["status"] == "fired"


def test_task_watch_ignores_own_changes_and_noop_rewrites(chat, seeded):
    task = Task(id="t_watch", project_id="webshop", title="上线检查",
                labels=["status: 处理中"])
    seeded.put_task(task)
    identity, run_id, _config = _start_run(seeded, chat, "dev")
    callback = _call(chat, identity, "callback.create",
                     {"note": "Task 有变化就复核", "task": "t_watch"})["callback"]
    # 登记者自己写简报:顺延基线,不把自己叫醒
    _call(chat, identity, "task.brief", {"id": "t_watch", "content": "我先看日志"})
    chat.callbacks.tick()
    assert _callback_messages(seeded) == []
    _finish(seeded, run_id)

    # 数据源同步式的原样重写只刷新 updated_at,不算更新
    seeded.put_task(seeded.get_task("t_watch"))
    chat.callbacks.tick()
    assert _callback_messages(seeded) == []

    changed = seeded.get_task("t_watch")
    changed.labels = ["status: 已完成"]
    seeded.put_task(changed)
    chat.callbacks.tick()
    chat.wait_idle()
    [fired] = _callback_messages(seeded)
    assert "Task「上线检查」（t_watch）有更新，当前状态：已完成" in fired["content"]
    assert seeded.get_chat_callback(callback["id"])["status"] == "fired"


def test_watch_with_time_fires_at_deadline_and_watch_alone_expires(chat, seeded):
    _add_channel(seeded)
    identity, run_id, _config = _start_run(seeded, chat, "dev")
    deadline = _call(chat, identity, "callback.create",
                     {"note": "到点没人回也要跟进", "channel": "ops",
                      "delay_seconds": 300})["callback"]
    watch_only = _call(chat, identity, "callback.create",
                       {"note": "等 ops 回复", "channel": "ops"})["callback"]
    _finish(seeded, run_id)

    chat.callbacks.tick(now=time.time() + 301)
    chat.wait_idle()
    [fired] = _callback_messages(seeded)
    assert "到点，频道 #ops 有新消息仍未发生" in fired["content"]
    assert seeded.get_chat_callback(deadline["id"])["status"] == "fired"

    chat.callbacks.tick(now=time.time() + callbacks_mod.WATCH_TTL_SECONDS + 1)
    expired = seeded.get_chat_callback(watch_only["id"])
    assert expired["status"] == "expired"
    notice = _callback_messages(seeded)[-1]
    assert "已过期" in notice["content"] and "未触发" in notice["content"]
    # 过期提示不启动任何角色
    assert seeded.chat_runs_for_trigger(notice["id"]) == []


@pytest.mark.parametrize("arguments,fragment", [
    ({"delay_seconds": 60}, "note 必填"),
    ({"note": "x"}, "至少给出"),
    ({"note": "x", "delay_seconds": 5}, "至少在 30 秒之后"),
    ({"note": "x", "delay_seconds": 60, "at": "2030-01-01T00:00:00Z"}, "只能给一个"),
    ({"note": "x", "at": "2030-01-01T00:00:00"}, "必须带时区"),
    ({"note": "x", "channel": "general", "task": "t1"}, "只能盯一个"),
    ({"note": "x", "channel": "nope"}, "频道不存在"),
    ({"note": "x", "task": "missing"}, "Task 不存在"),
])
def test_callback_create_validates_arguments(chat, seeded, arguments, fragment):
    identity, _run_id, _config = _start_run(seeded, chat, "dev")
    with pytest.raises(AgentToolError) as error:
        _call(chat, identity, "callback.create", arguments)
    assert fragment in error.value.message


def test_pending_callbacks_per_owner_are_capped(chat, seeded, monkeypatch):
    monkeypatch.setattr(callbacks_mod, "MAX_PENDING_PER_OWNER", 2)
    identity, _run_id, _config = _start_run(seeded, chat, "dev")
    for _ in range(2):
        _call(chat, identity, "callback.create", {"note": "x", "delay_seconds": 60})
    with pytest.raises(AgentToolError) as error:
        _call(chat, identity, "callback.create", {"note": "x", "delay_seconds": 60})
    assert "上限 2" in error.value.message


def test_cancel_rules_and_channel_stop(chat, seeded):
    dev, dev_run, _config = _start_run(seeded, chat, "dev")
    mine = _call(chat, dev, "callback.create", {"note": "a", "delay_seconds": 60})["callback"]
    other = _call(chat, dev, "callback.create", {"note": "b", "delay_seconds": 60})["callback"]
    assert [item["id"] for item in _call(chat, dev, "callback.list", {})["callbacks"]] == [
        other["id"], mine["id"]]
    _call(chat, dev, "callback.cancel", {"id": mine["id"]})
    assert seeded.get_chat_callback(mine["id"])["status"] == "cancelled"
    with pytest.raises(AgentToolError):
        _call(chat, dev, "callback.cancel", {"id": mine["id"]})   # 已结束
    _finish(seeded, dev_run)

    # 主控能取消项目里任何角色的回调
    lead, lead_run, _config = _start_run(seeded, chat, "lead")
    _call(chat, lead, "callback.cancel", {"id": f"#{other['id']}"})
    assert seeded.get_chat_callback(other["id"])["status"] == "cancelled"
    _finish(seeded, lead_run)

    # 停止频道 Agent = 不再调度:等待中的回调一并取消
    dev, dev_run, _config = _start_run(seeded, chat, "dev")
    pending = _call(chat, dev, "callback.create", {"note": "c", "delay_seconds": 60})["callback"]
    result = chat.stop_channel_agents("general")
    assert result["cancelled_callbacks"] == 1
    assert seeded.get_chat_callback(pending["id"])["status"] == "cancelled"
    assert "取消 1 个等待中的回调" in seeded.list_messages("general")[-1]["content"]


def test_executor_cannot_cancel_other_roles_callbacks(chat, seeded):
    lead, lead_run, _config = _start_run(seeded, chat, "lead")
    callback = _call(chat, lead, "callback.create",
                     {"note": "主控的提醒", "delay_seconds": 60})["callback"]
    _finish(seeded, lead_run)
    dev, _dev_run, _config = _start_run(seeded, chat, "dev")
    with pytest.raises(AgentToolError) as error:
        _call(chat, dev, "callback.cancel", {"id": callback["id"]})
    assert error.value.code == "permission_denied"


def test_callback_cancelled_when_owner_role_disabled(chat, seeded):
    identity, run_id, _config = _start_run(seeded, chat, "dev")
    callback = _call(chat, identity, "callback.create",
                     {"note": "x", "delay_seconds": 60})["callback"]
    _finish(seeded, run_id)
    role = seeded.get_role("webshop", "dev")
    role.enabled = False
    seeded.put_role(role)
    chat.callbacks.tick(now=time.time() + 61)
    row = seeded.get_chat_callback(callback["id"])
    assert row["status"] == "cancelled" and row["reason"] == "角色已停用"
    assert _callback_messages(seeded) == []


def test_chat_api_lists_and_cancels_channel_callbacks(seeded):
    client = TestClient(create_app())
    chat = ChatEngine(seeded)
    identity, _run_id, _config = _start_run(seeded, chat, "dev")
    callback = _call(chat, identity, "callback.create",
                     {"note": "页面可见", "delay_seconds": 90})["callback"]
    payload = client.get("/api/chat/general/messages?tail=true").json()
    assert [item["id"] for item in payload["callbacks"]] == [callback["id"]]
    assert payload["callbacks"][0]["conditions"].endswith("到点")
    response = client.post(f"/api/chat/callbacks/{callback['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["callback"]["status"] == "cancelled"
    assert client.post(f"/api/chat/callbacks/{callback['id']}/cancel").status_code == 409
    assert client.get("/api/chat/general/messages?tail=true").json()["callbacks"] == []
