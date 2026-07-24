#!/usr/bin/env python3
"""测试用假 stream-json CLI:模仿 claude --output-format stream-json 的事件流。

argv 模式:"plain" 普通 CLI 行输出;"noresult" 只发文本块、不发 result
事件就退出(模拟异常中断);"grandchild" 打印回复后留下持有 stdout 的
孙进程(验证进程组清理)；"opencode-*" 模拟 OpenCode JSON 终态。
缺省输出完整事件流。
"""
import json
import subprocess
import sys


def line(d):
    print(json.dumps(d, ensure_ascii=False), flush=True)


def main():
    if "plain-long" in sys.argv:
        for i in range(500):
            print(f"完整回复第{i:03d}行", flush=True)
        return
    if "long" in sys.argv:
        reply = "完整开头：不能丢失\n" + "长回复正文" * 1200
        line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": reply}]}})
        line({"type": "result", "subtype": "success", "result": reply,
              "is_error": False})
        return
    if "plain" in sys.argv:
        print("第一行进度", flush=True)
        print("第二行进度", flush=True)
        print("警告:示例 stderr", file=sys.stderr, flush=True)
        print("最终回复", flush=True)
        return
    if "noresult" in sys.argv:
        line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "中断前的部分回复"}]}})
        return
    if "opencode-tool-denied" in sys.argv:
        line({"type": "text", "part": {
            "type": "text", "text": "我先开始检查。"}})
        line({"type": "step_finish", "part": {
            "type": "step-finish", "reason": "tool-calls"}})
        line({"type": "tool_use", "part": {
            "type": "tool", "tool": "read", "state": {
                "status": "error",
                "input": {"filePath": "/vault/Daily/today.md"},
                "error": "The user rejected permission to use this specific tool call.",
            }}})
        line({"type": "step_finish", "part": {
            "type": "step-finish", "reason": "tool-calls"}})
        print("permission requested: external_directory (/vault/Daily/*); "
              "auto-rejecting", file=sys.stderr, flush=True)
        return
    if "opencode-recovered" in sys.argv:
        line({"type": "text", "part": {
            "type": "text", "text": "我先开始检查。"}})
        line({"type": "tool_use", "part": {
            "type": "tool", "tool": "read", "state": {
                "status": "error",
                "input": {"filePath": "/vault/Daily/today.md"},
                "error": "permission denied",
            }}})
        line({"type": "step_finish", "part": {
            "type": "step-finish", "reason": "tool-calls"}})
        line({"type": "text", "part": {
            "type": "text", "text": "已跳过无权限目录，核心任务完成。"}})
        line({"type": "step_finish", "part": {
            "type": "step-finish", "reason": "stop"}})
        return
    if "codex" in sys.argv:
        # 模仿 codex exec:过程日志全走 stderr(实测 0.144 分节格式),
        # stdout 只有最终回复
        err = sys.stderr
        print("Reading additional input from stdin...", file=err, flush=True)
        print("OpenAI Codex v0.144.6", file=err)
        print("--------", file=err)
        print("workdir: /tmp/x", file=err)
        print("model: gpt-test", file=err)
        print("reasoning effort: high", file=err)
        print("--------", file=err)
        print("user", file=err)
        print("# 聊天协作请求", file=err)
        print("这里是很长的提示词回显", file=err)
        print("thinking", file=err)
        print("先理解需求再回答", file=err)
        print("exec bash -lc 'echo hi'", file=err)
        print("hi", file=err)
        print("codex", file=err)
        print("最终回复正文", file=err)
        print("tokens used", file=err)
        print("12,008", file=err, flush=True)
        print("最终回复正文", flush=True)   # stdout
        return
    if "grandchild" in sys.argv:
        print("REAL-ANSWER", flush=True)
        # 孙进程继承 stdout 并持续写入;本进程立即退出
        subprocess.Popen([sys.executable, "-c",
                          "import time\n"
                          "for i in range(60):\n"
                          "    print(f'tick {i}', flush=True)\n"
                          "    time.sleep(0.5)\n"])
        return
    line({"type": "system", "subtype": "init", "model": "fake-model"})
    line({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12})
    line({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "先复述要求,再执行。"},
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "echo hi"}}]}})
    line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "hi",
         "is_error": False}]}})
    line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "OK"}]}})
    line({"type": "result", "subtype": "success", "result": "最终回复:OK",
          "is_error": False})


if __name__ == "__main__":
    main()
