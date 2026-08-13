"""Cursor Agent CLI 的静态声明。

Cursor print 模式需 --force 才会实际落盘;该 CLI 没有多根目录参数,额外资源
通过绝对路径上下文访问,进程本身不设文件系统沙箱。
"""
from .spec import CliSpec

def session_args(cmd: list[str], session_id: str,
                 reused: bool) -> tuple[list[str], bool]:
    args = ["--output-format", "json"]
    if reused:
        args += ["--resume", session_id]
    return [*cmd, *args], True


SPEC = CliSpec(
    adapter="cursor",
    binary="cursor-agent",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    command=("cursor-agent", "-p", "--force", "{prompt}", "--model", "{model}"),
    session_id="captured",
    update={"self_update": ["cursor-agent", "update"]},
    session_args=session_args,
)
