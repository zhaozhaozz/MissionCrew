"""GitHub Copilot CLI(ACP stdio)的静态声明。

copilot 1.0.80 起提供 `--acp` 服务模式,能力全面优于旧打印模式:
session/new 返回完整模型目录(auto + 各厂商模型,_meta 携带
copilotUsage 价格倍率),session/set_model 每轮生效,loadSession=true
支持服务重启后恢复会话。session/load 会回放历史且不识别 noReplay
_meta,但协议层在 load 之后才 begin_turn,回放不会混入本轮回复。
"""
from .spec import CliSpec


SPEC = CliSpec(
    adapter="copilot",
    binary="copilot",
    capabilities=("coding", "reasoning"),
    tier="economy",
    cost_per_run=2.0,
    # --no-ask-user:无头执行,禁用 ask_user 工具避免等待人工输入;
    # --allow-all-tools 在 approval != auto 时由权限策略层剥除,改走
    # ACP session/request_permission 应答。effort 进 serve 命令,改档位
    # 改变 client signature,长驻会话按新命令重启(同 grok)。
    acp_serve=("copilot", "--acp", "--allow-all-tools", "--no-ask-user",
               "--effort", "{effort}", "--add-dir={allowed_dirs}"),
    # 静态兜底档位 = CLI --effort 的全量取值;目录探测不返回按模型档位,
    # 不支持的模型由 copilot 自行忽略档位,不会报错
    efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    # copilot 常由 VS Code 扩展托管;仅当二进制确实由 npm 管理时才允许
    # `npm install -g` 更新,update_plan 会先做归属判断
    update={"npm": "@github/copilot"},
)
