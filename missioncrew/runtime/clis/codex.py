"""Codex CLI 的静态声明。

执行走原生 provider(runtime/codex.py 的 CodexRuntimeProvider,effort 档位
也在那里声明);这里负责检测、print 模式回退模板与升级渠道。
"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="codex",
    binary="codex",
    capabilities=("coding", "reasoning", "review", "security"),
    tier="standard",
    cost_per_run=5.0,
    command=("codex", "exec", "--sandbox", "workspace-write", "--add-dir",
             "{allowed_dirs}", "-m", "{model}",
             "-c", "model_reasoning_effort={effort}", "{prompt}"),
    session_id="captured",
    update={"npm": "@openai/codex"},
)
