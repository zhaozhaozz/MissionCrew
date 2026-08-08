"""Runtime 统一抽象层。

业务层只通过 :class:`RuntimeManager` 启动/停止 Runtime、管理会话与
模型、注入 Skill 和访问策略。CLI/ACP/Mock 原始执行器只在本模块之后可见。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from . import adapters as _executors
from .base import (RuntimeCapabilities, RuntimeExecutionInfo, RuntimeInstance,
                   RuntimeProvider, RuntimeUsageSnapshot)
from ..core.models import Backend, ExecutionConfig, RunResult


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
        is_acp = backend.adapter in _executors.ACP_SERVE_COMMANDS
        return RuntimeCapabilities(
            session_reuse=_executors.supports_native_session(backend),
            structured_events=is_acp,
            permission_control=is_acp,
            account_usage=backend.adapter in {"kimi", "grok_build"},
        )

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return _executors.list_runtime_models(backend, timeout=timeout)

    def instances(self, backend: Backend) -> list[RuntimeInstance]:
        instances = _executors.active_execution_instances(backend.id)
        if backend.adapter in _executors.ACP_SERVE_COMMANDS:
            instances.extend(_executors.acp.active_instances(backend.id))
        return [replace(instance, adapter=backend.adapter)
                for instance in instances]

    def execution_info(self, config: ExecutionConfig) -> RuntimeExecutionInfo:
        if config.backend.adapter in _executors.ACP_SERVE_COMMANDS:
            return RuntimeExecutionInfo(
                mode="persistent" if config.session_key else "one_shot",
                transport="acp-stdio",
            )
        return RuntimeExecutionInfo(mode="one_shot", transport="cli-command")

    def account_usage(self, backend: Backend,
                      timeout: int = 15) -> RuntimeUsageSnapshot:
        from .usage import probe_grok_usage, probe_kimi_usage
        if backend.adapter == "kimi":
            return probe_kimi_usage(backend, timeout)
        if backend.adapter == "grok_build":
            return probe_grok_usage(
                backend, [backend.binary_path or "grok"], timeout)
        return super().account_usage(backend, timeout)


class RuntimeManager:
    """主程序唯一可见的 Runtime 控制面。"""

    ACCOUNT_USAGE_CACHE_TTL = 60

    def __init__(self):
        self._providers: dict[str, RuntimeProvider] = {}
        self._usage_store = None
        self._account_usage_cache: dict[str, tuple[float, RuntimeUsageSnapshot]] = {}
        self._account_usage_guard = threading.Lock()
        self._usage_refresh_handler = None
        self._builtin = _BuiltinProvider()
        # 延迟导入避免 provider 与 manager 初始化互相依赖。业务层只会看到
        # RuntimeManager，Claude/Codex 原生协议类不会越过 runtime 包边界。
        from .claude import ClaudeRuntimeProvider
        from .codex import CodexRuntimeProvider
        from .pi import PiRuntimeProvider
        self._providers.update({
            "claude_code": ClaudeRuntimeProvider(self._builtin),
            "codex": CodexRuntimeProvider(self._builtin),
            "pi": PiRuntimeProvider(self._builtin),
        })

    def bind_usage_store(self, store) -> None:
        """绑定持久化记录器；Runtime 执行不因历史写入失败而失败。"""
        self._usage_store = store
        try:
            store.reconcile_runtime_usage()
        except Exception:
            pass

    def register(self, adapter: str, provider: RuntimeProvider) -> None:
        """注册/覆盖一个 adapter provider，供插件或测试扩展。"""
        if not adapter:
            raise ValueError("Runtime adapter 不能为空")
        self._providers[adapter] = provider

    def set_wake_handler(self, handler) -> None:
        """注册 Runtime 自唤醒回调(Claude 后台任务汇报 turn、ACP 客户端
        终端结束后的自发汇报)。

        业务层不感知具体协议:回调收到统一 payload(session_key/输出/
        触发任务),由 ChatEngine 落成频道内的新运行。"""
        from . import acp, claude
        acp.set_wake_handler(handler)
        claude.set_wake_handler(handler)

    def set_usage_refresh_handler(self, handler) -> None:
        """注册一次性用量刷新通知；每次 Runtime 执行结束后触发。"""
        self._usage_refresh_handler = handler

    def _notify_usage_refresh(self) -> None:
        handler = self._usage_refresh_handler
        if handler is None:
            return
        try:
            handler()
        except Exception:
            pass

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
        if prepared.cancellation_requested():
            return RunResult(False, "执行已停止")
        provider = self.provider_for(prepared.backend)
        execution = provider.execution_info(prepared)
        usage_store = self._usage_store
        usage_id = 0
        if usage_store is not None:
            try:
                usage_id = usage_store.start_runtime_usage(
                    backend_id=prepared.backend.id,
                    adapter=prepared.backend.adapter,
                    mode=execution.mode,
                    transport=execution.transport,
                    task_id=prepared.task_id,
                    stage_name=prepared.stage_name,
                    project_id=prepared.project_id,
                    role_id=prepared.role_id,
                    session_key=prepared.session_key,
                    model=prepared.backend.model,
                    effort=prepared.effort,
                    workdir=prepared.workdir,
                )
            except Exception:
                usage_id = 0
        try:
            result = provider.start(prepared)
        except Exception as exc:
            if usage_id:
                try:
                    usage_store.finish_runtime_usage(
                        usage_id, False, str(exc),
                        interrupted=prepared.cancellation_requested())
                except Exception:
                    pass
            self._notify_usage_refresh()
            raise
        if usage_id:
            try:
                usage_store.finish_runtime_usage(
                    usage_id, result.success, result.summary,
                    interrupted=prepared.cancellation_requested())
            except Exception:
                pass
        self._notify_usage_refresh()
        return result

    def stop(self, backend: Backend, session_key: str = "") -> int:
        return self.provider_for(backend).stop(backend, session_key)

    def interrupt(self, backend: Backend, session_key: str = "") -> int:
        return self.provider_for(backend).interrupt(backend, session_key)

    def shutdown(self) -> None:
        """停止所有内置 Runtime 进程，供服务生命周期调用。"""
        seen: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            provider.shutdown()
        _executors.close_active_executions()
        _executors.acp.close_sessions()
        with self._account_usage_guard:
            self._account_usage_cache.clear()

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return self.provider_for(backend).capabilities(backend)

    def supports_session(self, backend: Backend) -> bool:
        return self.capabilities(backend).session_reuse

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return self.provider_for(backend).list_models(backend, timeout=timeout)

    def status(self, backends: list[Backend]) -> dict:
        """聚合所有 provider 的实时实例，不向 API 暴露原始执行器。"""
        all_instances: list[RuntimeInstance] = []
        backend_rows = []
        for backend in backends:
            instances = self.provider_for(backend).instances(backend)
            all_instances.extend(instances)
            running = sum(instance.state in {"starting", "running"}
                          for instance in instances)
            connected = sum(instance.state != "disconnected"
                            for instance in instances)
            persistent = sum(instance.mode == "persistent"
                             for instance in instances)
            one_shot = sum(instance.mode == "one_shot"
                           for instance in instances)
            state = ("running" if running else "idle" if connected else
                     "disabled" if not backend.enabled else "stopped")
            backend_rows.append({
                "id": backend.id, "name": backend.name,
                "adapter": backend.adapter, "enabled": backend.enabled,
                "state": state, "running": running,
                "instances": len(instances), "connected": connected,
                "persistent": persistent, "one_shot": one_shot,
                "projects": sorted({instance.project_id for instance in instances
                                    if instance.project_id}),
                "roles": sorted({instance.role_id for instance in instances
                                 if instance.role_id}),
            })
        state_order = {"running": 0, "starting": 1, "idle": 2,
                       "disconnected": 3}
        all_instances.sort(key=lambda instance: (
            state_order.get(instance.state, 9), instance.backend_id,
            0 if instance.mode == "persistent" else 1,
            instance.started_at,
        ))
        live = [instance for instance in all_instances
                if instance.state != "disconnected"]
        return {
            "generated_at": time.time(),
            "summary": {
                "backends": len(backends),
                "connected_backends": sum(row["connected"] > 0
                                          for row in backend_rows),
                "tracked_instances": len(all_instances),
                "live_instances": len(live),
                "running": sum(instance.state in {"starting", "running"}
                               for instance in all_instances),
                "persistent": sum(instance.mode == "persistent"
                                  for instance in live),
                "one_shot": sum(instance.mode == "one_shot"
                                for instance in live),
            },
            "backends": backend_rows,
            "instances": [instance.to_dict() for instance in all_instances],
        }

    def _read_account_usage(
            self, backend: Backend, refresh: bool,
            timeout: int) -> RuntimeUsageSnapshot:
        now = time.time()
        with self._account_usage_guard:
            cached = self._account_usage_cache.get(backend.id)
            if (cached and not refresh
                    and cached[1].adapter == backend.adapter
                    and now - cached[0] < self.ACCOUNT_USAGE_CACHE_TTL):
                return cached[1]
        if not backend.enabled:
            snapshot = RuntimeUsageSnapshot(
                backend_id=backend.id, backend_name=backend.name,
                adapter=backend.adapter, status="disabled", source="disabled",
                message="该 Runtime 已停用",
            )
        else:
            provider = self.provider_for(backend)
            try:
                snapshot = provider.account_usage(backend, timeout)
            except Exception:
                snapshot = RuntimeUsageSnapshot(
                    backend_id=backend.id, backend_name=backend.name,
                    adapter=backend.adapter, status="unavailable",
                    source="runtime_provider",
                    message="账户限额探测暂时不可用",
                )
        with self._account_usage_guard:
            self._account_usage_cache[backend.id] = (time.time(), snapshot)
        return snapshot

    def account_usage(
            self, backends: list[Backend], refresh: bool = False,
            timeout: int = 15) -> dict:
        """并行读取已支持 Runtime 的账户限额并返回统一快照。"""
        supported = [
            backend for backend in backends
            if self.capabilities(backend).account_usage
        ]
        if supported:
            with ThreadPoolExecutor(max_workers=min(4, len(supported))) as executor:
                snapshots = list(executor.map(
                    lambda backend: self._read_account_usage(
                        backend, refresh, timeout),
                    supported,
                ))
        else:
            snapshots = []
        status_counts: dict[str, int] = {}
        for snapshot in snapshots:
            status_counts[snapshot.status] = status_counts.get(snapshot.status, 0) + 1
        return {
            "generated_at": time.time(),
            "cache_ttl_seconds": self.ACCOUNT_USAGE_CACHE_TTL,
            "summary": {
                "supported": len(snapshots),
                "available": status_counts.get("ok", 0),
                "unavailable": len(snapshots) - status_counts.get("ok", 0),
            },
            "usage": [snapshot.to_dict() for snapshot in snapshots],
        }

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
