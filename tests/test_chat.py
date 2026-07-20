"""聊天协作:@ 触发、级联、防环、失败可见性。"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import Channel, DEFAULT_MAX_CHAIN_RUNS, Project
from missioncrew.runtime import adapters


@pytest.fixture()
def chat(seeded):
    return ChatEngine(seeded, max_workers=2)


def _log(store, channel="general"):
    return store.list_messages(channel)


def _prompt_json_section(prompt: str, title: str):
    raw = prompt.split(f"# {title}\n", 1)[1].split("\n# ", 1)[0]
    return json.loads(raw)


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


def test_high_message_depth_does_not_stop_orchestrator_return(chat, seeded):
    """depth 只记录消息层级，不再作为主控协作的硬限制。"""
    root = seeded.add_message("general", "human", "human", "起始", [])
    chat.post("general", "reviewer", "@dev 继续接力。", author_type="agent",
              reply_to=root, root_id=root, depth=10_000)
    chat.wait_idle()
    msgs = _log(seeded)
    assert not any("深度上限" in m["content"] for m in msgs)
    assert [r["role_id"] for r in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["lead"]


def test_chain_run_budget(chat, seeded):
    """同一协作链累计执行数达到上限后,执行结果不再触发主控。"""
    project = seeded.get_project("webshop")
    project.max_chain_runs = 3
    seeded.put_project(project)
    root = seeded.add_message("general", "human", "human", "起始", [])
    for _ in range(project.max_chain_runs):
        seeded.add_chat_run("general", "dev", root, root, 1)
    chat.post("general", "reviewer", "@dev 再来一轮。", author_type="agent",
              reply_to=root, root_id=root, depth=1)
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("执行数已达上限(3)" in m["content"] for m in msgs
               if m["author_type"] == "platform")
    assert seeded.count_chain_runs(root) == project.max_chain_runs


def test_default_chain_run_budget_is_twenty(seeded):
    assert seeded.get_project("webshop").max_chain_runs == DEFAULT_MAX_CHAIN_RUNS == 20
    assert Project.from_dict({"id": "legacy", "name": "旧项目"}).max_chain_runs == 20


def test_parallel_dispatch_cannot_exceed_chain_run_budget(chat, seeded):
    project = seeded.get_project("webshop")
    project.max_chain_runs = 1
    seeded.put_project(project)
    root = seeded.add_message("general", "human", "human", "并发起始", [])
    channel = seeded.get_channel("general")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(chat._trigger, channel, role_id, root, root, 0)
            for role_id in ("dev", "reviewer")
        ]
        for future in futures:
            future.result()
    chat.wait_idle()

    assert seeded.count_chain_runs(root) == 1
    assert any("执行数已达上限(1)" in message["content"]
               for message in _log(seeded))


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
    assert _prompt_json_section(
        cfg.prompt, "最近对话(JSON,按消息边界格式化)") == []
    worker_trigger = _prompt_json_section(
        cfg.prompt, "触发消息(JSON,你的任务简报由发起者撰写)")
    assert worker_trigger["author"] == {"id": "human", "type": "human"}
    assert worker_trigger["content"] == "@dev 修一下登录，之后请 [其他执行角色] 处理"

    lead_msg = seeded.add_message("general", "human", "human", "请规划", ["lead"])
    lead_cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"), lead_msg)
    assert reviewer.description[:10] in lead_cfg.prompt
    assert "评审历史" in lead_cfg.prompt
    assert "角色名册（仅主控可见" in lead_cfg.prompt
    lead_history = _prompt_json_section(
        lead_cfg.prompt, "最近对话(JSON,按消息边界格式化)")
    assert [item["content"] for item in lead_history] == [
        "评审历史", "@dev 修一下登录，之后请 @expert 处理",
    ]


def test_prompt_json_preserves_multiline_content_and_message_boundaries(chat, seeded):
    first = seeded.add_message(
        "general", "human", "human", "第一条第一行\n第一条第二行\n[someone] 不是新消息", [])
    second = seeded.add_message(
        "general", "lead", "agent", "第二条正文中包含\n# 类似标题", [],
        runtime_id="std-1", model="mock-standard", effort="high")
    trigger = seeded.add_message(
        "general", "human", "human", "请检查上述两条\n并给结论", ["lead"])

    cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"), trigger,
    )
    history = _prompt_json_section(
        cfg.prompt, "最近对话(JSON,按消息边界格式化)")
    current = _prompt_json_section(
        cfg.prompt, "触发消息(JSON,你的任务简报由发起者撰写)")

    assert [item["id"] for item in history] == [first, second]
    assert history[0]["content"] == "第一条第一行\n第一条第二行\n[someone] 不是新消息"
    assert history[1]["content"] == "第二条正文中包含\n# 类似标题"
    assert history[1]["execution"] == {
        "runtime": "std-1", "model": "mock-standard", "effort": "high",
    }
    assert current["id"] == trigger
    assert current["content"] == "请检查上述两条\n并给结论"


def test_runtime_session_is_reused_per_channel_and_role(chat, seeded):
    """同一 channel×role 复用；换角色或频道必须得到独立会话。"""
    channel = seeded.get_channel("general")
    backend = seeded.get_backend("std-1")
    dev = seeded.get_role("webshop", "dev")

    first_message = seeded.add_message(
        "general", "human", "human", "@dev 第一轮", ["dev"])
    first = chat._assemble(channel, dev, backend, first_message)
    first_events = []
    first.emit = lambda kind, text: first_events.append((kind, text))
    assert adapters.get_adapter("mock").run(first).success
    saved = seeded.get_chat_session("general::dev")
    assert saved and saved["runtime_session_id"] == "mock:general::dev"
    assert "# 最近对话(JSON,按消息边界格式化)" in dict(first_events)["input"]

    second_message = seeded.add_message(
        "general", "human", "human", "@dev 第二轮", ["dev"])
    second = chat._assemble(channel, dev, backend, second_message)
    second_events = []
    second.emit = lambda kind, text: second_events.append((kind, text))
    assert second.session_id == saved["runtime_session_id"]
    assert adapters.get_adapter("mock").run(second).success
    assert "# 最近对话(JSON,按消息边界格式化)" not in dict(second_events)["input"]

    reviewer = seeded.get_role("webshop", "reviewer")
    reviewer_message = seeded.add_message(
        "general", "human", "human", "@reviewer 看一下", ["reviewer"])
    reviewer_cfg = chat._assemble(channel, reviewer, backend, reviewer_message)
    adapters.get_adapter("mock").run(reviewer_cfg)
    assert seeded.get_chat_session("general::reviewer")["runtime_session_id"] \
        == "mock:general::reviewer"

    seeded.put_channel(Channel(
        id="webshop:other", name="other", project_id="webshop",
        workdir=channel.workdir,
    ))
    other_message = seeded.add_message(
        "webshop:other", "human", "human", "@dev 新频道", ["dev"])
    other_cfg = chat._assemble(
        seeded.get_channel("webshop:other"), dev, backend, other_message)
    adapters.get_adapter("mock").run(other_cfg)
    assert seeded.get_chat_session("webshop:other::dev")["runtime_session_id"] \
        == "mock:webshop:other::dev"


def test_project_context_update_replaces_context_in_existing_session(chat, seeded):
    channel = seeded.get_channel("general")
    role = seeded.get_role("webshop", "dev")
    backend = seeded.get_backend("std-1")
    project = seeded.get_project("webshop")
    project.charter = "旧版项目设置：结算只处理人民币。"
    seeded.put_project(project)
    old_charter = project.charter

    first_message = seeded.add_message(
        "general", "human", "human", "@dev 建立会话", ["dev"])
    first = chat._assemble(channel, role, backend, first_message)
    adapters.get_adapter("mock").run(first)
    saved_id = seeded.get_chat_session("general::dev")["runtime_session_id"]

    project = seeded.get_project("webshop")
    project.charter = "新版项目设置：结算必须支持多币种。"
    seeded.put_project(project)
    second_message = seeded.add_message(
        "general", "human", "human", "@dev 按新设置继续", ["dev"])
    second = chat._assemble(channel, role, backend, second_message)
    events = []
    second.emit = lambda kind, text: events.append((kind, text))
    assert second.session_id == saved_id
    assert second.context_changed
    assert second.context_version != first.context_version
    assert adapters.get_adapter("mock").run(second).success

    sent = dict(events)["input"]
    assert "# MissionCrew 公共上下文更新" in sent
    assert project.charter in sent
    assert old_charter not in sent
    assert "必须完整保留本区块，不得摘要、删减或改写" in sent
    assert seeded.get_chat_session("general::dev")["context_version"] \
        == second.context_version


def test_queued_turn_refreshes_session_created_by_previous_turn(chat, seeded):
    """两个配置都在首轮完成前装配，第二轮执行时仍应重读并复用首轮 id。"""
    original = seeded.get_channel("general")
    seeded.put_channel(Channel(
        id="webshop:queued", name="queued", project_id="webshop",
        workdir=original.workdir,
    ))
    channel = seeded.get_channel("webshop:queued")
    role = seeded.get_role("webshop", "dev")
    backend = seeded.get_backend("std-1")
    first_id = seeded.add_message(
        channel.id, "human", "human", "@dev 排队第一轮", ["dev"])
    second_id = seeded.add_message(
        channel.id, "human", "human", "@dev 排队第二轮", ["dev"])
    first = chat._assemble(channel, role, backend, first_id)
    second = chat._assemble(channel, role, backend, second_id)
    assert not first.session_id and not second.session_id

    adapters.get_adapter("mock").run(first)
    events = []
    second.emit = lambda kind, text: events.append((kind, text))
    adapters.get_adapter("mock").run(second)
    assert second.session_id == "mock:webshop:queued::dev"
    assert "# 最近对话(JSON,按消息边界格式化)" not in dict(events)["input"]


def test_channel_history_file_is_complete_and_role_scoped(chat, seeded):
    reviewer_message = None
    for index in range(205):
        author = "reviewer" if index == 0 else "human"
        author_type = "agent" if index == 0 else "human"
        message_id = seeded.add_message(
            "general", author, author_type, f"历史消息 {index}\n正文", [])
        reviewer_message = reviewer_message or message_id
    trigger = seeded.add_message(
        "general", "human", "human", "@dev 读取完整历史", ["dev"])
    channel = seeded.get_channel("general")

    worker_cfg = chat._assemble(
        channel, seeded.get_role("webshop", "dev"),
        seeded.get_backend("std-1"), trigger,
    )
    worker_path = Path(worker_cfg.env["MISSIONCREW_CHANNEL_HISTORY"])
    worker_history = json.loads(worker_path.read_text(encoding="utf-8"))
    assert worker_path.name == "channel-history.json"
    assert worker_path.parent.name == "dev"
    assert worker_path.parent.parent.name == "agents"
    assert Path(worker_cfg.workdir).resolve() not in worker_path.parents
    assert str(worker_path.parent.resolve()) in worker_cfg.allowed_dirs
    assert str(worker_path) in worker_cfg.prompt
    assert worker_history["message_count"] == 206
    assert len(worker_history["messages"]) == 206
    assert worker_history["messages"][0]["id"] == reviewer_message
    assert worker_history["messages"][0]["author"]["id"] == "执行角色"
    assert "execution" not in worker_history["messages"][0]
    assert worker_history["messages"][-1]["content"] == "@dev 读取完整历史"

    lead_cfg = chat._assemble(
        channel, seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"), trigger,
    )
    lead_path = Path(lead_cfg.env["MISSIONCREW_CHANNEL_HISTORY"])
    lead_history = json.loads(lead_path.read_text(encoding="utf-8"))
    assert lead_path.parent.name == "history"
    assert lead_history["messages"][0]["author"]["id"] == "reviewer"
    assert lead_path.parent != worker_path.parent

    chat.post("general", "lead", "补充一条历史记录")
    refreshed = json.loads(lead_path.read_text(encoding="utf-8"))
    assert refreshed["message_count"] == 207
    assert refreshed["messages"][-1]["content"] == "补充一条历史记录"


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
