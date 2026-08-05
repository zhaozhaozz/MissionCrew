"""模拟 pi ``--mode rpc`` 的假进程,行式 JSON 协议与真实事件序列一致。

场景由环境变量 FAKE_PI_SCENARIO 控制:
  (默认)   正常回合:流式 text_delta + message_end + agent_end
  retry     第一次尝试 stopReason=error,agent_end 后紧跟 auto_retry_start,
            第二次尝试成功(验证 agent_end 不能直接判终)
  error     回合以错误结束且不重试
  empty     成功但零输出(无任何 text 内容)
FAKE_PI_LAUNCH_LOG 指定启动参数记录文件(每次启动追加一行 JSON argv)。
"""
import json
import os
import sys
from pathlib import Path


def _arg(name: str) -> str:
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args else ""


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def respond(command: dict, name: str, data=None, success=True) -> None:
    if "id" not in command:
        return
    payload = {"id": command["id"], "type": "response", "command": name,
               "success": success}
    if data is not None:
        payload["data"] = data
    emit(payload)


def main() -> None:
    log = os.environ.get("FAKE_PI_LAUNCH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")
    scenario = os.environ.get("FAKE_PI_SCENARIO", "")
    session_dir = _arg("--session-dir") or "."
    session_file = _arg("--session")
    no_session = "--no-session" in sys.argv
    if not session_file and not no_session:
        session_file = str(Path(session_dir) / f"fake-{os.getpid()}.jsonl")
    if session_file:
        Path(session_file).parent.mkdir(parents=True, exist_ok=True)
        Path(session_file).write_text("{}\n", encoding="utf-8")
    model = _arg("--model") or "test-openai/test-model"
    thinking = _arg("--thinking") or "off"
    prompts = 0

    def state() -> dict:
        provider, _, model_id = model.partition("/")
        return {
            "model": {"provider": provider, "id": model_id},
            "thinkingLevel": thinking, "isStreaming": False,
            "sessionFile": session_file, "sessionId": "fake-session-id",
        }

    def assistant_message(text: str, stop: str = "stop",
                          error: str = "") -> dict:
        message = {
            "role": "assistant",
            "content": ([{"type": "text", "text": text}] if text else []),
            "usage": {"input": 10, "output": 5, "totalTokens": 15,
                      "cost": {"total": 0.01}},
            "stopReason": stop, "timestamp": 0,
        }
        if error:
            message["errorMessage"] = error
        return message

    def run_attempt(text: str, *, fail: str = "") -> None:
        emit({"type": "agent_start"})
        emit({"type": "turn_start"})
        if fail:
            message = assistant_message("", stop="error", error=fail)
        else:
            emit({"type": "tool_execution_start", "toolCallId": "t1",
                  "toolName": "bash", "args": {"command": "true"}})
            emit({"type": "tool_execution_end", "toolCallId": "t1",
                  "result": {"content": [{"type": "text", "text": "ok"}]}})
            for piece in (text[: len(text) // 2], text[len(text) // 2:]):
                if piece:
                    emit({"type": "message_update", "assistantMessageEvent": {
                        "type": "text_delta", "delta": piece}})
            emit({"type": "message_update", "assistantMessageEvent": {
                "type": "thinking_delta", "delta": "thinking..."}})
            message = assistant_message(text)
        emit({"type": "message_end", "message": message})
        emit({"type": "turn_end", "message": message, "toolResults": []})
        emit({"type": "agent_end", "messages": [message]})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = command.get("type")
        if kind == "get_state":
            respond(command, kind, state())
        elif kind == "set_model":
            model = (f"{command.get('provider')}/"
                     f"{command.get('modelId')}")
            respond(command, kind, {})
        elif kind == "set_thinking_level":
            thinking = str(command.get("level") or "off")
            respond(command, kind, {})
        elif kind == "get_available_models":
            respond(command, kind, {"models": [
                {"provider": "test-openai", "id": "test-model"}]})
        elif kind == "abort":
            respond(command, kind, {})
            message = assistant_message("", stop="aborted")
            emit({"type": "message_end", "message": message})
            emit({"type": "agent_end", "messages": [message]})
        elif kind == "prompt":
            respond(command, kind, {})
            prompts += 1
            if scenario == "error":
                run_attempt("", fail="provider rejected request")
            elif scenario == "empty":
                run_attempt("")
            elif scenario == "retry" and prompts == 1:
                run_attempt("", fail="Connection error.")
                emit({"type": "auto_retry_start", "attempt": 1,
                      "maxAttempts": 3, "delayMs": 10,
                      "errorMessage": "Connection error."})
                run_attempt(f"pi answer {prompts}")
            else:
                run_attempt(f"pi answer {prompts}")
        else:
            respond(command, str(kind), {})


if __name__ == "__main__":
    main()
