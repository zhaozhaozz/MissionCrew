"""总览与词表端点。"""
from __future__ import annotations

from fastapi import FastAPI

from ..core.models import BOARD_WIDGET_TYPES, ROLE_ABILITIES, TIER_ORDER
from ..runtime.adapters import EFFORT_SUPPORT
from .context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/overview")
    def overview():
        return {
            "projects": [p.to_dict() for p in store.list_projects()],
            "backends": [b.to_dict() for b in store.list_backends()],
            "tasks": [t.to_dict() for t in store.list_tasks()],
            "roles": [r.to_dict() for r in store.list_roles()],
            "channels": [c.to_dict() for c in store.list_channels()],
            "boards": [b.to_dict() for b in store.list_boards()],
        }

    @app.get("/api/traits")
    def traits():
        return {"abilities": ROLE_ABILITIES, "tiers": TIER_ORDER,
                "board_widget_types": sorted(BOARD_WIDGET_TYPES),
                "effort_options": EFFORT_SUPPORT}   # adapter -> 可选推理力度档位
