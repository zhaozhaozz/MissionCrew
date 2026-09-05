"""API 共享上下文:存储、引擎实例与跨端点的校验助手。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request, Response

from ..collab.automations import AutomationScheduler, AutomationService
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
from ..runtime.role_usage_linkage import RoleUsageLinkage

MENTION_ID_RE = re.compile(r"[\w-]+")


def etag_json_response(request: Request, payload) -> Response:
    """带 ETag + no-cache 的 JSON 响应:高频轮询数据未变化时命中 304,
    浏览器 fetch 透明读缓存,前端无需感知。"""
    body = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    etag = f'"{hashlib.sha1(body).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json",
                    headers=headers)


@dataclass
class ApiContext:
    store: Store
    chat: ChatEngine
    role_usage_linkage: RoleUsageLinkage | None = None
    automations: AutomationService | None = None
    automation_scheduler: AutomationScheduler | None = None
    # 更新互斥与"更新中"标记:与 ChatEngine 共享同一集合,更新期间不派发该后端
    updating_backends: set[str] = field(default_factory=set)
    updating_guard: threading.Lock = field(default_factory=threading.Lock)
    # runtime 模型目录缓存:发现可能要起进程(codex/opencode/ACP),10 分钟内复用。
    # 一条缓存同时存模型目录与按模型的推理力度档位——两者来自同一次探测。
    model_catalog_cache: dict[
        str, tuple[float, list[str], dict[str, list[str]]]] = field(default_factory=dict)
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
        # 平台托管安装(vendored pi)的可执行路径随数据目录走:启动时按当前
        # 数据目录重新定位,数据目录搬迁后不必手工改库
        relocated = []
        for backend in store.list_backends():
            if runtime_manager.relocate_managed_binary(backend):
                store.put_backend(backend)
                relocated.append(backend.id)
        if relocated:
            store.audit("platform", "managed_binary_relocated",
                        detail=f"backends={relocated}")
        ctx = cls(store=store, chat=ChatEngine(store))
        ctx.chat.updating_backends = ctx.updating_backends
        ctx.automations = AutomationService(store, ctx.chat.agent_tools)
        ctx.automation_scheduler = AutomationScheduler(store, ctx.automations)
        ctx.role_usage_linkage = RoleUsageLinkage(
            store,
            lambda refresh=True: runtime_manager.account_usage(
                store.list_backends(), refresh=refresh),
        )
        return ctx

    def discovered_catalog(self, backend, refresh: bool = False
                           ) -> tuple[list[str], dict[str, list[str]]]:
        """缓存后的(模型目录, 按模型推理力度);两者一次探测取回,一起过期。"""
        with self.model_catalog_guard:
            cached = self.model_catalog_cache.get(backend.id)
            if cached and not refresh and time.time() - cached[0] < 600:
                return cached[1], cached[2]
        models, efforts = runtime_manager.list_model_catalog(backend)
        with self.model_catalog_guard:
            self.model_catalog_cache[backend.id] = (time.time(), models, efforts)
        return models, efforts

    def discovered_models(self, backend, refresh: bool = False) -> list[str]:
        return self.discovered_catalog(backend, refresh=refresh)[0]

    def discovered_model_efforts(self, backend, refresh: bool = False
                                 ) -> dict[str, list[str]]:
        return self.discovered_catalog(backend, refresh=refresh)[1]

    def must_project(self, project_id: str) -> Project:
        project = self.store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        project, _ = sync_project_skill_library(self.store, project)
        return project

    def validate_orchestrator_actor(self, project: Project,
                                    actor_role_id: Optional[str]) -> str:
        """人类请求无需角色身份;以角色身份调用时只允许拥有主控级权限的角色
        (有主控时是主控,无主控时是本项目任一角色)。"""
        if actor_role_id and not project.controls_platform(actor_role_id):
            raise HTTPException(403, f"只有项目主控 @{project.orchestrator_role_id} 可以执行此操作")
        if actor_role_id and self.store.get_role(project.id, actor_role_id) is None:
            raise HTTPException(403, f"角色不属于本项目: @{actor_role_id}")
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
