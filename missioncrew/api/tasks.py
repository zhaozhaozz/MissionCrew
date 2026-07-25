"""Issue 化 Task 端点：编辑、状态简报与 Channel 派发。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.recycle_bin import recycle_task
from ..collab.resource_urls import channel_resource_url, task_resource_url
from ..collab.tasks import (TaskDispatchError, add_task_brief, archive_task,
                            create_task, dispatch_task, restore_task,
                            update_task)
from .context import ApiContext
from .schemas import TaskBriefInput, TaskCreate, TaskProcessInput, TaskUpdate


def _task_data(store, task) -> dict:
    channels = []
    for channel_id in task.channel_ids:
        channel = store.get_channel(channel_id)
        if channel is not None:
            channels.append({
                **channel.to_dict(),
                "resource_url": channel_resource_url(channel.project_id, channel.id),
            })
    return {
        "task": {**task.to_dict(),
                 "resource_url": task_resource_url(task.project_id, task.id)},
        "channels": channels,
        "briefs": store.list_task_briefs(task.id),
        "audit": store.list_audit(task.id, 100),
    }


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.post("/api/tasks")
    def create(body: TaskCreate):
        try:
            task = create_task(
                store, body.project_id, title=body.title, summary=body.summary,
                body=body.body, labels=body.labels, channel_ids=body.channel_ids,
                status=body.status,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {**task.to_dict(),
                "resource_url": task_resource_url(task.project_id, task.id)}

    @app.get("/api/tasks/{task_id}")
    def detail(task_id: str):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return _task_data(store, task)

    @app.patch("/api/tasks/{task_id}")
    def edit(task_id: str, body: TaskUpdate):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        changes = body.model_dump(exclude_none=True, exclude={"snapshot_updated_at"})
        try:
            update_task(
                store, task, snapshot_updated_at=body.snapshot_updated_at,
                changes=changes,
            )
        except ValueError as exc:
            status = 409 if any(
                text in str(exc) for text in ("重新读取", "已归档")) else 400
            raise HTTPException(status, str(exc)) from exc
        return _task_data(store, task)

    @app.post("/api/tasks/{task_id}/archive")
    def archive(task_id: str):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        archive_task(store, task)
        return _task_data(store, task)

    @app.post("/api/tasks/{task_id}/restore")
    def restore(task_id: str):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        restore_task(store, task)
        return _task_data(store, task)

    @app.delete("/api/tasks/{task_id}")
    def delete(task_id: str):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        project = ctx.must_project(task.project_id)
        item = recycle_task(store, project, task, actor="human")
        return {"deleted": True, "recycle_item": item}

    @app.post("/api/tasks/{task_id}/briefs")
    def add_brief(task_id: str, body: TaskBriefInput):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        try:
            brief = add_task_brief(
                store, task, content=body.content, status=body.status)
        except ValueError as exc:
            raise HTTPException(
                409 if "已归档" in str(exc) else 400, str(exc)) from exc
        return {"brief": brief, **_task_data(store, task)}

    @app.post("/api/tasks/{task_id}/process")
    def process(task_id: str, body: TaskProcessInput):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        try:
            sent, brief = dispatch_task(
                store, ctx.chat, task, message=body.message)
        except TaskDispatchError as exc:
            raise HTTPException(409, f"Task 仅部分派发：{exc}") from exc
        except ValueError as exc:
            raise HTTPException(
                409 if "已归档" in str(exc) else 400, str(exc)) from exc
        return {"sent": sent, "brief": brief, **_task_data(store, task)}
