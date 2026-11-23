"""Kiro CLI(ACP stdio)的静态声明。"""
from .spec import CliSpec

def private_dirs() -> list[str]:
    """Kiro CLI 的用户级配置目录。"""
    from pathlib import Path
    return [str(Path.home() / ".kiro")]


SPEC = CliSpec(
    adapter="kiro",
    binary="kiro-cli",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    acp_serve=("kiro-cli", "acp", "--trust-all-tools"),
    private_dirs=private_dirs,
)
