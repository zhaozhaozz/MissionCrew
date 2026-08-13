"""Grok Build CLI(ACP stdio)的静态声明。

Grok 的 print 模式按无换行 token flush,通用逐行读取器无法实时消费;原生 ACP
同时提供正文、思考、工具、权限和可复用 session 生命周期,因此走 acp_serve。
"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="grok_build",
    binary="grok",
    detect_id="grok",
    capabilities=("coding", "reasoning", "review", "web_search", "sub_agents"),
    tier="standard",
    cost_per_run=5.0,
    # effort 进了 serve 命令,改档位会改变 acp.py 的 client signature,
    # 长驻会话按新命令重启,不会沿用旧档位
    acp_serve=("grok", "--cwd", "{workdir}", "agent",
               "--reasoning-effort", "{effort}",
               "--always-approve", "--no-leader", "stdio"),
    # 静态兜底档位是 grok-4.6 的全量;实际档位按模型动态发现
    # (list_runtime_model_catalog),因为低档模型只认子集(grok-4.5 无 xhigh),
    # 而 `grok agent` 不校验档位——越界静默回落到模型默认(xhigh + grok-4.5
    # 实测落到 high),不像 codex 那样报错,不要指望错误回流
    efforts=("low", "medium", "high", "xhigh"),
    update={"self_update": ["grok", "update"]},
    # session/load 带 noReplay:恢复会话时不回放历史,避免把旧输出当本轮
    load_session_meta={"noReplay": True},
    account_usage=True,
)
