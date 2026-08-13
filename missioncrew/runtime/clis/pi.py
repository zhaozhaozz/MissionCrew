"""pi 的静态声明。

执行走原生 provider(runtime/pi.py 的 PiRuntimeProvider,effort 档位也在那里
声明);pi 是平台 vendored 安装(MC_HOME/pi/vendor),检测不看系统 PATH,由
detect_report 按 adapter 特判。
"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="pi",
    binary="pi",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    # 版本比对走 npm registry;更新在 update_plan 里按 vendor 前缀特判
    # (npm --prefix),不允许 -g 全局安装
    update={"npm": "@mariozechner/pi-coding-agent"},
)
