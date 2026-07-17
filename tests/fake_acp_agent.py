#!/usr/bin/env python3
"""测试用假 ACP agent:实现最小的服务端协议,验证客户端全流程。

行为:initialize/session/new 正常应答;session/prompt 时先发一个文本块,
再反向发起 session/request_permission(验证客户端会从 options 里选
allow_once),收到应答后把所选 optionId 写进第二个文本块,最后结束回合。
"""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def chunk(text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "s-test",
        "update": {"sessionUpdate": "agent_message_chunk",
                   "content": {"type": "text", "text": text}}}})


def main():
    model = ""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s-test"}})
        elif method == "session/set_model":
            model = msg["params"]["modelId"]
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/prompt":
            text = msg["params"]["prompt"][0]["text"]
            chunk(f"ACP 收到任务({len(text)} 字符)")
            # 反向权限请求:客户端必须从 options 里选安全项,否则本进程会卡住
            send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission",
                  "params": {"sessionId": "s-test", "options": [
                      {"optionId": "no", "kind": "reject_once"},
                      {"optionId": "yes-once", "kind": "allow_once"}]}})
            for line2 in sys.stdin:
                resp = json.loads(line2.strip())
                if resp.get("id") == 900:
                    opt = resp["result"]["outcome"]["optionId"]
                    break
            chunk(f";权限选择={opt}" + (f";模型={model}" if model else ""))
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


if __name__ == "__main__":
    main()
