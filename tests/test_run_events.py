"""运行过程事件:适配器实时上报 -> 落库 -> 聊天接口内联展示。"""
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import Backend, ExecutionConfig
from missioncrew.runtime import adapters

FAKE_STREAM = str(Path(__file__).parent / "fake_stream_cli.py")
FAKE_ACP = str(Path(__file__).parent / "fake_acp_agent.py")


def _collect():
    events = []
    return events, lambda kind, text: events.append((kind, text))


def _cfg(tmp_path, backend, emit):
    return ExecutionConfig(task_id="t", stage_name="chat", backend=backend,
                           prompt="# 聊天协作请求\n测试", workdir=str(tmp_path),
                           timeout=30, emit=emit)


# ---- 存储:同类事件合并追加,换类/超限另起新行 ----

def test_run_event_coalescing(store):
    store.append_run_event(7, "text", "第一段")
    store.append_run_event(7, "text", ",第二段")
    store.append_run_event(7, "tool", "Bash echo\n")
    store.append_run_event(7, "text", "新文本块")
    events = store.run_events(7)
    assert [(e["kind"], e["content"]) for e in events] == [
        ("text", "第一段,第二段"), ("tool", "Bash echo\n"), ("text", "新文本块")]
    # 超过单行上限后另起新行,不无限膨胀
    store.append_run_event(8, "stdout", "x" * store.RUN_EVENT_MAX)
    store.append_run_event(8, "stdout", "y")
    assert len(store.run_events(8)) == 2
    # 长输入完整分段保存，不能因单行上限只留下前 8000 字符。
    long_input = "输" * (store.RUN_EVENT_MAX + 17)
    store.append_run_event(9, "input", long_input)
    saved = store.run_events(9)
    assert len(saved) == 2
    assert "".join(e["content"] for e in saved) == long_input


def test_duplicate_reply_output_is_removed_exactly(store):
    store.append_run_event(10, "thinking", "先思考\n")
    store.append_run_event(10, "text", "最终回复")
    store.append_run_event(10, "text", "正文\n")
    assert store.remove_duplicate_reply_output(10, "最终回复正文") == 1
    assert [(e["kind"], e["content"]) for e in store.run_events(10)] == [
        ("thinking", "先思考\n")]

    store.append_run_event(11, "stdout", "过程输出\n最终回复\n")
    assert store.remove_duplicate_reply_output(11, "最终回复") == 0
    assert store.run_events(11)[0]["kind"] == "stdout"


def test_legacy_truncated_reply_and_execution_metadata_are_migrated(store):
    trigger = store.add_message("general", "human", "human", "开始核查", [])
    run_id = store.add_chat_run("general", "reviewer", trigger, trigger, 0)
    store.append_run_event(
        run_id, "input",
        "# 聊天协作请求\n固定执行组合:claude/claude-opus-4-8/effort=high\n")
    full_reply = "完整开头：代码流程\n" + "证据正文" * 1200
    store.append_run_event(run_id, "text", full_reply)
    message_id = store.add_message(
        "general", "reviewer", "agent", full_reply[-4000:], [],
        reply_to=trigger, root_id=trigger, depth=1)

    assert store._migrate_chat_messages() == 1
    restored = store.get_message(message_id)
    assert restored["content"] == full_reply
    assert (restored["runtime_id"], restored["model"], restored["effort"]) == (
        "claude", "claude-opus-4-8", "high")
    assert store._migrate_chat_messages() == 0       # 幂等，不重复修改


