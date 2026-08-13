"""Qoder CLI(ACP stdio)的静态声明。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="qoder",
    binary="qodercli",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    acp_serve=("qodercli", "--add-dir", "{allowed_dirs}", "--yolo", "--acp"),
)
