"""opencode CLI 的静态声明。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="opencode",
    binary="opencode",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    command=("opencode", "run", "--dir", "{workdir}",
             "--model", "{model}", "{prompt}"),
    session_id="captured",
    update={"npm": "opencode-ai", "self_update": ["opencode", "upgrade"]},
)
