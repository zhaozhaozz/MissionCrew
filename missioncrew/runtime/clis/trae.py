"""Trae CLI(ACP stdio)的静态声明。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="trae",
    binary="traecli",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    acp_serve=("traecli", "--add-dir", "{allowed_dirs}",
               "acp", "serve", "--yolo"),
    # 与 kimi 同理:版本序列与公开包对不上,只提供自更新
    update={"self_update": ["traecli", "update"]},
)
