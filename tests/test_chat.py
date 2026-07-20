"""聊天协作:@ 触发、级联、防环、失败可见性。"""
import json

import pytest

from missioncrew.collab.chat import MAX_CHAIN_RUNS, MAX_DEPTH, ChatEngine


@pytest.fixture()
def chat(seeded):
    return ChatEngine(seeded, max_workers=2)


def _log(store, channel="general"):
    return store.list_messages(channel)


def test_mention_triggers_agent_reply(chat, seeded):
    chat.post("general", "human", "@dev 看一下购物车模块的异常处理。")
    chat.wait_idle()
    msgs = _log(seeded)
    agents = [m for m in msgs if m["author_type"] == "agent"]
    assert [m["author"] for m in agents] == ["dev", "lead"]
    assert agents[0]["reply_to"] == msgs[0]["id"]
    assert agents[0]["root_id"] == msgs[0]["id"]
    assert json.loads(agents[0]["mentions"]) == ["lead"]


def test_unknown_role_not_triggered(chat, seeded):
    chat.post("general", "human", "@nobody 你好 @dev 在吗")
    chat.wait_idle()
    agents = {m["author"] for m in _log(seeded) if m["author_type"] == "agent"}
    assert agents == {"dev", "lead"}


# ---- 人类不 @ 任何角色时默认交给项目主控 ----

def test_human_message_without_mention_goes_to_orchestrator(chat, seeded):
    chat.post("general", "human", "这个项目的支付流程现在怎么样了?")
    chat.wait_idle()
    msgs = _log(seeded)
    assert json.loads(msgs[0]["mentions"]) == ["lead"]   # 落库的提及即默认主控
    agents = [m for m in msgs if m["author_type"] == "agent"]
    assert [m["author"] for m in agents] == ["lead"]
    assert agents[0]["reply_to"] == msgs[0]["id"]


def test_unrecognized_mention_still_falls_back_to_orchestrator(chat, seeded):
    """@ 了不存在的角色 = 没有有效提及,同样交给主控,不至于没人响应。"""
    chat.post("general", "human", "@nobody 帮我看看")
    chat.wait_idle()
    agents = {m["author"] for m in _log(seeded) if m["author_type"] == "agent"}
    assert agents == {"lead"}


def test_worker_reply_automatically_returns_to_orchestrator(chat, seeded):
    """执行角色无需知道主控 id；平台把完整结果自动交回主控。"""
    chat.post("general", "human", "@dev 简单看一下就行,不用找别人。")
    chat.wait_idle()
    authors = [m["author"] for m in _log(seeded) if m["author_type"] == "agent"]
    assert authors == ["dev", "lead"]


def test_orchestrator_self_message_not_looped_back(chat, seeded):
    """主控自己发言(Agent 回复与以主控身份调用接口)都不触发自己。"""
    chat.post("general", "lead", "我先梳理一下需求。", author_type="agent")
    chat.wait_idle()
    assert [m["author"] for m in _log(seeded) if m["author_type"] == "agent"] == ["lead"]
    # 以主控身份调接口(author=lead,类型默认 human)同样不自我补 @
    chat.post("general", "lead", "继续跟进。")
    chat.wait_idle()
    msgs = _log(seeded)
    assert json.loads(msgs[-1]["mentions"]) == []
    assert [m["author"] for m in msgs if m["author_type"] == "agent"] == ["lead"]


def test_channel_without_project_has_no_default_target(seeded):
    """无项目归属的频道没有主控可默认;不补 @,也不该报错。"""
    from missioncrew.core.models import Channel
    seeded.put_channel(Channel(id="orphan", name="孤儿频道", project_id=None))
    engine = ChatEngine(seeded, max_workers=2)
    engine.post("orphan", "human", "有人吗")
    engine.wait_idle()
    msgs = seeded.list_messages("orphan")
    assert json.loads(msgs[0]["mentions"]) == []
    assert not [m for m in msgs if m["author_type"] == "agent"]


def test_worker_cannot_dispatch_reviewer_and_returns_to_lead(chat, seeded):
    """执行角色即使输出 @reviewer，也不能横向触发，只能返回主控。"""
    root = seeded.add_message("general", "human", "human", "开始", [])
    chat.post("general", "dev", "完整结果。@reviewer 请继续复核。",
              author_type="agent", reply_to=root, root_id=root, depth=1)
    chat.wait_idle()
    msgs = _log(seeded)
    authors = [m["author"] for m in msgs if m["author_type"] == "agent"]
    assert authors == ["dev", "lead"]
    dev_msg = next(m for m in msgs if m["author"] == "dev")
    assert "@reviewer" in dev_msg["content"]    # 原始结果不篡改
    assert json.loads(dev_msg["mentions"]) == ["lead"]
    assert not any(r["role_id"] == "reviewer" for r in
                   seeded._query("SELECT role_id FROM chat_runs"))


