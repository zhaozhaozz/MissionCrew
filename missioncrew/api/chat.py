"""聊天端点:频道管理与消息收发。"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, HTTPException

from ..collab.workspace import write_page_context_snapshot
from ..core.models import Channel
from .context import ApiContext
from .schemas import (ChannelCreate, MessageInput, PageContextInput,
                      RuntimeInteractionInput)


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

    def mutable_channel(channel_id: str) -> Channel:
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        if channel.is_general:
            raise HTTPException(409, "general 是项目默认频道，不能归档或删除")
        if store.active_chat_runs(channel_id):
            raise HTTPException(409, "频道仍有 Agent 正在运行，请等待本轮结束")
        return channel

    @app.post("/api/chat/channels/{channel_id}/archive")
    def archive_channel(channel_id: str):
        channel = mutable_channel(channel_id)
        if not channel.archived:
            stopped = chat.stop_channel_sessions(channel_id)
            channel.archived = True
            channel.archived_at = time.time()
            store.put_channel(channel)
            store.audit("human", "channel_archived",
                        detail=f"channel={channel_id} stopped_runtimes={stopped}")
        else:
            stopped = 0
        return {**channel.to_dict(), "stopped_runtimes": stopped}

    @app.post("/api/chat/channels/{channel_id}/restore")
    def restore_channel(channel_id: str):
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        if channel.archived:
            channel.archived = False
            channel.archived_at = 0.0
            store.put_channel(channel)
            store.audit("human", "channel_restored", detail=f"channel={channel_id}")
        return channel.to_dict()

    @app.get("/api/chat/{channel_id}/messages")
    def messages(channel_id: str, after_id: int = 0):
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        items = store.list_messages(channel_id, after_id)
        # 新消息持久化执行当时的组合；无法从旧执行事件迁移的历史消息才用
        # 当前角色配置兜底，避免页面继续只显示笼统的 "agent"。
        for item in items:
            if item["author_type"] != "agent" or item.get("runtime_id"):
                continue
            role = store.get_role(channel.project_id or "", item["author"])
            if role:
                item.update(runtime_id=role.runtime_id, model=role.model,
                            effort=role.effort)
        for item in items:
            try:
                item["mention_spans"] = json.loads(item.get("mention_spans") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["mention_spans"] = []
        return {
            "channel": channel.to_dict(),
            "messages": items,
            "active_runs": store.active_chat_runs(channel_id),
            # 最近执行记录(含已结束):前端按 events_size 变化拉取过程事件
            "runs": store.chat_runs_for_channel(channel_id),
        }

    @app.get("/api/chat/runs/{run_id}/events")
    def run_events(run_id: int):
        return {"events": store.run_events(run_id)}

    @app.post("/api/chat/runs/{run_id}/interactions/{request_id}")
    def respond_runtime_interaction(run_id: int, request_id: str,
                                    body: RuntimeInteractionInput):
        try:
            chat.respond_interaction(run_id, request_id, body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/chat/{channel_id}/messages")
    def post_message(channel_id: str, body: MessageInput):
        if store.get_channel(channel_id) is None:
            raise HTTPException(404, f"频道不存在: {channel_id}")
        try:
            msg_id = chat.post(
                channel_id, body.author, body.content,
                mention_spans=[item.model_dump() for item in body.mentions],
            )
        except ValueError as e:
            raise HTTPException(409 if "已归档" in str(e) else 400, str(e))
        return {"id": msg_id}

    @app.post("/api/chat/{channel_id}/clear-context")
    def clear_context(channel_id: str):
        try:
            return chat.clear_context(channel_id)
        except ValueError as exc:
            message = str(exc)
            raise HTTPException(
                409 if "正在运行" in message else 404, message) from exc

    @app.post("/api/chat/{channel_id}/page-context")
    def write_page_context(channel_id: str, body: PageContextInput):
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        if not channel.project_id:
            raise HTTPException(400, "频道未归属项目")
        project = ctx.must_project(channel.project_id)
        try:
            path = write_page_context_snapshot(
                project.id, channel.id, project.orchestrator_role_id,
                body.page_kind, body.page_key, body.content,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"path": str(path)}

    @app.delete("/api/chat/channels/{channel_id}")
    def delete_channel(channel_id: str):
        mutable_channel(channel_id)
        stopped = chat.stop_channel_sessions(channel_id)
        store.delete_channel(channel_id)
        store.audit("human", "channel_deleted",
                    detail=f"channel={channel_id} stopped_runtimes={stopped}")
        return {"ok": True, "stopped_runtimes": stopped}
