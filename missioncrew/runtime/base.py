"""Runtime provider 的稳定公共契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from ..core.models import Backend, ExecutionConfig, RunResult


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
    model: str = ""
    executable: str = ""
    started_at: float = 0.0
    last_activity: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class RuntimeProvider(ABC):
    """Runtime provider 契约；后端原生协议只在此边界之后可见。"""

    @abstractmethod
    def start(self, config: ExecutionConfig) -> RunResult:
        """启动或复用会话，完成一轮执行并返回标准结果。"""

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
