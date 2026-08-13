"""内置执行器 runtime 的按工具声明注册表。

一个工具一个模块(见 spec.CliSpec 的字段说明);SPECS 的声明顺序即检测报告
(detect_report)的展示顺序。执行行为是统一的(adapters 的 CliAdapter/
AcpAdapter + acp 协议层),新增工具时加一个声明模块并追加进 SPECS,不需要
动执行器;有原生 provider 的工具(claude/codex/pi)执行与 effort 档位在各自
provider 类里,这里只负责检测与回退模板。
"""
from .spec import CliSpec
from . import (claude, codex, grok, opencode, copilot, cursor, codebuddy,
               pi, kimi, kiro, qoder, trae, mock)

SPECS: tuple[CliSpec, ...] = (
    claude.SPEC, codex.SPEC, grok.SPEC, opencode.SPEC, copilot.SPEC,
    cursor.SPEC, codebuddy.SPEC, pi.SPEC, kimi.SPEC, kiro.SPEC,
    qoder.SPEC, trae.SPEC, mock.SPEC,
)
BY_ADAPTER: dict[str, CliSpec] = {spec.adapter: spec for spec in SPECS}

__all__ = ["CliSpec", "SPECS", "BY_ADAPTER"]
