"""内置执行器 runtime 的声明式描述。

每个工具一个模块,声明检测方式、命令模板、能力、模型与升级渠道;adapters 层
把全部声明汇总成检测/执行用的注册表(DEFAULT_COMMANDS、ACP_SERVE_COMMANDS、
KNOWN_CLIS 等)。执行行为本身是统一的(CliAdapter/AcpAdapter + acp 协议层),
这里只放"这个工具长什么样"的静态事实;有原生 provider 的工具(claude/codex/pi)
执行走各自 provider 类,这里只负责检测与回退模板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


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
    # 模型发现:(backend, timeout) -> (模型目录, 按模型 effort 档位)。
    # 工具自己的枚举方式(枚举子命令、静态目录)写在各自声明模块里;
    # None 且声明了 acp_serve 时走通用 ACP 探测(session/new 返回目录),
    # 两者都无 = 该工具不支持模型枚举。失败由回调自行兜底返回空。
    discover_models: Optional[
        Callable[..., tuple[list[str], dict[str, list[str]]]]] = None

    # ---- 以下是可选的工具特有行为钩子:声明了才生效,None = 用通用行为。
    # 钩子体内如需 adapters 的工具函数,用函数内延迟导入避免环形依赖。

    # ACP session/new models 块 -> {模型: 档位};解析厂商私有扩展
    # (如 grok 的 _meta.reasoningEfforts),协议层不认识这些字段
    parse_model_efforts: Optional[Callable[..., dict[str, list[str]]]] = None
    # () -> 可执行路径;不经 PATH 检测的安装方式(pi vendored)
    locate_binary: Optional[Callable[[], str]] = None
    # () -> 工具自带模型清单;清单需动态读取时替代静态 models(pi models.json)
    configured_models: Optional[Callable[[], list[str]]] = None
    # () -> ("npm"|"self", cmd);覆盖通用升级计划(pi 只写 vendor 目录)
    update_plan: Optional[Callable[[], tuple]] = None
    # (command, filesystem) -> command;把统一文件系统权限翻译为原生参数
    apply_permissions: Optional[Callable[..., list[str]]] = None
    # (env, external_dirs) -> env;需要配置式多目录授权的工具注入自有配置
    prepare_env: Optional[Callable[..., dict]] = None
    # (cmd, session_id, reused) -> (cmd, structured_json);
    # 各 CLI 的 create/resume 参数语法
    session_args: Optional[Callable[..., tuple[list[str], bool]]] = None
    # (backend, timeout) -> RuntimeUsageSnapshot;账户限额探测
    account_usage_probe: Optional[Callable] = None
    # () -> 工具自有目录清单(配置/Skill/记忆/会话等,如 ~/.codex);平台把
    # 其中存在的目录并入授权清单与沙箱可写根,Agent 才能使用工具自带能力
    private_dirs: Optional[Callable[[], list[str]]] = None
