"""内置执行器 runtime 的声明式描述。

每个工具一个模块,声明检测方式、命令模板、能力、模型与升级渠道;adapters 层
把全部声明汇总成检测/执行用的注册表(DEFAULT_COMMANDS、ACP_SERVE_COMMANDS、
KNOWN_CLIS 等)。执行行为本身是统一的(CliAdapter/AcpAdapter + acp 协议层),
这里只放"这个工具长什么样"的静态事实;有原生 provider 的工具(claude/codex/pi)
执行走各自 provider 类,这里只负责检测与回退模板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CliSpec:
    """一个内置执行器 runtime 的静态声明。"""

    adapter: str                        # 平台内 adapter 名
    binary: str = ""                    # 本地检测的可执行名;空 = 不参与自动检测
    detect_id: str = ""                 # 检测报告对外 id;空 = 用 adapter 名
    capabilities: tuple[str, ...] = ()
    tier: str = "standard"
    cost_per_run: float = 5.0
    # print 模式命令模板(汇总成 DEFAULT_COMMANDS);与 acp_serve 互斥
    command: tuple[str, ...] = ()
    # ACP serve 命令(汇总成 ACP_SERVE_COMMANDS;prompt 走协议,无 {prompt})
    acp_serve: tuple[str, ...] = ()
    # print 模式的原生会话恢复方式:"fixed" = 平台生成固定 id 传入,
    # "captured" = 从输出捕获 CLI 生成的 id;"" = 打印模式不支持会话恢复
    session_id: str = ""
    # 工具自带模型清单(汇总成 KNOWN_MODELS);"" = CLI 默认模型,排在最前
    models: tuple[str, ...] = ()
    # 静态 effort 档位兜底(汇总成 EFFORT_SUPPORT);只有内置执行器负责的
    # adapter 在此声明,原生 provider 的档位在各自 provider 类里
    efforts: tuple[str, ...] = ()
    # 升级渠道(汇总成 UPDATE_SPECS):self_update = 自带更新子命令(优先),
    # npm = 包名,用于最新版比对;仅二进制确实由 npm 管理时才允许 -g 更新
    update: dict = field(default_factory=dict)
    # ACP session/load 的额外 _meta(如 grok 的 noReplay)
    load_session_meta: Optional[dict] = None
    account_usage: bool = False         # usage.py 有对应账户限额探测
