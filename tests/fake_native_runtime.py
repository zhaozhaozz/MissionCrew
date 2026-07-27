"""Claude stream-json 与 Codex app-server 的确定性测试进程。"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


_SEND_LOCK = threading.Lock()


def send(payload: dict) -> None:
    with _SEND_LOCK:
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def mark_launch(kind: str) -> None:
    path = os.environ.get("FAKE_NATIVE_LAUNCH_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(kind + "\n")


def codex_finish(thread_id: str, turn_id: str, answer: str) -> None:
    send({"method": "item/reasoning/summaryTextDelta", "params": {
        "threadId": thread_id, "turnId": turn_id, "itemId": "reason-1",
        "delta": "先分析请求。",
    }})
    send({"method": "item/started", "params": {
        "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1,
        "item": {"id": "cmd-1", "type": "commandExecution",
                 "command": "echo fake", "commandActions": [],
                 "cwd": os.getcwd(), "status": "inProgress"},
    }})
    send({"method": "item/commandExecution/outputDelta", "params": {
        "threadId": thread_id, "turnId": turn_id, "itemId": "cmd-1",
        "delta": "fake output\n",
    }})
    send({"method": "item/agentMessage/delta", "params": {
        "threadId": thread_id, "turnId": turn_id, "itemId": "answer-1",
        "delta": answer,
    }})
    send({"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread_id, "turnId": turn_id,
        "tokenUsage": {"total": {"inputTokens": 10, "outputTokens": 4}},
    }})
    send({"method": "turn/completed", "params": {
        "threadId": thread_id,
        "turn": {"id": turn_id, "status": "completed", "error": None,
                 "items": [{"id": "answer-1", "type": "agentMessage",
                            "text": answer}]},
    }})


def run_codex() -> None:
    mark_launch("codex")
    thread_id = "codex-thread-1"
    turn_number = 0
    pending: dict[int, tuple[str, str, str]] = {}
    for raw in sys.stdin:
        message = json.loads(raw)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            send({"id": request_id, "result": {"userAgent": "fake"}})
        elif method == "initialized":
            continue
        elif method == "thread/start":
            send({"id": request_id, "result": {"thread": {"id": thread_id}}})
        elif method == "thread/resume":
            requested = params.get("threadId")
            if requested == "missing-thread":
                send({"id": request_id,
                      "error": {"code": -32000, "message": "thread not found"}})
            else:
                thread_id = str(requested)
                send({"id": request_id, "result": {"thread": {"id": thread_id}}})
        elif method == "model/list":
            send({"id": request_id, "result": {
                "data": [{"id": "gpt-test", "model": "gpt-test", "hidden": False}],
                "nextCursor": None,
            }})
        elif method == "turn/start":
            turn_number += 1
            turn_id = f"turn-{turn_number}"
            prompt = str(((params.get("input") or [{}])[0]).get("text") or "")
            send({"id": request_id, "result": {"turn": {"id": turn_id}}})
            send({"method": "turn/started", "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "inProgress", "items": []},
            }})
            if "ASK_PERMISSION" in prompt:
                pending[900 + turn_number] = ("permission", thread_id, turn_id)
                send({"id": 900 + turn_number,
                      "method": "item/commandExecution/requestApproval",
                      "params": {"threadId": thread_id, "turnId": turn_id,
                                 "itemId": "cmd-approval", "command": "touch file",
                                 "cwd": os.getcwd(), "reason": "test"}})
            elif "ASK_USER" in prompt:
                pending[900 + turn_number] = ("question", thread_id, turn_id)
                send({"id": 900 + turn_number,
                      "method": "item/tool/requestUserInput",
                      "params": {"threadId": thread_id, "turnId": turn_id,
                                 "itemId": "question-1", "questions": [{
                                     "id": "choice", "header": "Choice",
                                     "question": "Which option?", "options": [
                                         {"label": "A", "description": "first"},
                                         {"label": "B", "description": "second"},
                                     ]}]}})
            elif "TRIGGER_COMPACT" in prompt:
                send({"method": "thread/compacted", "params": {
                    "threadId": thread_id, "turnId": turn_id}})
                codex_finish(thread_id, turn_id, f"codex answer {turn_number}")
            else:
                codex_finish(thread_id, turn_id, f"codex answer {turn_number}")
        elif method == "turn/interrupt":
            send({"id": request_id, "result": {}})
        elif request_id in pending and method is None:
            kind, pending_thread, pending_turn = pending.pop(request_id)
            result = message.get("result") or {}
            codex_finish(pending_thread, pending_turn,
                         f"{kind}:{json.dumps(result, sort_keys=True)}")


def claude_finish(session_id: str, number: int, answer: str) -> None:
    send({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "thinking": "先分析请求。"},
    }})
    send({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "tool-1", "name": "Bash",
         "input": {"command": "echo fake"}},
    ]}})
    send({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": answer},
    }})
    send({"type": "result", "subtype": "success", "is_error": False,
          "session_id": session_id, "result": answer, "num_turns": number,
          "total_cost_usd": 0, "duration_ms": 1, "duration_api_ms": 1})


def claude_background_agent(session_id: str, task_id: str,
                            tool_use_id: str, number: int) -> None:
    time.sleep(0.05)
    send({"type": "system", "subtype": "task_progress",
          "task_id": task_id, "tool_use_id": tool_use_id,
          "description": "Verify source references", "subagent_type": "general-purpose",
          "usage": {"total_tokens": 12, "tool_uses": 2, "duration_ms": 50},
          "last_tool_name": "Read", "summary": "Checking remaining references"})
    send({"type": "system", "subtype": "task_updated", "task_id": task_id,
          "patch": {"status": "completed", "end_time": 50}})
    # task_updated 只是任务记录更新，不是父 Agent 已收到结果的完成边界。
    # 在 notification 前插入 result，验证 provider 不会提前结束本轮。
    claude_finish(session_id, number, "phase result after task update")
    send({"type": "system", "subtype": "task_notification",
          "task_id": task_id, "tool_use_id": tool_use_id,
          "status": "completed", "output_file": "/fake/task-output",
          "summary": "Source-reference verification completed",
          "usage": {"total_tokens": 15, "tool_uses": 2, "duration_ms": 55}})
    # 后台结果唤醒主 Agent 后仍可能继续申请工具权限；用于验证 MissionCrew
    # 在首个 result 之后没有提前释放本轮权限租约。
    send({"type": "control_request", "request_id": f"late-{number}",
          "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                      "input": {"command": "echo final-check"}}})


def run_claude() -> None:
    mark_launch("claude")
    args = sys.argv[1:]
    session_id = "claude-session-1"
    if "--resume" in args:
        session_id = args[args.index("--resume") + 1]
    missing_session = session_id == "missing-session"
    if not missing_session:
        send({"type": "system", "subtype": "init", "session_id": session_id,
              "model": "claude-test", "cwd": os.getcwd()})
    turn_number = 0
    pending: tuple[str, int] | None = None
    for raw in sys.stdin:
        message = json.loads(raw)
        if message.get("type") == "user":
            turn_number += 1
            prompt = str((message.get("message") or {}).get("content") or "")
            if missing_session:
                print("Conversation not found for session", file=sys.stderr,
                      flush=True)
                send({"type": "result", "subtype": "error_during_execution",
                      "is_error": True,
                      "error": "Conversation not found for session"})
            elif "ASK_WRITE" in prompt:
                pending = ("permission", turn_number)
                send({"type": "control_request", "request_id": f"p-{turn_number}",
                      "request": {"subtype": "can_use_tool", "tool_name": "Edit",
                                  "input": {"file_path": "blocked.txt",
                                            "old_string": "a", "new_string": "b"}}})
            elif "ASK_NETWORK" in prompt:
                pending = ("permission", turn_number)
                send({"type": "control_request", "request_id": f"p-{turn_number}",
                      "request": {"subtype": "can_use_tool",
                                  "tool_name": "WebFetch",
                                  "input": {"url": "https://example.test"}}})
            elif "ASK_PERMISSION" in prompt:
                pending = ("permission", turn_number)
                send({"type": "control_request", "request_id": f"p-{turn_number}",
                      "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                                  "input": {"command": "touch file"},
                                  "permission_suggestions": [{
                                      "type": "addRules", "behavior": "allow",
                                      "destination": "session", "rules": [{
                                          "toolName": "Bash",
                                          "ruleContent": "touch file",
                                      }],
                                  }]}})
            elif "ASK_USER" in prompt:
                pending = ("question", turn_number)
                send({"type": "control_request", "request_id": f"q-{turn_number}",
                      "request": {"subtype": "can_use_tool",
                                  "tool_name": "AskUserQuestion", "input": {
                                      "questions": [{"header": "Choice",
                                                     "question": "Which option?",
                                                     "options": [
                                                         {"label": "A", "description": "first"},
                                                         {"label": "B", "description": "second"},
                                                     ]}]}}})
            elif "BACKGROUND_AGENT" in prompt:
                legacy_lifecycle = "BACKGROUND_AGENT_LEGACY" in prompt
                tool_use_id = f"agent-tool-{turn_number}"
                task_id = f"agent-task-{turn_number}"
                send({"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Agent",
                     "input": {"description": "Verify source references",
                               "subagent_type": "general-purpose"}},
                ]}})
                if not legacy_lifecycle:
                    send({"type": "system", "subtype": "task_started",
                          "task_id": task_id, "tool_use_id": tool_use_id,
                          "description": "Verify source references",
                          "task_type": "local_agent"})
                send({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": "Async agent launched successfully."},
                ]}})
                pending = ("background_permission", turn_number)
                claude_finish(session_id, turn_number, "phase result")
                threading.Thread(
                    target=claude_background_agent,
                    args=(session_id, task_id, tool_use_id, turn_number),
                    daemon=True).start()
            elif "TRIGGER_COMPACT" in prompt:
                send({"type": "system", "subtype": "compact_boundary",
                      "session_id": session_id,
                      "compact_metadata": {"trigger": "auto",
                                           "pre_tokens": 160000}})
                claude_finish(session_id, turn_number,
                              f"claude answer {turn_number}")
            else:
                claude_finish(session_id, turn_number,
                              f"claude answer {turn_number}")
        elif message.get("type") == "control_response" and pending:
            kind, number = pending
            pending = None
            response = (message.get("response") or {}).get("response") or {}
            answer = ("claude final after background"
                      if kind == "background_permission" else
                      f"{kind}:{json.dumps(response, sort_keys=True)}")
            claude_finish(session_id, number, answer)
        elif (message.get("type") == "control_request"
              and (message.get("request") or {}).get("subtype") == "interrupt"):
            send({"type": "control_response", "response": {
                "subtype": "success", "request_id": message.get("request_id")}})


if "app-server" in sys.argv:
    run_codex()
else:
    run_claude()
