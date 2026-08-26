#!/usr/bin/env python3
"""测试用假 ACP agent:实现最小的服务端协议,验证客户端全流程。

行为:initialize/session/new 正常应答;session/prompt 时先发一个文本块,
再反向发起 session/request_permission(验证客户端会从 options 里选
allow_once),收到应答后把所选 optionId 写进第二个文本块,最后结束回合。

argv[1] 控制 session/new 的模型目录形态:缺省 = kimi 形态(configOptions,含
category=thought_level 的 `thinking` 档位选项,取值随当前模型变化,经
session/set_config_option 切换模型/档位);"trae" = trae 形态
(models.availableModels + currentModelId,无 configOptions)。
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


# kimi 形态的按模型档位:与真实 kimi 一致,K3 系列 low/high/max,K2.7 系列 on/high
_THINKING_LEVELS = {"fake/base": ["low", "high", "max"],
                    "fake/pro": ["on", "high"]}


def _config_options(model, thinking):
    model = model or "fake/base"
    return [{
        "type": "select", "id": "model", "category": "model",
        "currentValue": model,
        "options": [{"value": "fake/base", "name": "Base"},
                    {"value": "fake/pro", "name": "Pro"}]},
        {"type": "select", "id": "thinking", "name": "Thinking",
         "category": "thought_level", "currentValue": thinking or "high",
         "options": [{"value": v, "name": f"Thinking {v}"}
                     for v in _THINKING_LEVELS[model]]}]


def _session_new_result(shape, model="", thinking=""):
    if shape == "efforts":  # 真实 grok 的形态:每个模型自报推理力度(高到低)
        return {"sessionId": "s-test",
                "models": {"availableModels": [
                    {"modelId": "fake-4.6", "name": "Fake 4.6", "_meta": {
                        "supportsReasoningEffort": True,
                        "reasoningEfforts": [
                            {"id": "xhigh", "value": "xhigh", "label": "Extra High"},
                            {"id": "high", "value": "high", "label": "High"},
                            {"id": "medium", "value": "medium", "label": "Medium"},
                            {"id": "low", "value": "low", "label": "Low"}]}},
                    {"modelId": "fake-4.5", "name": "Fake 4.5", "_meta": {
                        "supportsReasoningEffort": True,
                        "reasoningEfforts": [
                            {"id": "high", "value": "high", "label": "High"},
                            {"id": "medium", "value": "medium", "label": "Medium"},
                            {"id": "low", "value": "low", "label": "Low"}]}},
                    # 不支持推理力度的模型不进档位表,调用方回退 adapter 静态档位
                    {"modelId": "fake-mini", "name": "Fake Mini", "_meta": {
                        "supportsReasoningEffort": False}}],
                    "currentModelId": "fake-4.6"}}
    if shape == "trae":   # 真实 traecli 的形态(实测 + Multica 对齐)
        return {"sessionId": "s-test",
                "models": {"availableModels": [
                    {"modelId": "GLM-5.2", "name": "GLM-5.2", "description": ""},
                    {"modelId": "Kimi-K2.6", "name": "Kimi K2.6", "description": ""}],
                    "currentModelId": "GLM-5.2"}}
    return {"sessionId": "s-test",
            "configOptions": _config_options(model, thinking)}


def main():
    shape = sys.argv[1] if len(sys.argv) > 1 else "config"
    model = ""
    thinking = ""
    new_count = 0
    load_count = 0
    load_no_replay = False
    prompt_count = 0
    pending_terminal = ""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method is None and mid == 902 and pending_terminal:
            # 客户端终端退出(wait_for_exit 返回):模拟 Grok 在 turn 之外
            # 自发跑一轮汇报——读取输出并推送 agent_message_chunk
            exit_code = (msg.get("result") or {}).get("exitCode")
            send({"jsonrpc": "2.0", "id": 903, "method": "terminal/output",
                  "params": {"sessionId": "s-test",
                             "terminalId": pending_terminal}})
            output = ""
            for line2 in sys.stdin:
                resp = json.loads(line2.strip())
                if resp.get("id") == 903:
                    output = resp["result"]["output"]
                    break
            send({"jsonrpc": "2.0", "id": 904, "method": "terminal/release",
                  "params": {"sessionId": "s-test",
                             "terminalId": pending_terminal}})
            pending_terminal = ""
            chunk(f"自发汇报:后台命令完成 exit={exit_code}"
                  f" 输出={output.strip()}")
            continue
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
            # 与真实 kimi 一致:load 应答同样带 configOptions(含档位选项)
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {**_session_new_result(shape, model, thinking),
                             "sessionId": msg["params"]["sessionId"]}})
        elif method == "session/set_model":
            model = msg["params"]["modelId"]
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/set_config_option":
            # ACP 标准会话配置项:model 切换后档位选项随之变化;thinking 只认
            # 当前模型的取值,越界返回 -32602(真实 kimi 的报错形态)
            config_id = msg["params"]["configId"]
            value = msg["params"]["value"]
            if config_id == "model":
                model = value
            elif config_id == "thinking":
                if value not in _THINKING_LEVELS.get(model or "fake/base", []):
                    send({"jsonrpc": "2.0", "id": mid, "error": {
                        "code": -32602,
                        "message": f"Invalid params: Unknown thinking value: {value}",
                        "data": {"configId": config_id, "value": value}}})
                    continue
                thinking = value
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "configOptions": _config_options(model, thinking)}})
        elif method == "session/prompt":
            prompt_count += 1
            if shape == "slow":
                time.sleep(1)
            text = msg["params"]["prompt"][0]["text"]
            if shape == "terminal":
                # 模拟 Grok:后台命令交给客户端终端,turn 立即结束;终端退出
                # 后(上方 mid==902 分支)在 turn 之外自发汇报
                send({"jsonrpc": "2.0", "id": 901, "method": "terminal/create",
                      "params": {"sessionId": "s-test", "command": "sh",
                                 "args": ["-c",
                                          "sleep 0.4; echo FAKE_TERMINAL_DONE"]}})
                for line2 in sys.stdin:
                    resp = json.loads(line2.strip())
                    if resp.get("id") == 901:
                        pending_terminal = resp["result"]["terminalId"]
                        break
                send({"jsonrpc": "2.0", "id": 902,
                      "method": "terminal/wait_for_exit",
                      "params": {"sessionId": "s-test",
                                 "terminalId": pending_terminal}})
                chunk("后台命令已交给客户端终端")
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"stopReason": "end_turn"}})
                continue
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
            chunk(f";权限选择={opt}" + (f";模型={model}" if model else "")
                  + (f";思考={thinking}" if thinking else ""))
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


if __name__ == "__main__":
    main()
