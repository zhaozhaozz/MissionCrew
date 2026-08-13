"""Kiro CLI(ACP stdio)的静态声明。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="kiro",
    binary="kiro-cli",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    acp_serve=("kiro-cli", "acp", "--trust-all-tools"),
)
