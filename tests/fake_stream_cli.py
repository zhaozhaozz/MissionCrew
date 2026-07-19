#!/usr/bin/env python3
"""测试用假 stream-json CLI:模仿 claude --output-format stream-json 的事件流。

argv 模式:"plain" 普通 CLI 行输出;"noresult" 只发文本块、不发 result
事件就退出(模拟异常中断);"grandchild" 打印回复后留下持有 stdout 的
孙进程(验证进程组清理)。缺省输出完整事件流。
"""
import json
import subprocess
import sys


def line(d):
    print(json.dumps(d, ensure_ascii=False), flush=True)


def main():
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
