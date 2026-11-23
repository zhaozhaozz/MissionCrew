"""Qoder CLI(ACP stdio)的静态声明。"""
from .spec import CliSpec

def private_dirs() -> list[str]:
    """Qoder CLI 的用户级配置目录。"""
    from pathlib import Path
    return [str(Path.home() / ".qoder")]


SPEC = CliSpec(
    adapter="qoder",
    binary="qodercli",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    acp_serve=("qodercli", "--add-dir", "{allowed_dirs}", "--yolo", "--acp"),
    private_dirs=private_dirs,
)
