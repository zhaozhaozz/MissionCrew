"""运行过程事件:适配器实时上报 -> 落库 -> 聊天接口内联展示。"""
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


# ---- CLI 适配器:stream-json 解析出思考/工具/文本,普通 CLI 按行透传 ----

def test_cli_adapter_parses_stream_json_events(tmp_path):
    events, emit = _collect()
    backend = Backend(id="c", name="c", adapter="claude_code",
                      command=[sys.executable, FAKE_STREAM, "stream-json"])
    result = adapters.CliAdapter("claude_code").run(_cfg(tmp_path, backend, emit))
    assert result.success
    assert result.output == "最终回复:OK"          # 回复取 result 事件,不是原始 JSONL
    kinds = [k for k, _ in events]
    assert {"status", "thinking", "tool", "tool_result", "text"} <= set(kinds)
    assert ("thinking", "先复述要求,再执行。") in events
    assert any(k == "tool" and "Bash" in t for k, t in events)
    # thinking_tokens 等 system 子事件不进过程流
    assert not any("thinking_tokens" in t for _, t in events)


def test_cli_adapter_streams_plain_lines(tmp_path):
    events, emit = _collect()
    backend = Backend(id="p", name="p", adapter="pi",
                      command=[sys.executable, FAKE_STREAM, "plain"])
    result = adapters.CliAdapter("pi").run(_cfg(tmp_path, backend, emit))
    assert result.success and "最终回复" in result.output
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


# ---- ACP 适配器:思考/工具通知与文本块实时上报 ----

def test_acp_adapter_emits_process_events(tmp_path):
    events, emit = _collect()
    backend = Backend(id="kimi", name="k", adapter="kimi",
                      command=[sys.executable, FAKE_ACP])
    result = adapters.get_adapter("kimi").run(_cfg(tmp_path, backend, emit))
    assert result.success
    kinds = [k for k, _ in events]
    assert "thinking" in kinds and "tool" in kinds and "text" in kinds
    assert ("thinking", "思考中…") in events
    assert any(k == "tool" and "read_file" in t for k, t in events)
    assert any(k == "status" and "权限请求" in t for k, t in events)


# ---- 端到端:聊天执行事件落库,经 API 内联提供给前端 ----

def test_chat_run_events_flow_to_api(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    chat.post("general", "human", "@dev 看一下这个问题")
    chat.wait_idle()
    client = TestClient(create_app())
    d = client.get("/api/chat/general/messages").json()
    assert d["runs"], "消息接口应返回频道执行记录"
    run = d["runs"][-1]
    assert run["role_id"] == "dev" and run["status"] == "done"
    assert run["events_size"] > 0
    assert run["trigger_message_id"] == d["messages"][0]["id"]
    events = client.get(f"/api/chat/runs/{run['id']}/events").json()["events"]
    kinds = {e["kind"] for e in events}
    assert {"thinking", "tool", "text"} <= kinds     # mock 全链路产生过程事件
