"""聊天中的原生 Runtime 用户交互桥。"""
from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.runtime import runtime_manager


def test_pending_runtime_question_is_resolved_without_persisting_answer(store):
    store._execute("INSERT INTO channels(id,data) VALUES(?,?)",
                   ("channel", '{"id":"channel","name":"channel"}'))
    trigger = store.add_message("channel", "human", "human", "question", [])
    run_id = store.add_chat_run("channel", "lead", trigger, trigger, 0)
    chat = ChatEngine(store, max_workers=1)
    result = {}

    def ask():
        result.update(chat._request_runtime_interaction(
            run_id, "codex", "user_input_request", {
                "runtime": "codex", "questions": [{
                    "id": "secret", "header": "Token", "question": "Enter token",
                    "isSecret": True,
                }],
            }, None))

    worker = threading.Thread(target=ask)
    worker.start()
    for _ in range(50):
        events = store.run_events(run_id)
        if events:
            break
        time.sleep(0.02)
    payload = json.loads(events[0]["content"])
    chat.respond_interaction(run_id, payload["request_id"], {
        "decision": "submit", "answers": {"secret": ["do-not-persist"]},
    })
    worker.join(timeout=5)
    assert result["answers"] == {"secret": ["do-not-persist"]}
    stored = store.run_events(run_id)[0]["content"]
    assert "do-not-persist" not in stored
    assert json.loads(stored)["status"] == "resolved"


def test_interaction_completion_does_not_overwrite_finished_run(store):
    store._execute("INSERT INTO channels(id,data) VALUES(?,?)",
                   ("channel", '{"id":"channel","name":"channel"}'))
    trigger = store.add_message("channel", "human", "human", "question", [])
    run_id = store.add_chat_run("channel", "lead", trigger, trigger, 0)
    chat = ChatEngine(store, max_workers=1)

    worker = threading.Thread(target=lambda: chat._request_runtime_interaction(
        run_id, "claude", "permission_request", {"tool": "Bash"}, 5))
    worker.start()
    for _ in range(50):
        events = store.run_events(run_id)
        if events:
            break
        time.sleep(0.02)
    payload = json.loads(events[0]["content"])
    store.update_chat_run(run_id, "failed", backend_id="claude", error="timeout")
    chat.respond_interaction(run_id, payload["request_id"], {
        "decision": "deny", "answers": {},
    })
    worker.join(timeout=5)
    row = store._query("SELECT status,error FROM chat_runs WHERE id=?", (run_id,))[0]
    assert row["status"] == "failed"
    assert row["error"] == "timeout"


def test_channel_stop_cancels_pending_interaction_without_reopening_run(
        seeded, monkeypatch):
    trigger = seeded.add_message("general", "human", "human", "question", [])
    run_id = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    seeded.update_chat_run(run_id, "running", backend_id="std-1")
    chat = ChatEngine(seeded, max_workers=1)
    result = {}

    worker = threading.Thread(target=lambda: result.update(
        chat._request_runtime_interaction(
            run_id, "std-1", "permission_request", {"tool": "Bash"}, None)))
    worker.start()
    for _ in range(50):
        if any(event["kind"] == "permission_request"
               for event in seeded.run_events(run_id)):
            break
        time.sleep(0.02)
    assert any(event["kind"] == "permission_request"
               for event in seeded.run_events(run_id))

    monkeypatch.setattr(runtime_manager, "stop", lambda *_args, **_kwargs: 1)
    stopped = chat.stop_channel_agents("general")
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert stopped["stopped_runs"] == 1
    assert result["decision"] == "cancel"
    assert "用户已停止" in result["reason"]
    row = seeded._query(
        "SELECT status FROM chat_runs WHERE id=?", (run_id,))[0]
    assert row["status"] == "stopped"
    interaction = next(event for event in seeded.run_events(run_id)
                       if event["kind"] == "permission_request")
    payload = json.loads(interaction["content"])
    assert payload["status"] == "stopped" and payload["decision"] == "cancel"
    audit = next(row for row in seeded.list_audit(limit=20)
                 if row["action"] == "runtime_interaction_resolved")
    assert audit["actor"] == "platform"


def test_chat_assets_include_runtime_interaction_controls(seeded):
    client = TestClient(create_app())
    js = client.get("/assets/js/sidebar.js").text
    assert "submitRuntimeAnswers" in js
    assert "sendRuntimeInteraction" in js
    assert "waiting_user" in js
    assert "permission_request" in js and "user_input_request" in js
