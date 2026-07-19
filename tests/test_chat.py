"""聊天协作:@ 触发、级联、防环、失败可见性。"""
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
    assert len(agents) == 1
    assert agents[0]["author"] == "dev"
    assert agents[0]["reply_to"] == msgs[0]["id"]
    assert agents[0]["root_id"] == msgs[0]["id"]


def test_unknown_role_not_triggered(chat, seeded):
    chat.post("general", "human", "@nobody 你好 @dev 在吗")
    chat.wait_idle()
    agents = {m["author"] for m in _log(seeded) if m["author_type"] == "agent"}
    assert agents == {"dev"}


def test_cascade_dev_to_reviewer(chat, seeded):
    """人类 @dev 并要求完成后请 @reviewer;dev 的回复应触发 reviewer。"""
    chat.post("general", "human", "@dev 修复金额计算,完成后请 @reviewer 复核。")
    chat.wait_idle()
    msgs = _log(seeded)
    authors = [m["author"] for m in msgs if m["author_type"] == "agent"]
    assert authors == ["dev", "reviewer"]
    dev_msg = next(m for m in msgs if m["author"] == "dev")
    rev_msg = next(m for m in msgs if m["author"] == "reviewer")
    assert rev_msg["reply_to"] == dev_msg["id"]
    assert rev_msg["depth"] == 2
    assert rev_msg["root_id"] == msgs[0]["id"]  # 同一条协作链


def test_multiple_mentions_run_in_parallel(chat, seeded):
    chat.post("general", "human", "@dev 和 @expert 分别评估一下方案 A/B。")
    chat.wait_idle()
    agents = {m["author"] for m in _log(seeded) if m["author_type"] == "agent"}
    assert agents == {"dev", "expert"}


def test_expert_role_uses_fixed_expert_runtime(chat, seeded):
    chat.post("general", "human", "@expert 分析一下这个架构问题。")
    chat.wait_idle()
    runs = seeded._query("SELECT * FROM chat_runs WHERE role_id='expert'")
    assert runs and runs[0]["backend_id"] == "exp-1"  # 默认专家角色已固定到 expert runtime


def test_depth_limit_stops_cascade(chat, seeded):
    """模拟已达深度上限的 agent 消息再 @dev:必须截断且不触发执行。"""
    root = seeded.add_message("general", "human", "human", "起始", [])
    chat.post("general", "reviewer", "@dev 继续接力。", author_type="agent",
              reply_to=root, root_id=root, depth=MAX_DEPTH)
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("级联深度上限" in m["content"] for m in msgs
               if m["author_type"] == "platform")
    assert not any(m["author"] == "dev" and m["author_type"] == "agent" for m in msgs)


def test_chain_run_budget(chat, seeded):
    """同一协作链累计执行数达到上限后,新的 @ 不再触发。"""
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
    # vision 角色要求 multimodal,唯一候选 vis-1;禁用后应把失败贴回频道
    b = seeded.get_backend("vis-1")
    b.enabled = False
    seeded.put_backend(b)
    chat.post("general", "human", "@vision 验证一下首页截图。")
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("无可用后端" in m["content"] for m in msgs
               if m["author_type"] == "platform")


def test_prompt_separates_persona_from_task(chat, seeded):
    """人格是选人画像,任务只来自触发消息;名册须含其他角色的人格供调度。"""
    msg_id = seeded.add_message("general", "human", "human", "@dev 修一下登录", ["dev"])
    cfg = chat._assemble(seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
                         seeded.get_backend("std-1"), msg_id)
    assert "角色定位" in cfg.prompt          # 人格被明确标注为定位,不是任务
    assert "任务简报" in cfg.prompt          # 任务来自发起者撰写的触发消息
    reviewer = seeded.get_role("webshop", "reviewer")
    assert reviewer.description[:10] in cfg.prompt  # 名册携带他人人格


def test_lead_dispatcher_role_seeded(seeded):
    lead = seeded.get_role("webshop", "lead")
    assert lead is not None
    assert "调度" in lead.description and "不亲自实现" in lead.description


def test_agent_messages_visible_to_human(chat, seeded):
    """Agent 之间的协作消息保存在频道消息流中,人类可完整读取。"""
    chat.post("general", "human", "@dev 处理,完成后请 @reviewer 复核。")
    chat.wait_idle()
    msgs = seeded.list_messages("general")
    # dev 发给 reviewer 的协作消息就在频道里
    dev_msg = next(m for m in msgs if m["author"] == "dev")
    assert "@reviewer" in dev_msg["content"]
