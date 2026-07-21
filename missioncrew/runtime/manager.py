"""Runtime 统一抽象层。

业务层只通过 :class:`RuntimeManager` 启动/停止 Runtime、管理会话与
模型、注入 Skill 和访问策略。CLI/ACP/Mock 原始执行器只在本模块之后可见。
"""
from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import adapters as _executors
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

    def to_dict(self) -> dict:
        return asdict(self)


class RuntimeProvider(ABC):
    """Runtime provider 契约；新后端只需实现这个边界。"""

    @abstractmethod
    def start(self, config: ExecutionConfig) -> RunResult:
        """启动一次执行并返回标准结果。"""

    @abstractmethod
    def stop(self, backend: Backend, session_key: str = "") -> int:
        """停止匹配的活动执行/长驻会话，返回停止数量。"""

    @abstractmethod
    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        """返回当前 Backend 配置实际支持的统一能力。"""

    @abstractmethod
    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        """从 Runtime 查询模型目录。"""


class _BuiltinProvider(RuntimeProvider):
    """把现有 CLI、ACP、Mock 执行器封装在统一 provider 后面。"""

    def start(self, config: ExecutionConfig) -> RunResult:
        return _executors.get_adapter(config.backend.adapter).run(config)

    def stop(self, backend: Backend, session_key: str = "") -> int:
        stopped = _executors.stop_active_executions(backend.id, session_key)
        if backend.adapter in _executors.ACP_SERVE_COMMANDS:
            stopped += _executors.acp.stop_runtime_sessions(backend.id, session_key)
        return stopped

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            session_reuse=_executors.supports_native_session(backend),
        )

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return _executors.list_runtime_models(backend, timeout=timeout)


class RuntimeManager:
    """主程序唯一可见的 Runtime 控制面。"""

    def __init__(self):
        self._providers: dict[str, RuntimeProvider] = {}
        self._builtin = _BuiltinProvider()

    def register(self, adapter: str, provider: RuntimeProvider) -> None:
        """注册/覆盖一个 adapter provider，供插件或测试扩展。"""
        if not adapter:
            raise ValueError("Runtime adapter 不能为空")
        self._providers[adapter] = provider

    def provider_for(self, backend: Backend) -> RuntimeProvider:
        return self._providers.get(backend.adapter, self._builtin)

    @staticmethod
    def _prepare(config: ExecutionConfig) -> ExecutionConfig:
        """把统一策略规范化为所有 provider 都能消费的环境和路径边界。"""
        policy = config.runtime_policy
        paths = [*policy.allowed_paths(), *policy.skill_paths]
        for skill_path in policy.skill_paths:
            if skill_path not in policy.readable_paths:
                policy.readable_paths.append(skill_path)
        paths = paths or list(config.allowed_dirs)
        config.allowed_dirs = list(dict.fromkeys(paths))
        config.env["MISSIONCREW_READABLE_DIRS"] = json.dumps(
            policy.readable_paths, ensure_ascii=False)
        config.env["MISSIONCREW_WRITABLE_DIRS"] = json.dumps(
            policy.writable_paths, ensure_ascii=False)
        config.env["MISSIONCREW_SKILL_DIRS"] = json.dumps(
            policy.skill_paths, ensure_ascii=False)
        config.env["MISSIONCREW_RUNTIME_PERMISSIONS"] = json.dumps(
            asdict(policy.permissions), ensure_ascii=False)
        if policy.skill_paths and not config.env.get("MISSIONCREW_SKILLS_DIR"):
            config.env["MISSIONCREW_SKILLS_DIR"] = policy.skill_paths[0]
        return config

    def start(self, config: ExecutionConfig) -> RunResult:
        """按统一策略启动 Runtime；调用方不接触任何原始执行器。"""
        prepared = self._prepare(config)
        return self.provider_for(prepared.backend).start(prepared)

    def stop(self, backend: Backend, session_key: str = "") -> int:
        return self.provider_for(backend).stop(backend, session_key)

    def shutdown(self) -> None:
        """停止所有内置 Runtime 进程，供服务生命周期调用。"""
        _executors.close_active_executions()
        _executors.acp.close_sessions()

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return self.provider_for(backend).capabilities(backend)

    def supports_session(self, backend: Backend) -> bool:
        return self.capabilities(backend).session_reuse

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return self.provider_for(backend).list_models(backend, timeout=timeout)

    def effort_options(self, backend: Backend) -> list[str]:
        return list(_executors.EFFORT_SUPPORT.get(backend.adapter, []))

    def effort_catalog(self) -> dict[str, list[str]]:
        return {adapter: list(options)
                for adapter, options in _executors.EFFORT_SUPPORT.items()}

    # ---- Runtime 发现、版本与更新也统一收口 ----
    def detect_report(self, with_version: bool = True) -> list[dict]:
        return _executors.detect_report(with_version=with_version)

    def detect_backends(self, report: Optional[list[dict]] = None) -> list[Backend]:
        return _executors.detect_backends(report)

    def can_check_updates(self, backend: Backend) -> bool:
        return backend.adapter in _executors.UPDATE_SPECS and bool(backend.binary_path)

    def update_plan(self, backend: Backend):
        return _executors.update_plan(backend)

    def fetch_latest_version(self, backend: Backend, timeout: int = 8) -> str:
        return _executors.fetch_latest_version(backend.adapter, timeout=timeout)

    def is_newer(self, latest: str, installed: str) -> bool:
        return _executors.is_newer(latest, installed)

    def update(self, backend: Backend, timeout: int = 600) -> tuple[bool, str]:
        return _executors.run_update(backend, timeout=timeout)

    def probe_version(self, binary: str) -> str:
        return _executors._cli_version(binary)

    def refresh_installation(self, backend: Backend) -> Backend:
        """重新解析 Runtime 可执行路径与版本，不向 API 泄漏探测细节。"""
        binary = Path(backend.binary_path).name if backend.binary_path else backend.adapter
        new_path = shutil.which(binary)
        if new_path:
            backend.binary_path = new_path
            backend.version = self.probe_version(binary)
        return backend


runtime_manager = RuntimeManager()
