"""聊天端点:频道管理与消息收发。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..core.models import Channel
from .context import ApiContext
from .schemas import ChannelCreate, MessageInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store, chat = ctx.store, ctx.chat

    @app.get("/api/chat/channels")
    def channels():
        return [c.to_dict() for c in store.list_channels()]

    @app.post("/api/chat/channels")
    def create_channel(body: ChannelCreate):
        if not body.project_id or store.get_project(body.project_id) is None:
            raise HTTPException(400, "频道必须归属一个已存在的项目")
        project = ctx.must_project(body.project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        # 频道 id 以项目为命名空间,避免多项目下同名冲突
        cid = ctx.namespaced_id(body.project_id, body.id, "频道")
        if store.get_channel(cid):
            raise HTTPException(400, "频道已存在")
        c = Channel(id=cid, name=body.name or body.id,
                    project_id=body.project_id, workdir=body.workdir,
                    purpose=body.purpose, created_by_role_id=body.actor_role_id or "")
        store.put_channel(c)
        store.audit(actor, "channel_created", detail=f"project={body.project_id} channel={cid}")
        return c.to_dict()

    @app.get("/api/chat/{channel_id}/messages")
    def messages(channel_id: str, after_id: int = 0):
        if store.get_channel(channel_id) is None:
            raise HTTPException(404, "频道不存在")
        return {
            "messages": store.list_messages(channel_id, after_id),
            "active_runs": store.active_chat_runs(channel_id),
            # 最近执行记录(含已结束):前端按 events_size 变化拉取过程事件
            "runs": store.chat_runs_for_channel(channel_id),
        }

    @app.get("/api/chat/runs/{run_id}/events")
    def run_events(run_id: int):
        return {"events": store.run_events(run_id)}

    @app.post("/api/chat/{channel_id}/messages")
    def post_message(channel_id: str, body: MessageInput):
        try:
            msg_id = chat.post(channel_id, body.author, body.content)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"id": msg_id}

    @app.delete("/api/chat/channels/{channel_id}")
    def delete_channel(channel_id: str):
        if store.get_channel(channel_id) is None:
            raise HTTPException(404, "频道不存在")
        store.delete_channel(channel_id)
        store.audit("human", "channel_deleted", detail=f"channel={channel_id}")
        return {"ok": True}