def test_only_orchestrator_agent_can_dispatch_other_roles(chat, seeded):
    root = chat.post("general", "lead", "@reviewer 请复核金额计算。",
                     author_type="agent")
    chat.wait_idle()
    msgs = _log(seeded)
    reviewer = next(m for m in msgs if m["author"] == "reviewer")
    assert reviewer["reply_to"] == root
    assert json.loads(reviewer["mentions"]) == ["lead"]
    assert [r["role_id"] for r in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["reviewer", "lead"]


def test_multiple_mentions_run_in_parallel(chat, seeded):
    chat.post("general", "human", "@dev 和 @expert 分别评估一下方案 A/B。")
    chat.wait_idle()
    agents = {m["author"] for m in _log(seeded) if m["author_type"] == "agent"}
    assert agents == {"dev", "expert", "lead"}


def test_expert_role_uses_fixed_expert_runtime(chat, seeded):
    chat.post("general", "human", "@expert 分析一下这个架构问题。")
    chat.wait_idle()
    runs = seeded._query("SELECT * FROM chat_runs WHERE role_id='expert'")
    assert runs and runs[0]["backend_id"] == "exp-1"  # 默认专家角色已固定到 expert runtime


def test_depth_limit_stops_cascade(chat, seeded):
    """执行角色在深度上限回主控时必须截断且不再触发执行。"""
    root = seeded.add_message("general", "human", "human", "起始", [])
    chat.post("general", "reviewer", "@dev 继续接力。", author_type="agent",
              reply_to=root, root_id=root, depth=MAX_DEPTH)
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("级联深度上限" in m["content"] for m in msgs
               if m["author_type"] == "platform")
    assert not seeded._query("SELECT id FROM chat_runs")


def test_chain_run_budget(chat, seeded):
    """同一协作链累计执行数达到上限后,执行结果不再触发主控。"""
    root = seeded.add_message("general", "human", "human", "起始", [])
    for _ in range(MAX_CHAIN_RUNS):
        seeded.add_chat_run("general", "dev", root, root, 1)
    chat.post("general", "reviewer", "@dev 再来一轮。", author_type="agent",
              reply_to=root, root_id=root, depth=1)
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("执行数已达上限" in m["content"] for m in msgs
               if m["author_type"] == "platform")
    assert seeded.count_chain_runs(root) == MAX_CHAIN_RUNS


def test_agent_failure_posted_to_channel(chat, seeded):
    # vision 固定 runtime 停用后，失败须公开并自动交回主控。
    b = seeded.get_backend("vis-1")
    b.enabled = False
    seeded.put_backend(b)
    chat.post("general", "human", "@vision 验证一下首页截图。")
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("无可用后端" in m["content"] for m in msgs
               if m["author_type"] == "platform")
    assert any(r["role_id"] == "lead" for r in
               seeded._query("SELECT role_id FROM chat_runs"))


def test_prompt_separates_worker_context_from_orchestrator_roster(chat, seeded):
    """执行角色只拿任务；只有主控能看到其他角色名册和最近对话。"""
    seeded.add_message("general", "reviewer", "agent", "评审历史", [])
    msg_id = seeded.add_message(
        "general", "human", "human", "@dev 修一下登录，之后请 @expert 处理", ["dev"])
    cfg = chat._assemble(seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
                         seeded.get_backend("std-1"), msg_id)
    assert "角色定位" in cfg.prompt          # 人格被明确标注为定位,不是任务
    assert "任务简报" in cfg.prompt          # 任务来自发起者撰写的触发消息
    reviewer = seeded.get_role("webshop", "reviewer")
    assert reviewer.description[:10] not in cfg.prompt
    assert "评审历史" not in cfg.prompt
    assert "@expert" not in cfg.prompt and "[其他执行角色]" in cfg.prompt
    assert "看不到其他执行角色名册" in cfg.prompt

    lead_msg = seeded.add_message("general", "human", "human", "请规划", ["lead"])
    lead_cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"), lead_msg)
    assert reviewer.description[:10] in lead_cfg.prompt
    assert "评审历史" in lead_cfg.prompt
    assert "角色名册（仅主控可见" in lead_cfg.prompt


def test_lead_dispatcher_role_seeded(seeded):
    lead = seeded.get_role("webshop", "lead")
    assert lead is not None
    assert "调度" in lead.description and "不亲自实现" in lead.description


def test_dispatch_and_worker_return_are_visible_to_human(chat, seeded):
    """执行结果与自动回主控的闭环都保存在频道消息流中。"""
    chat.post("general", "human", "@dev 处理,完成后请 @reviewer 复核。")
    chat.wait_idle()
    msgs = seeded.list_messages("general")
    dev_msg = next(m for m in msgs if m["author"] == "dev")
    assert dev_msg["content"]
    assert json.loads(dev_msg["mentions"]) == ["lead"]
    assert any(m["author"] == "lead" for m in msgs if m["author_type"] == "agent")
