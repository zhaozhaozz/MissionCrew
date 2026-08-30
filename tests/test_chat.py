"""聊天协作:@ 触发、级联、防环、失败可见性。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.agent_tools import DispatchInactiveError
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.documents import library_for
from missioncrew.core.models import (Backend, Channel, DEFAULT_MAX_CHAIN_RUNS,
                                     Project, RunResult)
from missioncrew.runtime import adapters, runtime_manager


@pytest.fixture()
def chat(seeded):
    return ChatEngine(seeded, max_workers=2)


def _log(store, channel="general"):
    return store.list_messages(channel)


def _prompt_json_section(prompt: str, title: str):
    raw = prompt.split(f"# {title}\n", 1)[1].split("\n# ", 1)[0]
    return json.loads(raw)


def test_mention_triggers_agent_reply(chat, seeded):
    chat.post("general", "human", "@[dev] 看一下购物车模块的异常处理。")
    chat.wait_idle()
    msgs = _log(seeded)
    agents = [m for m in msgs if m["author_type"] == "agent"]
    assert [m["author"] for m in agents] == ["dev"]
    assert agents[0]["reply_to"] == msgs[0]["id"]
    assert agents[0]["root_id"] == msgs[0]["id"]
    assert json.loads(agents[0]["mentions"]) == []


def test_message_context_is_persisted_and_added_to_runtime_trigger(chat, seeded):
    page_context = {
        "page_collaboration": {
            "page_kind": "docs",
            "current_item": "architecture.md",
            "selection": {"line_start": 4, "line_end": 6},
        },
    }
    message_id = seeded.add_message(
        "general", "human", "human", "解释这几行", [],
        context=page_context,
    )

    stored = seeded.get_message(message_id)
    record = chat._message_record(
        stored,
        seeded.get_role("webshop", "lead"),
        seeded.get_project("webshop"),
    )

    assert stored["content"] == "解释这几行"
    assert json.loads(stored["context"]) == page_context
    assert record["content"] == "解释这几行"
    assert record["context"] == page_context


def test_agent_document_links_are_published_as_resource_urls(
        chat, seeded, monkeypatch):
    library = library_for("webshop")
    document = library.root / "architecture" / "current.md"
    output = f"正式文档：[current.md]({document})"

    def _start(config):
        config.emit("tool_result", f"已写入 {document}")
        config.emit("text", output)
        return RunResult(True, "ok", output=output)

    monkeypatch.setattr(runtime_manager, "start", _start)

    chat.post("general", "human", "@[dev] 发布文档。")
    chat.wait_idle()

    replies = [m["content"] for m in _log(seeded) if m["author_type"] == "agent"]
    assert replies
    assert all(str(library.root) not in reply for reply in replies)
    assert all("[current.md](/resources/webshop/documents/architecture/current.md)"
               in reply for reply in replies)
    events = seeded._query("SELECT kind, content FROM run_events ORDER BY id")
    assert all(str(library.root) not in event["content"] for event in events)
    assert all(event["kind"] != "text" for event in events)
    assert any("/resources/webshop/documents/architecture/current.md"
               in event["content"] for event in events)

    legacy_content = f"旧链接：[current.md]({document}) @dev"
    mention_start = legacy_content.index("@dev")
    legacy_id = seeded.add_message(
        "general", "lead", "agent", legacy_content, ["dev"],
        mention_spans=[{
            "role_id": "dev", "start": mention_start,
            "end": mention_start + len("@dev"),
        }])
    api_messages = TestClient(create_app()).get(
        "/api/chat/general/messages").json()["messages"]
    legacy = next(message for message in api_messages if message["id"] == legacy_id)
    assert str(library.root) not in legacy["content"]
    assert "/resources/webshop/documents/architecture/current.md" in legacy["content"]
    normalized_mention = legacy["content"].index("@dev")
    assert legacy["mention_spans"] == [{
        "role_id": "dev", "start": normalized_mention,
        "end": normalized_mention + len("@dev"),
    }]


def test_plain_and_unknown_mentions_are_text_and_fall_back_to_lead(chat, seeded):
    chat.post("general", "human", "@nobody 你好 @dev 在吗")
    chat.wait_idle()
    agents = {m["author"] for m in _log(seeded) if m["author_type"] == "agent"}
    assert agents == {"lead"}


def test_selected_scribe_does_not_dispatch_plain_dev_reference(chat, seeded):
    """回归：只有选择器确认的 @scribe 合法，正文 @dev 只是取证来源。"""
    content = "@scribe 请依据 @dev 已验收的源码取证报告创建架构文档。"
    chat.post("general", "human", content, mention_spans=[
        {"role_id": "scribe", "start": 0, "end": len("@scribe")},
    ])
    chat.wait_idle()

    message = _log(seeded)[0]
    assert json.loads(message["mentions"]) == ["scribe"]
    assert json.loads(message["mention_spans"]) == [
        {"role_id": "scribe", "start": 0, "end": len("@scribe")},
    ]
    api_message = TestClient(create_app()).get(
        "/api/chat/general/messages").json()["messages"][0]
    assert api_message["mention_spans"] == [
        {"role_id": "scribe", "start": 0, "end": len("@scribe")},
    ]
    assert [row["role_id"] for row in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["scribe"]


def test_human_picker_offsets_use_unicode_code_points(chat, seeded):
    content = "😀 @dev 检查 Unicode 偏移"
    chat.post("general", "human", content, mention_spans=[
        {"role_id": "dev", "start": 2, "end": 6},
    ])
    chat.wait_idle()
    assert json.loads(_log(seeded)[0]["mention_spans"]) == [
        {"role_id": "dev", "start": 2, "end": 6},
    ]


def test_invalid_picker_range_is_rejected(chat, seeded):
    with pytest.raises(ValueError, match="提及范围无效"):
        chat.post("general", "human", "@dev 检查", mention_spans=[
            {"role_id": "dev", "start": 1, "end": 5},
        ])
    assert _log(seeded) == []


def test_disabled_role_cannot_be_selected_or_dispatched(chat, seeded):
    role = seeded.get_role("webshop", "dev")
    role.enabled = False
    seeded.put_role(role)

    with pytest.raises(ValueError, match="角色不存在或不可用: @dev"):
        chat.post("general", "human", "@[dev] 检查")
    with pytest.raises(ValueError, match="提及范围无效"):
        chat.post("general", "human", "@dev 检查", mention_spans=[
            {"role_id": "dev", "start": 0, "end": len("@dev")},
        ])
    assert _log(seeded) == []
    assert seeded._query("SELECT * FROM chat_runs") == []

    trigger = seeded.add_message(
        "general", "human", "human", "内部入口防御", [])
    chat._trigger(
        seeded.get_channel("general"), "dev", trigger, trigger, 0)
    chat.wait_idle()
    assert seeded._query("SELECT * FROM chat_runs") == []
    assert _log(seeded)[-1]["content"] == "@dev 不存在，本次不触发执行。"
    assert "停用" not in _log(seeded)[-1]["content"]


def test_disabled_role_is_absent_from_orchestrator_roster(seeded):
    role = seeded.get_role("webshop", "dev")
    role.enabled = False
    seeded.put_role(role)
    message_id = seeded.add_message(
        "general", "human", "human", "请选择合适角色", [])
    chat = ChatEngine(seeded)
    cfg = chat._assemble(
        seeded.get_channel("general"),
        seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"),
        message_id,
    )
    assert "@dev(" not in cfg.common_prompt
    assert "@reviewer(" in cfg.common_prompt
    assert "无其他已启用角色" not in cfg.common_prompt


def test_message_api_rejects_forged_picker_range(seeded):
    client = TestClient(create_app())
    response = client.post("/api/chat/general/messages", json={
        "author": "human", "content": "@dev 检查",
        "mentions": [{"role_id": "dev", "start": 1, "end": 5}],
    })
    assert response.status_code == 400
    assert "提及范围无效" in response.json()["detail"]
    assert seeded.list_messages("general") == []


def test_orchestrator_text_mentions_never_dispatch(chat, seeded):
    """主控消息正文里的 @ 与 @[ ] 都是普通文字;派发只认结构化范围。"""
    chat.post("general", "lead", "@scribe 请参考 @dev 的报告。", author_type="agent")
    chat.wait_idle()
    assert seeded._query("SELECT * FROM chat_runs") == []

    kept = chat.post("general", "lead", "@[scribe] 请参考 @dev 的报告。",
                     author_type="agent")
    chat.wait_idle()
    stored = seeded.get_message(kept)
    assert stored["content"].startswith("@[scribe]")   # 旧语法保留为字面文本
    assert json.loads(stored["mentions"]) == []
    assert json.loads(stored["mention_spans"]) == []
    assert seeded._query("SELECT * FROM chat_runs") == []

    # message.publish 显式命令生成的结构化范围才会派发
    root = chat.post("general", "lead", "@scribe\n\n请复核报告。",
                     author_type="agent",
                     mention_spans=[{"role_id": "scribe", "start": 0, "end": 7}])
    chat.wait_idle()
    stored = seeded.get_message(root)
    assert json.loads(stored["mentions"]) == ["scribe"]
    assert json.loads(stored["mention_spans"])[0]["role_id"] == "scribe"
    assert {row["role_id"] for row in seeded._query(
        "SELECT role_id FROM chat_runs")} == {"scribe", "lead"}


def test_archived_channel_is_read_only_until_agent_reactivates(chat, seeded):
    channel = Channel(id="webshop:old-topic", name="old-topic", project_id="webshop",
                      archived=True, archived_at=123.0)
    seeded.put_channel(channel)

    with pytest.raises(ValueError, match="频道已归档"):
        chat.post(channel.id, "human", "继续讨论", mention_spans=[])
    assert seeded.list_messages(channel.id) == []

    message_id = chat.post(channel.id, "lead", "重新启用这个频道。", author_type="agent")
    chat.wait_idle()
    restored = seeded.get_channel(channel.id)
    assert restored.archived is False and restored.archived_at == 0
    assert seeded.get_message(message_id)["author"] == "lead"
    audit = seeded.list_audit(limit=10)
    assert any(row["action"] == "channel_reactivated" for row in audit)


def test_channels_sort_general_then_latest_message_and_filter_archived(seeded):
    seeded.put_channel(Channel(id="webshop:older", name="older", project_id="webshop",
                               created_at=10))
    seeded.put_channel(Channel(id="webshop:newer", name="newer", project_id="webshop",
                               created_at=20))
    seeded.put_channel(Channel(id="webshop:archived", name="archived",
                               project_id="webshop", archived=True, created_at=30))
    older_message = seeded.add_message(
        "webshop:older", "human", "human", "older activity", [])
    newer_message = seeded.add_message(
        "webshop:newer", "human", "human", "newer activity", [])
    seeded._execute("UPDATE messages SET created_at=100 WHERE id=?", (older_message,))
    seeded._execute("UPDATE messages SET created_at=200 WHERE id=?", (newer_message,))

    channels = seeded.list_channels("webshop")
    assert [channel.id for channel in channels[:3]] == [
        "general", "webshop:newer", "webshop:older"]
    assert next(channel for channel in channels
                if channel.id == "webshop:newer").last_message_at == 200
    assert "webshop:archived" not in {
        channel.id for channel in seeded.list_channels(
            "webshop", include_archived=False)}


def test_overview_reports_active_run_count_per_channel(seeded):
    seeded.put_channel(Channel(id="webshop:idle", name="idle", project_id="webshop"))
    trigger = seeded.add_message("general", "human", "human", "跑起来", [])
    running = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    seeded.update_chat_run(running, "running", backend_id="std-1")
    seeded.add_chat_run("general", "lead", trigger, trigger, 0)   # queued 也算在跑
    finished = seeded.add_chat_run("general", "expert", trigger, trigger, 0)
    seeded.update_chat_run(finished, "succeeded")

    channels = {channel["id"]: channel
                for channel in TestClient(create_app()).get("/api/overview")
                .json()["channels"]}

    assert channels["general"]["active_run_count"] == 2
    assert channels["webshop:idle"]["active_run_count"] == 0


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


def test_human_direct_worker_reply_waits_for_next_orchestrator_message(
        chat, seeded, monkeypatch):
    """人类直接点名执行角色时不拉起主控；主控稍后仍能读取完整记录。"""
    lead_prompts = []

    def _start(config):
        if config.role_id == "dev":
            return RunResult(True, "已完成检查", output="dev 的完整检查结果。")
        lead_prompts.append(config.prompt)
        return RunResult(True, "已接手", output="已读取先前记录并继续处理。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    chat.post("general", "human", "@[dev] 简单看一下就行，不用找别人。")
    chat.wait_idle()

    messages = _log(seeded)
    assert [m["author"] for m in messages
            if m["author_type"] == "agent"] == ["dev"]
    assert json.loads(messages[-1]["mentions"]) == []
    assert [run["role_id"] for run in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["dev"]

    chat.post("general", "human", "请主控接着处理。")
    chat.wait_idle()

    assert [m["author"] for m in _log(seeded)
            if m["author_type"] == "agent"] == ["dev", "lead"]
    assert len(lead_prompts) == 1
    assert "dev 的完整检查结果。" in lead_prompts[0]


def test_worker_failure_reason_is_delivered_to_orchestrator(
        chat, seeded, monkeypatch):
    """主控派发的执行角色失败时，主控收到具体 Runtime 原因。"""
    lead_prompts = []
    failure = ("OpenCode 未生成最终答复：最后执行阶段仍停在工具调用"
               "（step_finish.reason=tool-calls）；最后工具 read 失败："
               "The user rejected permission；权限信息：external_directory "
               "auto-rejecting")

    def _start(config):
        if config.role_id == "dev":
            return RunResult(False, failure)
        lead_prompts.append(config.prompt)
        return RunResult(True, "已处理失败", output="已看到 dev 的失败原因并调整安排。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    chat.post("general", "lead", "@dev\n\n检查外部文档。", author_type="agent",
              mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    chat.wait_idle()

    platform = [m for m in _log(seeded) if m["author_type"] == "platform"]
    assert len(platform) == 1
    assert failure in platform[0]["content"]
    assert len(lead_prompts) == 1
    assert failure in lead_prompts[0]
    assert any(
        m["author"] == "lead" and "已看到 dev 的失败原因" in m["content"]
        for m in _log(seeded) if m["author_type"] == "agent")


@pytest.mark.parametrize(
    ("runtime_success", "exit_kind"),
    [(True, "正常退出"), (False, "异常退出")],
)
def test_worker_exit_without_output_notifies_orchestrator(
        chat, seeded, monkeypatch, runtime_success, exit_kind):
    """主控派发的执行角色无输出时，平台补发原因并交回主控。"""
    lead_prompts = []
    worker_backends = []

    def _start(config):
        if config.role_id == "dev":
            worker_backends.append(config.backend.id)
            return RunResult(runtime_success, " \n", output="\t")
        lead_prompts.append(config.prompt)
        return RunResult(True, "已处理", output="已收到无输出告警并重新安排。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    chat.post("general", "lead", "@dev\n\n执行检查。", author_type="agent",
              mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    chat.wait_idle()

    platform = [m for m in _log(seeded) if m["author_type"] == "platform"]
    assert len(platform) == 1
    assert f"@dev(后端 {worker_backends[0]}){exit_kind}" in platform[0]["content"]
    assert "未产生任何可回传输出" in platform[0]["content"]
    assert len(lead_prompts) == 1
    assert platform[0]["content"] in lead_prompts[0]
    dev_run = seeded._query(
        "SELECT status, error FROM chat_runs WHERE role_id='dev' ORDER BY id DESC"
    )[0]
    assert dev_run["status"] == "failed"
    assert "未产生可回传输出" in dev_run["error"]


def test_orchestrator_exit_without_output_posts_platform_message(
        chat, seeded, monkeypatch):
    """主控自身无输出时补发平台消息，但不能再次触发自己形成循环。"""
    lead_backends = []

    def _start(config):
        lead_backends.append(config.backend.id)
        return RunResult(True, "", output="   ")

    monkeypatch.setattr(runtime_manager, "start", _start)

    chat.post("general", "human", "请主控检查项目。")
    chat.wait_idle()

    messages = _log(seeded)
    platform = [m for m in messages if m["author_type"] == "platform"]
    assert len(platform) == 1
    assert f"@lead(后端 {lead_backends[0]})正常退出" in platform[0]["content"]
    assert "未产生任何可回传输出" in platform[0]["content"]
    assert not [m for m in messages if m["author_type"] == "agent"]
    runs = seeded._query(
        "SELECT role_id, status FROM chat_runs ORDER BY id"
    )
    assert [(run["role_id"], run["status"]) for run in runs] == [
        ("lead", "failed")]


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
    root = chat.post("general", "lead", "@reviewer\n\n请复核金额计算。",
                     author_type="agent",
                     mention_spans=[{"role_id": "reviewer", "start": 0, "end": 9}])
    chat.wait_idle()
    msgs = _log(seeded)
    reviewer = next(m for m in msgs if m["author"] == "reviewer")
    lead_message = next(m for m in msgs if m["id"] == root)
    assert lead_message["content"].startswith("@reviewer")
    assert json.loads(lead_message["mention_spans"])[0]["role_id"] == "reviewer"
    assert reviewer["reply_to"] == root
    assert json.loads(reviewer["mentions"]) == ["lead"]
    assert [r["role_id"] for r in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["reviewer", "lead"]


def test_chat_pool_size_is_configurable(seeded, monkeypatch):
    """并发上限读 MISSIONCREW_CHAT_MAX_WORKERS;非法回落默认,显式传参优先。"""
    monkeypatch.setenv("MISSIONCREW_CHAT_MAX_WORKERS", "9")
    assert ChatEngine(seeded)._pool._max_workers == 9
    monkeypatch.setenv("MISSIONCREW_CHAT_MAX_WORKERS", "0")
    assert ChatEngine(seeded)._pool._max_workers == 1      # 下限钳制
    monkeypatch.setenv("MISSIONCREW_CHAT_MAX_WORKERS", "abc")
    assert ChatEngine(seeded)._pool._max_workers == 16     # 非法值回落默认
    monkeypatch.delenv("MISSIONCREW_CHAT_MAX_WORKERS")
    assert ChatEngine(seeded)._pool._max_workers == 16
    assert ChatEngine(seeded, max_workers=2)._pool._max_workers == 2


def test_mock_orchestrator_dispatches_via_agent_action(chat, seeded):
    """mock 主控经 cfg.agent_action 执行 message.publish 真实派发级联。"""
    chat.post("general", "human", "帮忙,请 @dev 检查购物车。")
    chat.wait_idle()
    runs = [r["role_id"] for r in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")]
    assert runs[0] == "lead" and "dev" in runs
    brief = next(m for m in _log(seeded)
                 if m["author"] == "lead" and m["content"].startswith("@dev"))
    assert json.loads(brief["mention_spans"]) == [
        {"role_id": "dev", "start": 0, "end": 4}]
    # dev 结果自动回传主控:链尾又有一次 lead 执行
    assert runs[-1] == "lead"


def test_mock_worker_dispatch_attempt_is_denied(chat, seeded):
    """非主控经 agent_action 尝试 message.publish 会被权限拒绝,不产生派发。"""
    chat.post("general", "human", "@[dev] 检查,做完请 @lead 汇报。")
    chat.wait_idle()
    dev_msg = next(m for m in _log(seeded) if m["author"] == "dev")
    assert "调度 @lead 未执行" in dev_msg["content"]
    runs = [r["role_id"] for r in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")]
    assert runs == ["dev"]     # 人类直派:无 auto-return,也没有越权派发


def test_agent_publish_rejected_when_origin_run_inactive(chat, seeded):
    """post 的停止互斥守卫:发起 run 已停止时,消息不落库、不调度。"""
    trigger = seeded.add_message("general", "human", "human", "go", ["lead"])
    run_id = seeded.add_chat_run("general", "lead", trigger, trigger, 0)
    seeded.update_chat_run(run_id, "running", backend_id="std-1")
    seeded.update_chat_run(run_id, "stopped")
    before = len(_log(seeded))
    with pytest.raises(DispatchInactiveError):
        chat.post("general", "lead", "@dev\n\n迟到的派发。", author_type="agent",
                  mention_spans=[{"role_id": "dev", "start": 0, "end": 4}],
                  origin_run_id=run_id)
    assert len(_log(seeded)) == before
    assert not any(r["role_id"] == "dev" for r in
                   seeded._query("SELECT role_id FROM chat_runs"))


def test_stale_bracket_syntax_gets_platform_hint(chat, seeded):
    """主控正文残留 @[已知角色] 且无结构化派发时,平台提示旧语法已失效。"""
    chat.post("general", "lead", "@[dev] 请实现支付。", author_type="agent")
    chat.wait_idle()
    hint = _log(seeded)[-1]
    assert hint["author_type"] == "platform"
    assert "旧派发" in hint["content"] and "@[dev]" in hint["content"]
    assert seeded._query("SELECT * FROM chat_runs") == []

    # 未知角色的 @[x] 不提示;带结构化派发的消息也不提示
    chat.post("general", "lead", "@[nobody] 只是文字。", author_type="agent")
    chat.wait_idle()
    assert _log(seeded)[-1]["author_type"] == "agent"


def test_worker_structured_spans_cannot_dispatch(chat, seeded):
    """结构化范围只对主控生效;执行角色带范围发消息也只会回传主控。"""
    root = seeded.add_message("general", "human", "human", "开始", [])
    chat.post("general", "dev", "@reviewer\n\n请复核。", author_type="agent",
              reply_to=root, root_id=root, depth=1,
              mention_spans=[{"role_id": "reviewer", "start": 0, "end": 9}])
    chat.wait_idle()
    assert not any(r["role_id"] == "reviewer" for r in
                   seeded._query("SELECT role_id FROM chat_runs"))
    dev_msg = next(m for m in _log(seeded) if m["author"] == "dev")
    assert json.loads(dev_msg["mentions"]) == ["lead"]


def test_multiple_human_mentions_start_only_orchestrator(
        chat, seeded, monkeypatch):
    """多人提及保留原名单，但只启动主控统一协调。"""
    lead_prompts = []

    def _start(config):
        assert config.role_id == "lead"
        lead_prompts.append(config.prompt)
        return RunResult(True, "已规划", output="已读取角色名单并规划协作。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    content = "@dev 和 @expert 分别评估一下方案 A/B。"
    expert_start = content.index("@expert")
    chat.post("general", "human", content, mention_spans=[
        {"role_id": "dev", "start": 0, "end": len("@dev")},
        {"role_id": "expert", "start": expert_start,
         "end": expert_start + len("@expert")},
    ])
    chat.wait_idle()

    messages = _log(seeded)
    trigger = messages[0]
    assert json.loads(trigger["mentions"]) == ["dev", "expert"]
    assert [span["role_id"] for span in
            json.loads(trigger["mention_spans"])] == ["dev", "expert"]
    assert [run["role_id"] for run in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["lead"]
    assert [message["author"] for message in messages
            if message["author_type"] == "agent"] == ["lead"]
    runtime_trigger = _prompt_json_section(
        lead_prompts[0], "触发消息(JSON,你的任务简报由发起者撰写)")
    assert runtime_trigger["mentions"] == ["dev", "expert"]
    assert [span["role_id"] for span in
            runtime_trigger["mention_spans"]] == ["dev", "expert"]
    assert "平台只启动你" in lead_prompts[0]


def test_expert_role_uses_fixed_expert_runtime(chat, seeded):
    chat.post("general", "human", "@[expert] 分析一下这个架构问题。")
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


def test_default_chain_run_budget_is_one_hundred(seeded):
    assert seeded.get_project("webshop").max_chain_runs == DEFAULT_MAX_CHAIN_RUNS == 100
    assert Project.from_dict({"id": "legacy", "name": "旧项目"}).max_chain_runs == 100


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
    # 人类直接点名的角色失败须公开，但不能自动触发主控。
    b = seeded.get_backend("vis-1")
    b.enabled = False
    seeded.put_backend(b)
    chat.post("general", "human", "@[vision] 验证一下首页截图。")
    chat.wait_idle()
    msgs = _log(seeded)
    assert any("无可用后端" in m["content"] for m in msgs
               if m["author_type"] == "platform")
    assert [r["role_id"] for r in seeded._query(
        "SELECT role_id FROM chat_runs ORDER BY id")] == ["vision"]


def test_bracket_mention_text_is_redacted_for_workers(chat, seeded):
    """字面 @[其他角色] 虽不再触发执行,但同样要对执行角色脱敏。"""
    msg_id = seeded.add_message(
        "general", "human", "human", "@dev 参考 @[expert] 的历史结论。", ["dev"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "dev"),
                         seeded.get_backend("std-1"), msg_id)
    worker_trigger = _prompt_json_section(
        cfg.prompt, "触发消息(JSON,你的任务简报由发起者撰写)")
    assert worker_trigger["content"] == "@dev 参考 [其他执行角色] 的历史结论。"


def test_prompt_lists_runtime_private_dirs(chat, seeded, tmp_path, monkeypatch):
    """工具自有目录(如 Codex 主目录)要进入授权清单,Agent 才敢用自带能力。"""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    msg_id = seeded.add_message(
        "general", "human", "human", "@dev 修一下登录", ["dev"])
    backend = Backend(id="codex", name="codex", adapter="codex")
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "dev"), backend, msg_id)
    assert str(codex_home.resolve()) in cfg.common_prompt


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


def test_clear_context_stops_sessions_and_resets_recent_history(
        chat, seeded, monkeypatch):
    old_message = seeded.add_message(
        "general", "human", "human", "旧上下文，不应再自动注入", [])
    seeded.put_chat_session(
        "general::lead", "general", "lead", "std-1", "mock", "/work",
        "old-native-session", "old-context")
    stopped = []
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)

    result = chat.clear_context("general")

    channel = seeded.get_channel("general")
    marker = seeded.get_message(result["marker_id"])
    assert channel.context_start_message_id == marker["id"]
    assert marker["kind"] == "context_boundary"
    assert seeded.chat_sessions_for_channel("general") == []
    assert ("std-1", "general::lead") in stopped
    assert old_message < marker["id"]

    seeded.add_message("general", "human", "human", "新上下文第一条", [])
    trigger = seeded.add_message(
        "general", "human", "human", "请只基于新上下文回答", ["lead"])
    cfg = chat._assemble(
        channel, seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"), trigger)
    history = _prompt_json_section(
        cfg.prompt, "最近对话(JSON,按消息边界格式化)")
    assert [item["content"] for item in history] == ["新上下文第一条"]
    assert not cfg.session_id
    assert cfg.session_key == f"general::lead::context-{marker['id']}"
    assert (cfg.project_id, cfg.role_id) == ("webshop", "lead")


def test_clear_context_api_rejects_active_run(seeded, monkeypatch):
    trigger = seeded.add_message("general", "human", "human", "仍在运行", [])
    seeded.add_chat_run("general", "lead", trigger, trigger, 0)
    monkeypatch.setattr(runtime_manager, "stop", lambda *_args, **_kwargs: 0)
    client = TestClient(create_app())

    response = client.post("/api/chat/general/clear-context")

    assert response.status_code == 409
    assert "正在运行" in response.json()["detail"]


def test_stop_channel_api_marks_every_run_and_terminates_runtime_processes(
        seeded, monkeypatch):
    trigger = seeded.add_message("general", "human", "human", "并行任务", [])
    dev_run = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    expert_run = seeded.add_chat_run("general", "expert", trigger, trigger, 0)
    queued_run = seeded.add_chat_run("general", "lead", trigger, trigger, 0)
    seeded.update_chat_run(dev_run, "running", backend_id="std-1")
    seeded.update_chat_run(expert_run, "running", backend_id="exp-1")
    assert seeded.wait_chat_run_for_interaction(expert_run, "exp-1")

    stopped = []
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)

    client = TestClient(create_app())
    response = client.post("/api/chat/general/stop")

    assert response.status_code == 200
    assert response.json()["stopped_runs"] == 3
    assert response.json()["interrupted_runtimes"] == 0
    assert response.json()["stopped_runtimes"] == 2
    assert sorted(stopped) == [
        ("exp-1", "general::expert"), ("std-1", "general::dev")]
    assert seeded.active_chat_runs("general") == []
    rows = seeded._query(
        "SELECT id,status,finished_at FROM chat_runs WHERE id IN (?,?,?) ORDER BY id",
        (dev_run, expert_run, queued_run))
    assert [row["status"] for row in rows] == ["stopped"] * 3
    assert all(row["finished_at"] for row in rows)
    assert all(any(event["kind"] == "status" and "用户已停止" in event["content"]
                   for event in seeded.run_events(run_id))
               for run_id in (dev_run, expert_run, queued_run))
    marker = seeded.list_messages("general")[-1]
    assert marker["kind"] == "agent_stop" and "3 个 Agent" in marker["content"]
    assert "终止 2 个 Runtime 进程" in marker["content"]
    assert client.post("/api/chat/general/stop").json()["stopped_runs"] == 0
    assert client.post("/api/chat/missing/stop").status_code == 404


def test_stop_channel_prevents_late_reply_and_queued_agent_start(
        seeded, monkeypatch):
    chat = ChatEngine(seeded, max_workers=1)
    started = threading.Event()
    release = threading.Event()
    starts = []

    def blocking_start(config):
        starts.append(config.role_id)
        started.set()
        assert release.wait(5)
        return RunResult(True, "late result", output="这个回复不应发布")

    stopped = []
    monkeypatch.setattr(runtime_manager, "start", blocking_start)
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)

    chat.post("general", "lead", "@dev @expert\n\n同时执行", author_type="agent",
              mention_spans=[{"role_id": "dev", "start": 0, "end": 4},
                             {"role_id": "expert", "start": 5, "end": 12}])
    assert started.wait(5)
    result = chat.stop_channel_agents("general")
    release.set()
    chat.wait_idle()

    assert result["stopped_runs"] == 2
    assert result["stopped_runtimes"] == 1
    assert starts == ["dev"]  # 第二个 future 尚在队列，停止后不能再启动 Runtime
    assert stopped == [(seeded.get_role("webshop", "dev").runtime_id,
                        "general::dev")]
    assert {row["status"] for row in seeded.chat_runs_for_channel("general")} \
        == {"stopped"}
    messages = seeded.list_messages("general")
    assert [message["author"] for message in messages
            if message["author_type"] == "agent"] == ["lead"]
    assert messages[-1]["kind"] == "agent_stop"


def test_stop_channel_terminates_native_instance_when_turn_is_not_registered(
        seeded, monkeypatch):
    trigger = seeded.add_message("general", "human", "human", "启动中", [])
    run_id = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    backend_id = seeded.get_role("webshop", "dev").runtime_id
    seeded.update_chat_run(run_id, "running", backend_id=backend_id)
    chat = ChatEngine(seeded)
    stopped = []
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)

    result = chat.stop_channel_agents("general")

    assert result["interrupted_runtimes"] == 0
    assert result["stopped_runtimes"] == 1
    assert stopped == [(backend_id, "general::dev")]
    assert not seeded.chat_run_is_active(run_id)


def test_stop_channel_terminates_orphan_runtime_without_active_run(
        seeded, monkeypatch):
    backend_id = seeded.get_role("webshop", "dev").runtime_id
    seeded.put_chat_session(
        "general::dev", "general", "dev", backend_id, "mock", "/tmp",
        "native-orphan", "v1")
    chat = ChatEngine(seeded)
    stopped = []
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)

    result = chat.stop_channel_agents("general")

    assert result["stopped_runs"] == 0
    assert result["interrupted_runtimes"] == 0
    assert result["stopped_runtimes"] == 1
    assert stopped == [(backend_id, "general::dev")]
    marker = seeded.list_messages("general")[-1]
    assert marker["kind"] == "agent_stop"
    assert "没有活动 Agent" in marker["content"]
    assert "终止 1 个 Runtime 进程" in marker["content"]


def test_stop_single_run_only_affects_target_role(seeded, monkeypatch):
    trigger = seeded.add_message("general", "human", "human", "并行任务", [])
    dev_run = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    expert_run = seeded.add_chat_run("general", "expert", trigger, trigger, 0)
    seeded.update_chat_run(dev_run, "running", backend_id="std-1")
    seeded.update_chat_run(expert_run, "running", backend_id="exp-1")

    stopped = []
    monkeypatch.setattr(
        runtime_manager, "stop",
        lambda backend, session_key="": stopped.append(
            (backend.id, session_key)) or 1)

    client = TestClient(create_app())
    response = client.post(f"/api/chat/runs/{dev_run}/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["stopped_runs"] == 1 and body["role_id"] == "dev"
    assert body["stopped_runtimes"] == 1
    assert stopped == [("std-1", "general::dev")]
    # 只停目标 run:同频道其他角色的运行不受影响
    assert [run["id"] for run in seeded.active_chat_runs("general")] \
        == [expert_run]
    row = seeded.get_chat_run(dev_run)
    assert row["status"] == "stopped" and row["finished_at"]
    assert any(event["kind"] == "status" and "用户已停止本次" in event["content"]
               for event in seeded.run_events(dev_run))
    marker = seeded.list_messages("general")[-1]
    assert marker["kind"] == "agent_stop" and "@dev" in marker["content"]
    # 重复停止与不存在的 run 都返回冲突,不产生新的停止动作
    assert client.post(f"/api/chat/runs/{dev_run}/stop").status_code == 409
    assert client.post("/api/chat/runs/999999/stop").status_code == 409
    assert stopped == [("std-1", "general::dev")]


def test_run_records_execution_combo_on_start(seeded, monkeypatch):
    """转入 running 时盖章 model/effort,运行卡片按执行当时组合展示。"""
    role = seeded.get_role("webshop", "dev")
    role.effort = "high"
    seeded.put_role(role)
    monkeypatch.setattr(
        runtime_manager, "start",
        lambda config: RunResult(True, "", output="完成"))
    chat = ChatEngine(seeded, max_workers=1)
    chat.post("general", "human", "@dev 干活",
              mention_spans=[{"role_id": "dev", "start": 0, "end": 4}])
    chat.wait_idle()

    run = seeded.chat_runs_for_channel("general")[-1]
    assert run["role_id"] == "dev" and run["status"] == "done"
    assert run["model"] == seeded.get_role("webshop", "dev").model
    assert run["effort"] == "high"


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


def test_lean_turns_accumulate_and_counter_forces_reinjection(
        chat, seeded, monkeypatch):
    """复用会话版本未变走增量回合;计数达到阈值后强制完整重注入并清零。"""
    channel = seeded.get_channel("general")
    role = seeded.get_role("webshop", "dev")
    backend = seeded.get_backend("std-1")

    first_message = seeded.add_message(
        "general", "human", "human", "@dev 第一轮", ["dev"])
    assert adapters.get_adapter("mock").run(
        chat._assemble(channel, role, backend, first_message)).success
    row = seeded.get_chat_session("general::dev")
    assert (row["lean_turns"], row["lean_bytes"]) == (0, 0)

    second_message = seeded.add_message(
        "general", "human", "human", "@dev 第二轮", ["dev"])
    second = chat._assemble(channel, role, backend, second_message)
    events = []
    second.emit = lambda kind, text: events.append((kind, text))
    assert not second.reinject_due
    assert adapters.get_adapter("mock").run(second).success
    sent = dict(events)["input"]
    assert "# MissionCrew 增量回合" in sent
    assert second.context_version in sent
    assert "# MissionCrew 持久公共上下文" not in sent
    row = seeded.get_chat_session("general::dev")
    assert row["lean_turns"] == 1 and row["lean_bytes"] > 0
    assert any(kind == "status" and "增量回合" in text
               for kind, text in events)

    monkeypatch.setenv("MISSIONCREW_CONTEXT_REINJECT_TURNS", "1")
    third_message = seeded.add_message(
        "general", "human", "human", "@dev 第三轮", ["dev"])
    third = chat._assemble(channel, role, backend, third_message)
    events = []
    third.emit = lambda kind, text: events.append((kind, text))
    assert third.reinject_due
    assert adapters.get_adapter("mock").run(third).success
    sent = dict(events)["input"]
    assert "# MissionCrew 公共上下文重注入" in sent
    assert "# MissionCrew 持久公共上下文" in sent
    row = seeded.get_chat_session("general::dev")
    assert (row["needs_reinject"], row["lean_turns"], row["lean_bytes"]) \
        == (0, 0, 0)


def test_compact_mark_forces_reinjection_and_survives_full_turn(chat, seeded):
    """压缩标记触发下一轮完整注入;完整轮中再次压缩时标记保留。"""
    channel = seeded.get_channel("general")
    role = seeded.get_role("webshop", "dev")
    backend = seeded.get_backend("std-1")

    first_message = seeded.add_message(
        "general", "human", "human", "@dev 建会话", ["dev"])
    first = chat._assemble(channel, role, backend, first_message)
    assert adapters.get_adapter("mock").run(first).success

    # 模拟 Runtime 在上一轮报告 compact
    seeded.mark_chat_session_reinject("general::dev")
    second_message = seeded.add_message(
        "general", "human", "human", "@dev 压缩后继续", ["dev"])
    second = chat._assemble(channel, role, backend, second_message)
    events = []
    second.emit = lambda kind, text: events.append((kind, text))
    assert second.reinject_due
    # 完整重注入轮中 Runtime 又检测到压缩:标记不能被轮末保存清掉
    second.compact_detected = True
    assert adapters.get_adapter("mock").run(second).success
    assert "# MissionCrew 公共上下文重注入" in dict(events)["input"]
    assert seeded.get_chat_session("general::dev")["needs_reinject"] == 1

    third_message = seeded.add_message(
        "general", "human", "human", "@dev 再来一轮", ["dev"])
    third = chat._assemble(channel, role, backend, third_message)
    events = []
    third.emit = lambda kind, text: events.append((kind, text))
    assert third.reinject_due
    assert adapters.get_adapter("mock").run(third).success
    assert seeded.get_chat_session("general::dev")["needs_reinject"] == 0


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
    assert worker_path.parent.name == ".missioncrew"
    assert worker_path.parent.parent.name == "dev"
    assert worker_path.is_relative_to(Path(worker_cfg.env["MISSIONCREW_WORKSPACE"]))
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
    assert lead_path.parent.name == ".missioncrew"
    assert lead_path.parent.parent.name == "lead"
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


def test_human_direct_dispatch_result_is_visible_without_orchestrator(chat, seeded):
    """人类直接调度的执行结果保存在频道中，但不会自动拉起主控。"""
    chat.post("general", "human", "@[dev] 处理,完成后请 @reviewer 复核。")
    chat.wait_idle()
    msgs = seeded.list_messages("general")
    dev_msg = next(m for m in msgs if m["author"] == "dev")
    assert dev_msg["content"]
    assert json.loads(dev_msg["mentions"]) == []
    assert not any(m["author"] == "lead" for m in msgs
                   if m["author_type"] == "agent")


def test_message_paging_tail_and_before_id(seeded):
    """长频道首屏 tail 直接取最新一页；before_id 向上翻页；has_earlier 指示更早历史。"""
    ids = [seeded.add_message("general", "human", "human", f"历史消息 {i}", [])
           for i in range(205)]
    client = TestClient(create_app())

    # 增量模式行为不变，且不携带分页标记
    incremental = client.get("/api/chat/general/messages").json()
    assert "has_earlier" not in incremental
    assert [m["id"] for m in incremental["messages"]] == ids[:200]

    tail = client.get("/api/chat/general/messages", params={"tail": True}).json()
    assert [m["id"] for m in tail["messages"]] == ids[-200:]
    assert tail["has_earlier"] is True

    earlier = client.get("/api/chat/general/messages",
                         params={"before_id": tail["messages"][0]["id"]}).json()
    assert [m["id"] for m in earlier["messages"]] == ids[:5]
    assert earlier["has_earlier"] is False

    # 空频道 tail：无消息也不误报更早历史
    empty = client.get("/api/chat/general/messages",
                       params={"before_id": ids[0]}).json()
    assert empty["messages"] == []
    assert empty["has_earlier"] is False


def test_runtime_wake_turn_lands_as_channel_run(chat, seeded):
    """后台命令结束后的自唤醒汇报:落成新运行、回放事件、按常规回路交回主控。"""
    seeded.put_chat_session("general::dev", "general", "dev", "mock-claude",
                            "claude_code", "/tmp", "native-1", "v1")
    chat._process_runtime_wake({
        "runtime": "claude",
        "session_key": "general::dev",
        "backend_id": "mock-claude",
        "success": True,
        "output": "后台部署已完成：全部服务健康，exit 0。",
        "events": [("status", "Claude 会话已连接\n"),
                   ("text", "后台部署已完成：全部服务健康，exit 0。")],
        "tasks": [{"task_id": "bg1", "description": "deploy.sh",
                   "status": "completed"}],
    })
    chat.wait_idle()

    msgs = seeded.list_messages("general")
    trigger = next(m for m in msgs if m["author_type"] == "platform"
                   and "后台命令已结束" in m["content"])
    assert "`deploy.sh`" in trigger["content"]
    reply = next(m for m in msgs if m["author"] == "dev"
                 and m["author_type"] == "agent")
    assert "后台部署已完成" in reply["content"]
    assert reply["reply_to"] == trigger["id"]

    runs = [r for r in seeded.chat_runs_for_channel("general")
            if r["role_id"] == "dev"]
    assert runs and runs[-1]["status"] == "done"
    assert runs[-1]["trigger_message_id"] == trigger["id"]
    events = seeded.run_events(runs[-1]["id"])
    assert any(e["kind"] == "status" for e in events)


def test_runtime_wake_ignored_for_archived_or_unknown_session(chat, seeded):
    """会话不存在或频道已归档时,自唤醒静默丢弃,不产生消息。"""
    chat._process_runtime_wake({
        "session_key": "nonexistent::role", "output": "孤儿汇报"})
    assert seeded.list_messages("general") == []

    seeded.put_channel(Channel(id="deploy", name="deploy",
                               project_id="webshop", archived=True))
    seeded.put_chat_session("deploy::dev", "deploy", "dev", "b1",
                            "claude_code", "/tmp", "n1", "v1")
    chat._process_runtime_wake({
        "session_key": "deploy::dev", "output": "归档后的汇报"})
    chat.wait_idle()
    assert seeded.list_messages("deploy") == []


def test_runtime_wake_respects_direct_human_dispatch(chat, seeded):
    """人类直接点名角色启动的后台任务:唤醒汇报挂回原人类消息,不交回主控。"""
    human_msg = seeded.add_message("general", "human", "human",
                                   "@dev 部署到测试机", ["dev"])
    seeded.put_chat_session("general::dev", "general", "dev", "b1",
                            "claude_code", "/tmp", "n1", "v1")
    chat._process_runtime_wake({
        "session_key": "general::dev", "backend_id": "b1",
        "success": True, "output": "部署完成:服务健康,exit 0。",
        "events": [("text", "部署完成:服务健康,exit 0。")],
        "tasks": [{"task_id": "bg1", "description": "deploy.sh",
                   "status": "completed", "origin_trigger": human_msg}],
    })
    chat.wait_idle()

    msgs = seeded.list_messages("general")
    reply = next(m for m in msgs if m["author"] == "dev"
                 and m["author_type"] == "agent")
    assert reply["reply_to"] == human_msg
    assert reply["root_id"] == human_msg
    # 与直接点名的常规回复一致:主控不被唤醒
    assert not any(m["author"] == "lead" and m["author_type"] == "agent"
                   for m in msgs)
    assert [r["role_id"] for r in seeded.chat_runs_for_channel("general")] == ["dev"]


def test_atomic_write_json_survives_concurrent_writers(tmp_path):
    """多个写者同时更新同一历史文件:临时文件名唯一,replace 不会互相抢走。"""
    path = tmp_path / "channel-history.json"
    errors: list[Exception] = []

    def writer(tag: int) -> None:
        try:
            for i in range(200):
                ChatEngine._atomic_write_json(path, {"writer": tag, "i": i})
        except Exception as e:  # noqa: BLE001 - 断言用
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert json.loads(path.read_text(encoding="utf-8"))["i"] == 199
    assert not list(tmp_path.glob(".*.tmp"))   # 不留临时文件


def test_engine_shutdown_waits_for_runs_and_rejects_new_ones(seeded, monkeypatch):
    """shutdown() 先等已派发运行结束;关闭后再派发不能停留在 queued。"""
    def _slow_start(config):
        time.sleep(0.2)
        return RunResult(True, "ok", output="分析完毕")

    monkeypatch.setattr(runtime_manager, "start", _slow_start)
    chat = ChatEngine(seeded, max_workers=2)
    chat.post("general", "human", "@[dev] 看一下购物车。")
    assert seeded.active_chat_runs("general")

    chat.shutdown()
    assert seeded.active_chat_runs("general") == []

    chat.post("general", "human", "@[dev] 再看一下。")
    runs = seeded._query("SELECT status, error FROM chat_runs ORDER BY id")
    assert [run["status"] for run in runs] == ["done", "failed"]
    assert "聊天引擎已关闭" in runs[-1]["error"]


def test_app_shutdown_waits_for_dispatched_runs(seeded, monkeypatch):
    """TestClient/服务退出时必须等聊天线程池收尾,运行不能活过应用生命周期。"""
    def _slow_start(config):
        time.sleep(0.3)
        return RunResult(True, "ok", output="检查完毕")

    monkeypatch.setattr(runtime_manager, "start", _slow_start)
    with TestClient(create_app()) as client:
        response = client.post("/api/chat/general/messages", json={
            "author": "human", "content": "@dev 检查",
            "mentions": [{"role_id": "dev", "start": 0, "end": 4}],
        })
        assert response.status_code == 200, response.text
        assert seeded.active_chat_runs("general")
    assert seeded.active_chat_runs("general") == []
