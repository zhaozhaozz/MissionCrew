"""项目统一回收站端点。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException

from ..collab.recycle_bin import (RecycleConflictError, empty_recycle_bin,
                                  list_recycle_items, purge_recycle_item,
                                  recycle_bin_url, restore_recycle_item)
from .context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/projects/{project_id}/recycle-bin")
    def list_items(project_id: str):
        ctx.must_project(project_id)
        return {
            "resource_url": recycle_bin_url(project_id),
            "items": list_recycle_items(project_id),
        }

    @app.post("/api/projects/{project_id}/recycle-bin/{item_id}/restore")
    def restore_item(project_id: str, item_id: str,
                     actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        try:
            return restore_recycle_item(store, project, item_id, actor=actor)
        except RecycleConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/projects/{project_id}/recycle-bin/{item_id}")
    def purge_item(project_id: str, item_id: str,
                   actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        try:
            return purge_recycle_item(store, project, item_id, actor=actor)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/projects/{project_id}/recycle-bin")
    def empty_bin(project_id: str, actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        return {"ok": True, "purged": empty_recycle_bin(store, project, actor=actor)}
