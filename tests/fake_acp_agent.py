#!/usr/bin/env python3
"""测试用假 ACP agent:实现最小的服务端协议,验证客户端全流程。

行为:initialize/session/new 正常应答;session/prompt 时先发一个文本块,
再反向发起 session/request_permission(验证客户端会从 options 里选
allow_once),收到应答后把所选 optionId 写进第二个文本块,最后结束回合。

argv[1] 控制 session/new 的模型目录形态:缺省 = kimi 形态(configOptions);
"trae" = trae 形态(models.availableModels + currentModelId,无 configOptions)。
"""
import json
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def chunk(text, meta=None):
    params = {
        "sessionId": "s-test",
        "update": {"sessionUpdate": "agent_message_chunk",
                   "content": {"type": "text", "text": text}}}
    if meta:
        params["_meta"] = meta
    send({"jsonrpc": "2.0", "method": "session/update", "params": params})


def _session_new_result(shape):
    if shape == "trae":   # 真实 traecli 的形态(实测 + Multica 对齐)
        return {"sessionId": "s-test",
                "models": {"availableModels": [
                    {"modelId": "GLM-5.2", "name": "GLM-5.2", "description": ""},
                    {"modelId": "Kimi-K2.6", "name": "Kimi K2.6", "description": ""}],
                    "currentModelId": "GLM-5.2"}}
    return {"sessionId": "s-test",
            "configOptions": [{
                "type": "select", "id": "model", "category": "model",
                "currentValue": "fake/base",
                "options": [{"value": "fake/base", "name": "Base"},
                            {"value": "fake/pro", "name": "Pro"}]}]}


def main():
    shape = sys.argv[1] if len(sys.argv) > 1 else "config"
    model = ""
    new_count = 0
    load_count = 0
    load_no_replay = False
    prompt_count = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": shape != "noload"},
            }})
        elif method == "session/new":
            new_count += 1
            send({"jsonrpc": "2.0", "id": mid,
                  "result": _session_new_result(shape)})
        elif method == "session/load":
            load_count += 1
            load_no_replay = bool(
                (msg["params"].get("_meta") or {}).get("noReplay"))
            if shape == "replay" and not load_no_replay:
                chunk("不应进入当前回合的历史", {"isReplay": True})
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"sessionId": msg["params"]["sessionId"]}})
        elif method == "session/set_model":
            model = msg["params"]["modelId"]
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/prompt":
            prompt_count += 1
            if shape == "slow":
                time.sleep(1)
            text = msg["params"]["prompt"][0]["text"]
            if shape == "detached":
                if prompt_count == 1:
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "s-test",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "background-launch",
                            "title": "Bash",
                            "kind": "execute",
                            "status": "pending",
                        }}})
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "s-test",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "background-launch",
                            "title": "Start background process",
                            "kind": "execute",
                            "status": "in_progress",
                            "rawInput": {
                                "command": "long-running-check",
                                "run_in_background": True,
                            },
                        }}})
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "s-test",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "background-launch",
                            "status": "completed",
                            "rawOutput": (
                                "task_id: fake-background-1\n"
                                "status: running\n"
                                "automatic_notification: true"
                            ),
                        }}})
                    chunk("后台任务已启动")
                else:
                    assert "fake-background-1" in text
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "s-test",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "background-wait",
                            "title": "TaskOutput",
                            "kind": "execute",
                            "status": "in_progress",
                            "rawInput": {
                                "task_id": "fake-background-1",
                                "block": True,
                            },
                        }}})
                    time.sleep(0.3)
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "s-test",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "background-wait",
                            "status": "completed",
                            "rawOutput": (
                                "retrieval_status: success\n"
                                "task_id: fake-background-1\n"
                                "status: completed"
                            ),
                        }}})
                    chunk(";后台任务结果=completed")
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"stopReason": "end_turn"}})
                continue
            # 思考与工具调用通知:验证客户端把运行过程实时上报
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s-test",
                "update": {"sessionUpdate": "agent_thought_chunk",
                           "content": {"type": "text", "text": "思考中…"}}}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s-test",
                "update": {"sessionUpdate": "tool_call", "toolCallId": "t1",
                           "title": "read_file", "status": "completed"}}})
            chunk(f"ACP 收到任务({len(text)} 字符);轮次={prompt_count};"
                  f"new={new_count};load={load_count};"
                  f"noReplay={int(load_no_replay)}")
            # 反向权限请求:客户端必须从 options 里选安全项,否则本进程会卡住
            send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission",
                  "params": {"sessionId": "s-test", "options": [
                      {"optionId": "no", "kind": "reject_once"},
                      {"optionId": "yes-once", "kind": "allow_once"}]}})
            opt = ""
            for line2 in sys.stdin:
                resp = json.loads(line2.strip())
                if resp.get("id") == 900:
                    opt = resp["result"]["outcome"]["optionId"]
                    break
            chunk(f";权限选择={opt}" + (f";模型={model}" if model else ""))
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


if __name__ == "__main__":
    main()
