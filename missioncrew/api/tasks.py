"""结构化任务端点:创建、详情、推进与审批。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.resource_urls import task_resource_url
from .context import ApiContext
from .schemas import ApprovalInput, TaskCreate


def register(app: FastAPI, ctx: ApiContext) -> None:
    store, engine = ctx.store, ctx.engine

    @app.post("/api/tasks")
    def create_task(body: TaskCreate):
        try:
            t = engine.create_task(body.project_id, body.title, body.description,
                                   body.task_type, body.labels, body.risk,
                                   body.security_level, body.max_tier)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {**t.to_dict(), "resource_url": task_resource_url(t.project_id, t.id)}

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        t = store.get_task(task_id)
        if t is None:
            raise HTTPException(404, "任务不存在")
        return {
            "task": {**t.to_dict(),
                     "resource_url": task_resource_url(t.project_id, t.id)},
            "evidence": store.list_evidence(task_id),
            "runs": store.list_runs(task_id),
            "approvals": store.list_approvals(task_id),
            "audit": store.list_audit(task_id, 100),
        }

    @app.post("/api/tasks/{task_id}/advance")
    def advance(task_id: str):
        try:
            reports = engine.run(task_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        task = reports[-1].task if reports else store.get_task(task_id)
        return {"messages": [r.message for r in reports],
                "task": ({**task.to_dict(),
                          "resource_url": task_resource_url(task.project_id, task.id)}
                         if task else None)}

    @app.post("/api/tasks/{task_id}/approve")
    def approve(task_id: str, body: ApprovalInput):
        try:
            engine.approve(task_id, body.approver, body.decision, body.note, body.stage)
            reports = engine.run(task_id) if body.decision == "approved" else []
        except ValueError as e:
            raise HTTPException(400, str(e))
        t = store.get_task(task_id)
        return {"messages": [r.message for r in reports],
                "task": ({**t.to_dict(),
                          "resource_url": task_resource_url(t.project_id, t.id)}
                         if t else None)}
