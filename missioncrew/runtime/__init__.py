"""Runtime 统一抽象层。业务代码只应使用 ``runtime_manager``。"""

from .base import RuntimeCapabilities, RuntimeProvider
from .manager import RuntimeManager, runtime_manager

__all__ = [
    "RuntimeCapabilities", "RuntimeManager", "RuntimeProvider", "runtime_manager",
]