def test_existing_message_table_gets_execution_metadata_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL, author TEXT NOT NULL, author_type TEXT NOT NULL,
        content TEXT NOT NULL, mentions TEXT DEFAULT '[]',
        reply_to INTEGER, root_id INTEGER, depth INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    )""")
    con.commit()
    con.close()

    from missioncrew.core.store import Store
    legacy = Store(path)
    columns = {row["name"] for row in legacy._query("PRAGMA table_info(messages)")}
    assert {"runtime_id", "model", "effort", "kind"} <= columns


# ---- CLI 适配器:stream-json 解析出思考/工具/文本,普通 CLI 按行透传 ----

def test_cli_adapter_parses_stream_json_events(tmp_path):
    events, emit = _collect()
    backend = Backend(id="c", name="c", adapter="claude_code",
                      command=[sys.executable, FAKE_STREAM, "stream-json"])
    cfg = _cfg(tmp_path, backend, emit)
    cfg.env["MISSIONCREW_WORKSPACE"] = str(tmp_path / "harness" / ".missioncrew")
    result = adapters.CliAdapter("claude_code").run(cfg)
    assert result.success
    assert result.output == "最终回复:OK"          # 回复取 result 事件,不是原始 JSONL
    kinds = [k for k, _ in events]
    assert {"status", "thinking", "tool", "tool_result", "text"} <= set(kinds)
    assert ("thinking", "先复述要求,再执行。") in events
    assert any(k == "tool" and "Bash" in t for k, t in events)
    # thinking_tokens 等 system 子事件不进过程流
    assert not any("thinking_tokens" in t for _, t in events)
    assert (tmp_path / "harness" / ".missioncrew" / "runtime"
            / "last-output-claude_code.log").is_file()
    assert not (tmp_path / ".mc_last_output_claude_code.log").exists()
    assert not (tmp_path / ".missioncrew").exists()


def test_cli_adapter_keeps_full_long_stream_reply(tmp_path):
    events, emit = _collect()
    backend = Backend(id="c", name="c", adapter="claude_code",
                      command=[sys.executable, FAKE_STREAM, "long", "stream-json"])
    result = adapters.CliAdapter("claude_code").run(_cfg(tmp_path, backend, emit))
    assert len(result.output) > 4000
    assert result.output.startswith("完整开头：不能丢失")


def test_plain_cli_adapter_keeps_more_than_tail_lines(tmp_path):
    events, emit = _collect()
    backend = Backend(id="p", name="p", adapter="pi",
                      command=[sys.executable, FAKE_STREAM, "plain-long"])
    result = adapters.CliAdapter("pi").run(_cfg(tmp_path, backend, emit))
    assert result.output.startswith("完整回复第000行")
    assert result.output.endswith("完整回复第499行")


def test_cli_adapter_streams_plain_lines(tmp_path):
    events, emit = _collect()
    backend = Backend(id="p", name="p", adapter="pi",
                      command=[sys.executable, FAKE_STREAM, "plain", "{prompt}"])
    result = adapters.CliAdapter("pi").run(_cfg(tmp_path, backend, emit))
    assert result.success and "最终回复" in result.output
    command = next(t for k, t in events if k == "command")
    assert "<输入>" in command and "# 聊天协作请求" not in command
    assert ("input", "# 聊天协作请求\n测试") in events
    assert ("stdout", "第一行进度\n") in events
    assert ("stderr", "警告:示例 stderr\n") in events


def test_cli_adapter_survives_raising_emit(tmp_path):
    """emit 落库失败只丢事件:读线程不能死,否则管道写满整次执行假死。"""
    def bad_emit(kind, text):
        raise RuntimeError("db unavailable")
    backend = Backend(id="p", name="p", adapter="pi",
                      command=[sys.executable, FAKE_STREAM, "plain"])
    cfg = _cfg(tmp_path, backend, bad_emit)
    cfg.timeout = 15
    result = adapters.CliAdapter("pi").run(cfg)
    assert result.success and "最终回复" in result.output


def test_stream_json_without_result_falls_back_to_text_blocks(tmp_path):
    """异常中断没等到 result 事件:回复退回已解析文本块,不发原始 JSONL。"""
    events, emit = _collect()
    backend = Backend(id="c", name="c", adapter="claude_code",
                      command=[sys.executable, FAKE_STREAM, "noresult", "stream-json"])
    result = adapters.CliAdapter("claude_code").run(_cfg(tmp_path, backend, emit))
    assert result.output == "中断前的部分回复"
    assert '"type"' not in result.output          # 不把 JSONL 泄给频道


def test_cli_adapter_reaps_pipe_holding_grandchildren(tmp_path):
    """子进程退出但孙进程握着管道:按进程组清理,不悬挂、回复不被污染。"""
    import time
    events, emit = _collect()
    backend = Backend(id="p", name="p", adapter="pi",
                      command=[sys.executable, FAKE_STREAM, "grandchild"])
    cfg = _cfg(tmp_path, backend, emit)
    cfg.timeout = 30
    t0 = time.time()
    result = adapters.CliAdapter("pi").run(cfg)
    assert time.time() - t0 < 15          # join 超时后组清理,不等满 timeout
    assert result.success and "REAL-ANSWER" in result.output
    n_before = len(events)
    time.sleep(1.5)                       # run 返回后事件不再增长(读线程已结束)
    assert len(events) == n_before


def test_codex_stderr_parsed_into_sections(tmp_path):
    """codex 的 stderr 过程日志分节归类:头部/思考/命令,提示词回显去重,
    回复回显跳过(stdout 已有),tokens used 并入状态。"""
    events, emit = _collect()
    backend = Backend(id="cx", name="cx", adapter="codex",
                      command=[sys.executable, FAKE_STREAM, "codex"])
    result = adapters.CliAdapter("codex").run(_cfg(tmp_path, backend, emit))
    assert result.success and result.output == "最终回复正文"
    joined = {k: "".join(t for kk, t in events if kk == k)
              for k in ("input", "status", "thinking", "tool", "tool_result", "stdout")}
    assert "model: gpt-test" in joined["status"]           # 配置头部 -> 状态
    assert joined["input"] == "# 聊天协作请求\n测试"         # 完整输入单独展示
    assert "任务简报" not in str(events)                    # 不用字符数简报替代输入
    assert "很长的提示词回显" not in str(events)             # stderr 回显不重复展示
    assert "tokens used: 12,008" in joined["status"]
    assert "先理解需求再回答" in joined["thinking"]
    assert "exec bash -lc 'echo hi'" in joined["tool"]
    assert "hi" in joined["tool_result"]
    assert "最终回复正文" not in joined["status"]            # codex 节回显被跳过
    assert not any(k == "stderr" for k, _ in events)        # 全部行都被归了类


# ---- ACP 适配器:思考/工具通知与文本块实时上报 ----

def test_acp_adapter_emits_process_events(tmp_path):
    events, emit = _collect()
    backend = Backend(id="kimi", name="k", adapter="kimi",
                      command=[sys.executable, FAKE_ACP])
    result = adapters.get_adapter("kimi").run(_cfg(tmp_path, backend, emit))
    assert result.success
    kinds = [k for k, _ in events]
    assert {"command", "input", "thinking", "tool", "text"} <= set(kinds)
    assert FAKE_ACP in next(t for k, t in events if k == "command")
    assert ("input", "# 聊天协作请求\n测试") in events
    assert ("thinking", "思考中…") in events
    assert any(k == "tool" and "read_file" in t for k, t in events)
    assert any(k == "status" and "权限请求" in t for k, t in events)


def test_acp_adapter_keeps_full_long_reply(tmp_path, monkeypatch):
    long_reply = "ACP 完整开头\n" + "长回复" * 1600
    monkeypatch.setattr(adapters.acp, "run_prompt",
                        lambda *args, **kwargs: (True, long_reply))
    backend = Backend(id="kimi", name="k", adapter="kimi",
                      command=[sys.executable, FAKE_ACP])
    result = adapters.AcpAdapter("kimi").run(_cfg(tmp_path, backend, None))
    assert result.output == long_reply


# ---- 端到端:聊天执行事件落库,经 API 内联提供给前端 ----

def test_chat_run_events_flow_to_api(seeded):
    dev = seeded.get_role("webshop", "dev")
    dev.effort = "high"
    seeded.put_role(dev)
    chat = ChatEngine(seeded, max_workers=2)
    chat.post("general", "human", "@dev 看一下这个问题")
    chat.wait_idle()
    client = TestClient(create_app())
    d = client.get("/api/chat/general/messages").json()
    assert d["runs"], "消息接口应返回频道执行记录"
    run = next(r for r in d["runs"] if r["role_id"] == "dev")
    assert run["role_id"] == "dev" and run["status"] == "done"
    assert run["events_size"] > 0
    assert run["trigger_message_id"] == d["messages"][0]["id"]
    events = client.get(f"/api/chat/runs/{run['id']}/events").json()["events"]
    kinds = {e["kind"] for e in events}
    assert {"thinking", "tool"} <= kinds
    assert "text" not in kinds       # 与最终 Agent 回复完全一致，不在过程流重复展示
    dev_message = next(m for m in d["messages"] if m["author"] == "dev")
    assert (dev_message["runtime_id"], dev_message["model"], dev_message["effort"]) == (
        dev.runtime_id, dev.model, "high")
    agent_reply = dev_message["content"]
    assert agent_reply and all(e["content"].strip() != agent_reply for e in events)


def test_chat_ui_shows_execution_combo_and_folds_long_replies(seeded):
    client = TestClient(create_app())
    js = client.get("/assets/js/sidebar.js").text
    css = client.get("/assets/css/app.css").text
    assert "agentExecutionLabel" in js
    assert "runtime=${message.runtime_id" in js
    assert "model=${message.model" in js
    assert "effort=${message.effort" in js
    assert "MESSAGE_FOLD_AT" in js and "toggleMessageBody" in js
    assert "renderRunEvent" in js and "latestEventId" in js
    assert 'body.querySelectorAll(".re-fold[open]")' in js
    assert (".re-fold { border:" in css
            and "padding: 0 8px 8px;\n             white-space: normal;" in css)
    assert "RUN_INPUT_FOLD_AT" not in js
    assert "/clear-context" in js and 'm.kind === "context_boundary"' in js
    assert "清除上下文" in client.get("/").text
    assert '<span class="via">agent</span>' not in js
