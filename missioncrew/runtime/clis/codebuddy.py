"""CodeBuddy CLI 的静态声明。"""
from .spec import CliSpec


def apply_permissions(command: list[str], filesystem: str) -> list[str]:
    """与 claude 同一 CLI 方言:只读文件系统映射为 plan 模式。"""
    from ..adapters import _remove_command_option
    if filesystem != "read-only":
        return command
    command = _remove_command_option(command, "--permission-mode", has_value=True)
    return [*command, "--permission-mode", "plan"]


def session_args(cmd: list[str], session_id: str,
                 reused: bool) -> tuple[list[str], bool]:
    return [*cmd, "--resume" if reused else "--session-id", session_id], False


SPEC = CliSpec(
    adapter="codebuddy",
    binary="codebuddy",
    capabilities=("coding",),
    tier="economy",
    cost_per_run=2.0,
    command=("codebuddy", "-p", "{prompt}", "--model", "{model}",
             "--permission-mode", "acceptEdits", "--add-dir", "{allowed_dirs}"),
    session_id="fixed",
    update={"npm": "@tencent-ai/codebuddy-code"},
    apply_permissions=apply_permissions,
    session_args=session_args,
)
