"""API 共享上下文:存储、引擎实例与跨端点的校验助手。"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException

from ..collab.chat import ChatEngine
from ..collab.content_channels import content_channel
from ..collab.guidelines import sync_all_guideline_libraries
from ..collab.recycle_bin import migrate_legacy_skill_trash
from ..collab.skills import (sync_all_project_skill_libraries,
                             sync_project_skill_library)
from ..collab.workspace import (migrate_legacy_workspace_layout,
                                migrate_resource_workspace_links,
                                purge_channel_workspaces)
from ..core import seed as seed_mod
from ..core.config import db_path
from ..core.models import Channel, Project
from ..core.store import Store
from ..runtime import runtime_manager

MENTION_ID_RE = re.compile(r"[\w-]+")


@dataclass
class ApiContext:
    store: Store
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
        runtime_manager.bind_usage_store(store)
        seed_mod.ensure_role_bindings(store)
        seed_mod.ensure_role_templates(store)
        seed_mod.migrate_project_fields(store)
        sync_all_guideline_libraries(store)
        migrated_paths = migrate_legacy_workspace_layout()
        if migrated_paths:
            store.audit("platform", "agent_workspace_layout_migrated",
                        detail=f"paths={migrated_paths}")
        sync_all_project_skill_libraries(store)
        migrated_skills = migrate_legacy_skill_trash(store)
        if migrated_skills:
            store.audit("platform", "legacy_skill_trash_migrated",
                        detail=f"skills={migrated_skills}")
        resource_links = migrate_resource_workspace_links(store)
        if resource_links:
            store.audit(
                "platform", "resource_workspace_links_migrated",
                detail=f"paths={resource_links}")
        ctx = cls(store=store, chat=ChatEngine(store))
        ctx.chat.updating_backends = ctx.updating_backends
        return ctx

    def discovered_models(self, backend, refresh: bool = False) -> list[str]:
        with self.model_catalog_guard:
            cached = self.model_catalog_cache.get(backend.id)
            if cached and not refresh and time.time() - cached[0] < 600:
                return cached[1]
        models = runtime_manager.list_models(backend)
        with self.model_catalog_guard:
            self.model_catalog_cache[backend.id] = (time.time(), models)
        return models

    def must_project(self, project_id: str) -> Project:
        project = self.store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        project, _ = sync_project_skill_library(self.store, project)
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

    def prepare_content_channel_deletion(
            self, project_id: str, content_kind: str,
            content_key: str) -> tuple[Channel | None, int]:
        """删除内容或其频道前做运行态检查，并关闭空闲的持久 Runtime。"""
        channel = content_channel(
            self.store, project_id, content_kind, content_key)
        if channel is None:
            return None, 0
        if self.store.active_chat_runs(channel.id):
            raise HTTPException(
                409, "内容频道仍有 Agent 正在运行，请先停止或等待本轮结束")
        return channel, self.chat.stop_channel_sessions(channel.id)

    def purge_content_channel(
            self, channel: Channel | None, *, actor: str,
            reason: str, stopped_runtimes: int = 0) -> dict:
        """永久清空内容频道，使同一页面下次从全新会话开始。"""
        if channel is None:
            return {
                "deleted": False, "stopped_runtimes": stopped_runtimes,
                "workspaces": 0,
            }
        workspaces = purge_channel_workspaces(
            channel.project_id or "", channel.id)
        try:
            counts = self.store.purge_channel_conversation(channel.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        self.store.audit(
            actor, "content_channel_conversation_deleted",
            detail=(
                f"project={channel.project_id} channel={channel.id} "
                f"kind={channel.content_kind} key={channel.content_key} "
                f"reason={reason} stopped_runtimes={stopped_runtimes} "
                f"records={counts} workspaces={workspaces}"
            ),
        )
        return {
            "deleted": bool(counts["channels"]),
            "stopped_runtimes": stopped_runtimes,
            "workspaces": workspaces,
            "records": counts,
        }
