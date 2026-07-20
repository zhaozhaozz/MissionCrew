"""总览与词表端点。"""
from __future__ import annotations

from fastapi import FastAPI

from ..collab.skills import sync_all_project_skill_libraries
from ..core.models import BOARD_WIDGET_TYPES, ROLE_ABILITIES, TIER_ORDER
from ..runtime.adapters import EFFORT_SUPPORT
from .context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/overview")
    def overview():
        # 用户可直接向项目 skills/ 投放目录；轮询总览时自动发现并同步。
        sync_all_project_skill_libraries(store)
        return {
            "projects": [p.to_dict() for p in store.list_projects()],
            "backends": [b.to_dict() for b in store.list_backends()],
            "tasks": [t.to_dict() for t in store.list_tasks()],
            "roles": [r.to_dict() for r in store.list_roles()],
            "role_templates": [r.to_dict() for r in store.list_role_templates()],
            "channels": [c.to_dict() for c in store.list_channels()],
            "boards": [b.to_dict() for b in store.list_boards()],
        }

    @app.get("/api/traits")
    def traits():
        return {"abilities": ROLE_ABILITIES, "tiers": TIER_ORDER,
                "board_widget_types": sorted(BOARD_WIDGET_TYPES),
                "effort_options": EFFORT_SUPPORT}   # adapter -> 可选推理力度档位
