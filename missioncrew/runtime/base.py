"""Runtime provider 的稳定公共契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Mapping, Optional

from ..core.models import Backend, ExecutionConfig, RunResult

# 服务进程的环境会整份铺给 Agent CLI,宿主(VS Code、外层 Agent 会话、SSH
# 登录)注入的变量会让子进程以为自己跑在那个宿主里:CLI 去连宿主的 IPC,
# git 去调宿主的 askpass 而在无头执行中挂住。这里按"变量来自宿主"剥掉,
# 不动 PATH、代理设置和平台自己注入的 MISSIONCREW_*——PATH 不是污染源,
# 服务同样要靠它探测本机装了哪些 CLI。
_HOST_ENV_PREFIXES = (
    "VSCODE_",        # IPC hook、askpass、nonce、debugpy 端点
    "CLAUDE_",        # 外层 Claude Code 会话的 session/socket
    "CURSOR_",
    "TERM_PROGRAM",   # 宿主终端身份,含 TERM_PROGRAM_VERSION
)
_HOST_ENV_NAMES = frozenset({
    "CLAUDECODE",     # 没有下划线,前缀匹配不到
    "GIT_ASKPASS",    # 值指向宿主 askpass 脚本,无头执行下会挂住
    "SSH_ASKPASS",
    "GIT_EDITOR",     # 宿主会改写成 true 或自己的 helper
    "PYTHONSTARTUP",  # 指向宿主注入的 pythonrc
    "SSH_AUTH_SOCK",  # 随终端失效,且等于把私钥代理交给 Agent
    "SSH_CLIENT",
    "SSH_CONNECTION",
    "NoDefaultCurrentDirectoryInExePath",
})


def host_isolated_environ(
        source: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """剥掉宿主注入的变量,得到派发 Agent 用的基础环境。

    放在派发这一层而不是启动脚本里:无论服务由 pm2、CLI 还是测试拉起,
    Agent 拿到的环境都一致,不依赖"必须用某个脚本启动"的约定。
    """
    items: Mapping[str, str] = os.environ if source is None else source
    return {name: value for name, value in items.items()
            if name not in _HOST_ENV_NAMES
            and not name.startswith(_HOST_ENV_PREFIXES)}


@dataclass(frozen=True)
class RuntimeCapabilities:
    """一个 Runtime 通过统一接口对外暴露的操作能力。"""

    start: bool = True
    stop: bool = True
    session_reuse: bool = False
    model_management: bool = True
    skill_injection: bool = True
    writable_paths: bool = True
    permissions: bool = True
    structured_events: bool = False
    user_interaction: bool = False
    permission_control: bool = False
    interrupt: bool = False
    account_usage: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeInstance:
    """一个当前由 MissionCrew 进程持有的 Runtime 实例快照。"""

    instance_id: str
    backend_id: str
    adapter: str
    mode: str                 # persistent | one_shot
    transport: str            # 原生双向协议、ACP stdio 或一次性 CLI
    state: str                # starting | running | idle | disconnected
    pid: int | None = None
    session_key: str = ""
    native_session_id: str = ""
    workdir: str = ""
    task_id: str = ""
    stage_name: str = ""
    project_id: str = ""
    role_id: str = ""
    model: str = ""
    executable: str = ""
    started_at: float = 0.0
    last_activity: float = 0.0
    # 实例内仍存活的后台命令数(run_in_background):>0 时回收/重启该实例
    # 会终止这些命令,状态页需要展示以供操作前判断
    background_tasks: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeExecutionInfo:
    """一次调用在历史记录中的稳定执行形态。"""

    mode: str = "one_shot"       # persistent | one_shot
    transport: str = "runtime"   # 原生协议、ACP stdio 或 CLI 命令


@dataclass(frozen=True)
class RuntimeUsageWindow:
    """一个可比较的账户限额时间窗口。"""

    key: str
    label: str
    used_percent: float
    resets_at: float | None = None
    duration_minutes: int | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["remaining_percent"] = round(max(0.0, 100.0 - self.used_percent), 2)
        return data


@dataclass(frozen=True)
class RuntimeUsageMetric:
    """限额卡片上的补充账户指标，不包含凭据或身份信息。"""

    label: str
    value: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeUsageSnapshot:
    """一个 Runtime 账户当前限额的统一、安全快照。"""

    backend_id: str
    backend_name: str
    adapter: str
    status: str
    source: str
    fetched_at: float = field(default_factory=time.time)
    plan: str = ""
    windows: tuple[RuntimeUsageWindow, ...] = ()
    metrics: tuple[RuntimeUsageMetric, ...] = ()
    message: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["windows"] = [window.to_dict() for window in self.windows]
        data["metrics"] = [metric.to_dict() for metric in self.metrics]
        return data


class RuntimeProvider(ABC):
    """Runtime provider 契约；后端原生协议只在此边界之后可见。"""

    @abstractmethod
    def start(self, config: ExecutionConfig) -> RunResult:
        """启动或复用会话；拿到后端会话锁后须再次检查取消状态。"""

    @abstractmethod
    def stop(self, backend: Backend, session_key: str = "") -> int:
        """停止匹配的活动执行或长驻会话，返回停止数量。"""

    @abstractmethod
    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        """返回当前 Backend 配置实际支持的统一能力。"""

    @abstractmethod
    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        """从 Runtime 查询模型目录。"""

    def shutdown(self) -> None:
        """释放 provider 持有的所有长驻进程；无状态 provider 无需实现。"""

    def interrupt(self, backend: Backend, session_key: str = "") -> int:
        """中断匹配会话的当前 turn，但保留可继续复用的会话。"""
        return 0

    def instances(self, backend: Backend) -> list[RuntimeInstance]:
        """返回当前活动或长驻实例；无状态 provider 默认没有实例。"""
        return []

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        """描述本次调用的形态，供统一使用历史记录；provider 可覆盖。"""
        return RuntimeExecutionInfo()

    def account_usage(self, backend: Backend,
                      timeout: int = 15) -> RuntimeUsageSnapshot:
        """读取 Runtime 账户限额；默认明确声明不支持。"""
        return RuntimeUsageSnapshot(
            backend_id=backend.id, backend_name=backend.name,
            adapter=backend.adapter, status="unsupported",
            source="unsupported", message="该 Runtime 暂不支持账户限额读取",
        )
