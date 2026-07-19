"""API 共享上下文:存储、引擎实例与跨端点的校验助手。"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException

from ..collab.chat import ChatEngine
from ..core import seed as seed_mod
from ..core.config import db_path
from ..core.models import Project
from ..core.store import Store
from ..runtime import adapters
from ..taskflow.engine import Engine

MENTION_ID_RE = re.compile(r"[\w-]+")


@dataclass
class ApiContext:
    store: Store
    engine: Engine
    chat: ChatEngine
    # 更新互斥与"更新中"标记:与 ChatEngine 共享同一集合,更新期间不派发该后端
    updating_backends: set[str] = field(default_factory=set)
    updating_guard: threading.Lock = field(default_factory=threading.Lock)
    # runtime 模型目录缓存:发现可能要起进程(codex/opencode/ACP),10 分钟内复用
    model_catalog_cache: dict[str, tuple[float, list[str]]] = field(default_factory=dict)
    model_catalog_guard: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def build(cls) -> "ApiContext":
        store = Store(db_path())
        seed_mod.ensure_role_bindings(store)
        seed_mod.migrate_project_fields(store)
        ctx = cls(store=store, engine=Engine(store), chat=ChatEngine(store))
        ctx.chat.updating_backends = ctx.updating_backends
        return ctx

    def discovered_models(self, backend, refresh: bool = False) -> list[str]:
        with self.model_catalog_guard:
            cached = self.model_catalog_cache.get(backend.id)
            if cached and not refresh and time.time() - cached[0] < 600:
                return cached[1]
        models = adapters.list_runtime_models(backend)
        with self.model_catalog_guard:
            self.model_catalog_cache[backend.id] = (time.time(), models)
        return models

    def must_project(self, project_id: str) -> Project:
        project = self.store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        return project

    def validate_orchestrator_actor(self, project: Project,
                                    actor_role_id: Optional[str]) -> str:
        """人类请求无需角色身份;以角色身份调用时只允许项目主控。"""
        if actor_role_id and actor_role_id != project.orchestrator_role_id:
            raise HTTPException(403, f"只有项目主控 @{project.orchestrator_role_id} 可以执行此操作")
        return actor_role_id or "human"

    def namespaced_id(self, project_id: str, raw_id: str, kind: str) -> str:
        short_id = raw_id.removeprefix(f"{project_id}:")
        if not short_id or not MENTION_ID_RE.fullmatch(short_id):
            raise HTTPException(400, f"{kind} id 只能包含字母、数字、下划线、连字符")
        return f"{project_id}:{short_id}"
