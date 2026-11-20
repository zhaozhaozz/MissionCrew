"""运行过程事件写缓冲:流式事件不在调用线程落库,读与状态迁移前排空。"""
import json
import sqlite3
import threading
import time

from missioncrew.runtime import adapters


def _run(store, status="running"):
    trigger = store.add_message("general", "human", "human", "go", [])
    run_id = store.add_chat_run("general", "dev", trigger, trigger, 0)
    if status != "queued":
        store.update_chat_run(run_id, status, backend_id="b")
    return run_id


def _persisted(store, run_id):
    """绕过 Store 用独立连接读库,只看真正提交了的内容。"""
    con = sqlite3.connect(store.path)
    try:
        return [(kind, content) for kind, content in con.execute(
            "SELECT kind, content FROM run_events WHERE run_id=? ORDER BY id", (run_id,))]
    finally:
        con.close()


def test_stream_events_stay_in_memory_until_read_and_merge_per_run(store):
    run_id = _run(store)
    other = _run(store)
    store.stream_run_event(run_id, "text", "answer ")
    store.stream_run_event(other, "text", "elsewhere")   # 别的运行插进来不打断合并
    store.stream_run_event(run_id, "text", "complete")
    store.stream_run_event(run_id, "usage", json.dumps({"n": 1}))
    store.stream_run_event(run_id, "usage", json.dumps({"n": 2}))
    assert _persisted(store, run_id) == []               # 调用线程没有写库
    assert [(e["kind"], e["content"]) for e in store.run_events(run_id)] == [
        ("text", "answer complete"),
        ("usage", '{"n": 1}'), ("usage", '{"n": 2}'),     # 结构化事件各占一行
    ]
    assert store.run_live_output(other) == "elsewhere"


def test_background_flush_persists_without_any_reader(store):
    run_id = _run(store)
    store.stream_run_event(run_id, "text", "streamed")
    deadline = time.monotonic() + 3
    while not _persisted(store, run_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _persisted(store, run_id) == [("text", "streamed")]


def test_events_before_terminal_status_are_kept_and_late_ones_dropped(store):
    run_id = _run(store)
    store.stream_run_event(run_id, "text", "final words")
    store.update_chat_run(run_id, "done", backend_id="b")   # 先排空,再转终态
    store.stream_run_event(run_id, "text", " after the end")
    assert store.run_live_output(run_id) == "final words"
    stopped = _run(store)
    store.stream_run_event(stopped, "tool", "Bash ls\n")
    store.stop_chat_run(stopped)
    store.append_run_event(stopped, "status", "用户已停止")   # 平台事件不看运行状态
    assert [e["kind"] for e in store.run_events(stopped)] == ["tool", "status"]
    store.stream_run_event(10_000, "text", "no such run")
    assert store.run_events(10_000) == []


def test_platform_and_interaction_events_keep_order_with_buffered_stream(store):
    run_id = _run(store)
    store.stream_run_event(run_id, "text", "before the question")
    event_id = store.append_interaction_event(run_id, "permission_request", {"tool": "Bash"})
    store.stream_run_event(run_id, "text", "after")
    store.append_run_event(run_id, "status", "waiting")
    events = store.run_events(run_id)
    assert [e["kind"] for e in events] == ["text", "permission_request", "text", "status"]
    assert events[1]["id"] == event_id


def test_pipe_reader_wait_tracks_progress_not_a_fixed_deadline():
    progress = adapters._ReadProgress()

    def slow_reader():          # 消化积压的总时长远超 idle,但一直有进展
        for _ in range(12):
            time.sleep(0.05)
            progress.tick()

    reader = threading.Thread(target=slow_reader)
    reader.start()
    assert adapters._await_pipe_readers([reader], progress, idle=0.2)
    blocked = threading.Event()
    stuck = threading.Thread(target=blocked.wait, daemon=True)   # 管道被握住:不再有新行
    stuck.start()
    started = time.monotonic()
    assert not adapters._await_pipe_readers([stuck], progress, idle=0.2)
    assert time.monotonic() - started < 1
    blocked.set()
    chatty = threading.Event()                      # 握管道的进程还在不停写:按总时限放弃

    def endless_writer():
        while not chatty.is_set():
            time.sleep(0.02)
            progress.tick()

    writer = threading.Thread(target=endless_writer, daemon=True)
    writer.start()
    started = time.monotonic()
    assert not adapters._await_pipe_readers([writer], progress, idle=0.2, limit=0.5)
    assert time.monotonic() - started < 1.5
    chatty.set()
