"""聊天端点:频道管理与消息收发。"""
from __future__ import annotations

import json
import re
import time

from fastapi import FastAPI, HTTPException, Request

from ..collab import channels as channel_ops
from ..collab.callbacks import public_callback
from ..collab.content_channels import (CONTENT_KIND_LABELS,
                                       content_channel as find_content_channel,
                                       ensure_content_channel)
from ..collab.documents import normalize_document_resource_urls
from ..collab.recycle_bin import recycle_channel
from ..collab.resource_urls import channel_resource_url
from ..collab.workspace import write_page_context_snapshot
from ..core.config import projects_dir
from ..core.models import Channel
from .context import ApiContext, etag_json_response
from .schemas import (ChannelCreate, ChannelUpdate, ContentChannelInput, MessageInput, PageContextInput,
                      RuntimeInteractionInput)

LIST_SCOPES = {"active", "archived", "all"}
# 通知增量的最长补报窗口:页面挂起/休眠很久后回来,只补最近这段,不把积压一次弹出
COMPLETION_LOOKBACK_SECONDS = 3600
COMPLETION_TEXT_MAX = 160


def _notice_text(text: str) -> str:
    """把 Markdown 回复压成通知里放得下的一行纯文本。"""
    plain = " ".join(re.sub(r"`+|\*\*", "", text or "").split())
    return plain if len(plain) <= COMPLETION_TEXT_MAX else plain[:COMPLETION_TEXT_MAX] + "…"


def register(app: FastAPI, ctx: ApiContext) -> None:
    store, chat = ctx.store, ctx.chat

    @app.get("/api/chat/channels")
    def channels():
        return [{**c.to_dict(), **(
            {"resource_url": channel_resource_url(c.project_id, c.id)}
            if c.project_id else {})}
            for c in store.list_channels()]

    def channel_data(channel: Channel) -> dict:
        return {**channel.to_dict(),
                "resource_url": channel_resource_url(channel.project_id, channel.id)}

    @app.get("/api/projects/{project_id}/channels")
    def project_channels(project_id: str, request: Request,
                         scope: str = "active"):
        """按项目 + 归档态取频道列表;counts 固定按全量统计,供筛选菜单显示。"""
        ctx.must_project(project_id)
        if scope not in LIST_SCOPES:
            raise HTTPException(400, "scope 只能是 active/archived/all")
        channels = store.list_channels(project_id)
        counts = {"all": len(channels),
                  "archived": sum(1 for c in channels if c.archived)}
        counts["active"] = counts["all"] - counts["archived"]
        if scope != "all":
            channels = [c for c in channels
                        if c.archived == (scope == "archived")]
        active_run_counts = store.active_chat_run_counts()
        callback_counts = store.pending_chat_callback_counts()
        return etag_json_response(request, {
            "channels": [{**channel_data(c),
                          "active_run_count": active_run_counts.get(c.id, 0),
                          "pending_callback_count": callback_counts.get(c.id, 0)}
                         for c in channels],
            "counts": counts,
        })

    @app.post("/api/chat/channels")
    def create_channel(body: ChannelCreate):
        if not body.project_id or store.get_project(body.project_id) is None:
            raise HTTPException(400, "频道必须归属一个已存在的项目")
        project = ctx.must_project(body.project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        # 频道 id 以项目为命名空间,避免多项目下同名冲突;不给 id 时由名称推导
        cid = ctx.namespaced_id(
            body.project_id, body.id or channel_ops.channel_short_id(body.name), "频道")
        if store.get_channel(cid):
            raise HTTPException(400, "频道已存在")
        c = Channel(id=cid, name=body.name or body.id,
                    project_id=body.project_id, workdir=body.workdir,
                    purpose=body.purpose, created_by_role_id=body.actor_role_id or "")
        store.put_channel(c)
        store.audit(actor, "channel_created", detail=f"project={body.project_id} channel={cid}")
        return channel_data(c)

    @app.post("/api/projects/{project_id}/content-channel")
    def content_channel(project_id: str, body: ContentChannelInput):
        project = ctx.must_project(project_id)
        try:
            channel, created = ensure_content_channel(
                store, project, body.content_kind, body.content_key, body.label)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if created:
            store.audit(
                "platform", "content_channel_created",
                detail=(f"project={project_id} kind={body.content_kind} "
                        f"key={body.content_key} channel={channel.id}"),
            )
        return {**channel_data(channel), "created": created}

    @app.get("/api/projects/{project_id}/content-channel")
    def get_content_channel(project_id: str, content_kind: str, content_key: str):
        """只查找已有绑定；打开内容页时不得隐式创建频道。"""
        ctx.must_project(project_id)
        key = content_key.strip()
        if content_kind not in CONTENT_KIND_LABELS:
            raise HTTPException(400, f"不支持的内容类型：{content_kind}")
        if not key:
            raise HTTPException(400, "内容键不能为空")
        channel = find_content_channel(store, project_id, content_kind, key)
        if channel is None:
            raise HTTPException(404, "该内容尚未发起对话")
        return {**channel_data(channel), "created": False}

    def must_channel(channel_id: str) -> Channel:
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        return channel

    def mutable_channel(channel_id: str) -> Channel:
        channel = must_channel(channel_id)
        try:
            channel_ops.ensure_channel_archivable(store, channel)
        except channel_ops.ChannelChangeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return channel

    @app.patch("/api/chat/channels/{channel_id}")
    def patch_channel(channel_id: str, body: ChannelUpdate):
        """修改名称、用途或工作目录;工作目录从下一轮执行起生效。"""
        channel = must_channel(channel_id)
        fields = body.model_dump(exclude_unset=True)
        try:
            channel_ops.update_channel(store, channel, actor="human", **fields)
        except channel_ops.ChannelChangeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return channel_data(channel)

    @app.post("/api/chat/channels/{channel_id}/archive")
    def archive_channel(channel_id: str):
        channel = must_channel(channel_id)
        try:
            stopped = channel_ops.archive_channel(
                store, channel, actor="human",
                stop_sessions=chat.stop_channel_sessions)
        except channel_ops.ChannelChangeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**channel_data(channel), "stopped_runtimes": stopped}

    @app.post("/api/chat/channels/{channel_id}/restore")
    def restore_channel(channel_id: str):
        channel = must_channel(channel_id)
        channel_ops.restore_channel(store, channel, actor="human")
        return channel_data(channel)

    @app.get("/api/chat/{channel_id}/messages")
    def messages(channel_id: str, after_id: int = 0, before_id: int = 0,
                 tail: bool = False):
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        # tail 服务首屏(直接定位频道末尾一页),before_id 服务向上翻页;
        # 两者都取窗口内最新 200 条,否则保持按 after_id 增量拉取
        paging = tail or before_id > 0
        items = (store.recent_messages(channel_id, 200, before_id=before_id)
                 if paging else store.list_messages(channel_id, after_id))
        project = store.get_project(channel.project_id or "")
        document_root = projects_dir() / project.id / "documents" if project else None
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
            try:
                item["context"] = json.loads(item.get("context") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["context"] = {}
            if project and document_root:
                original = str(item.get("content", ""))
                normalized = normalize_document_resource_urls(
                    original, project.id, [document_root])
                if normalized != original:
                    for span in item["mention_spans"]:
                        start, end = span.get("start"), span.get("end")
                        if type(start) is not int or type(end) is not int:
                            continue
                        span["start"] = len(normalize_document_resource_urls(
                            original[:start], project.id, [document_root]))
                        span["end"] = len(normalize_document_resource_urls(
                            original[:end], project.id, [document_root]))
                    item["content"] = normalized
        payload = {
            "channel": channel_data(channel),
            "messages": items,
            "active_runs": store.active_chat_runs(channel_id),
            # 本频道角色登记、仍在等待的回调:频道头显示并可取消
            "callbacks": [public_callback(row) for row in
                          store.list_chat_callbacks(channel=channel_id, status="pending")],
            # 最近执行记录(含已结束):前端按 events_size 变化拉取过程事件
            "runs": store.chat_runs_for_channel(channel_id),
        }
        if paging:
            # 告知前端窗口之前是否还有更早历史,决定是否继续向上翻页
            earliest = items[0]["id"] if items else before_id
            payload["has_earlier"] = bool(
                earliest and store.recent_messages(channel_id, 1, before_id=earliest))
        return payload

    @app.get("/api/chat/runs/completions")
    def run_completions(since: float | None = None):
        """浏览器"任务结束"通知的增量源:``(since, now]`` 内整条结束的协作链。

        游标一律是服务端时间(响应的 ``now``),不受浏览器时钟偏差影响;首次调用
        不带 since 只领游标,不把历史运行当成新通知。项目开关是浏览器本地偏好,
        由前端过滤。
        """
        now = time.time()
        if since is None:
            return {"now": now, "completions": []}
        completions = []
        for chain in store.finished_chat_chains(
                max(since, now - COMPLETION_LOOKBACK_SECONDS), now):
            channel = store.get_channel(chain["channel"])
            project = store.get_project(channel.project_id or "") if channel else None
            if project is None:
                continue
            reply = store.last_chain_reply(channel.id, chain["root_id"])
            completions.append({
                **chain,
                "error": _notice_text(chain["error"]),
                "project_id": project.id, "project_name": project.name or project.id,
                "channel_name": channel.name or channel.id,
                "reply": ({"author": reply["author"],
                           "content": _notice_text(reply["content"])}
                          if reply else None),
                "resource_url": channel_resource_url(project.id, channel.id),
            })
        return {"now": now, "completions": completions}

    @app.get("/api/chat/runs/{run_id}/events")
    def run_events(run_id: int):
        # live_output 是不受事件窗口限制的完整 text 输出,供实时输出框使用
        return {"events": store.run_events(run_id),
                "live_output": store.run_live_output(run_id)}

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
                context=body.context,
            )
        except ValueError as e:
            conflict = "已归档" in str(e) or "正在停止" in str(e)
            raise HTTPException(409 if conflict else 400, str(e))
        channel = store.get_channel(channel_id)
        return {"id": msg_id,
                "resource_url": channel_resource_url(channel.project_id, channel.id)}

    @app.post("/api/chat/{channel_id}/clear-context")
    def clear_context(channel_id: str):
        try:
            return chat.clear_context(channel_id)
        except ValueError as exc:
            message = str(exc)
            raise HTTPException(
                409 if "正在运行" in message else 404, message) from exc

    @app.post("/api/chat/{channel_id}/stop")
    def stop_channel_agents(channel_id: str):
        try:
            return chat.stop_channel_agents(channel_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/chat/runs/{run_id}/stop")
    def stop_chat_run(run_id: int):
        try:
            return chat.stop_run(run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/chat/{channel_id}/page-context")
    def write_page_context(channel_id: str, body: PageContextInput):
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, "频道不存在")
        if not channel.project_id:
            raise HTTPException(400, "频道未归属项目")
        project = ctx.must_project(channel.project_id)
        # 快照落在频道级目录,装配时授权给频道内所有角色;不绑定接收者
        try:
            path = write_page_context_snapshot(
                project.id, channel.id,
                body.page_kind, body.page_key, body.content,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"path": str(path)}

    @app.post("/api/chat/callbacks/{callback_id}/cancel")
    def cancel_callback(callback_id: int):
        row = store.get_chat_callback(callback_id)
        if row is None:
            raise HTTPException(404, "回调不存在")
        if not chat.callbacks.cancel(callback_id, "human", "用户在页面取消"):
            raise HTTPException(409, f"回调 #{callback_id} 已结束")
        return {"ok": True, "callback": public_callback(store.get_chat_callback(callback_id))}

    @app.delete("/api/chat/channels/{channel_id}")
    def delete_channel(channel_id: str):
        channel = mutable_channel(channel_id)
        if channel.content_kind:
            stopped = chat.stop_channel_sessions(channel_id)
            conversation = ctx.purge_content_channel(
                channel, actor="human", reason="channel_deleted",
                stopped_runtimes=stopped)
            return {
                "ok": True, "permanent": True,
                "stopped_runtimes": stopped,
                "conversation": conversation,
            }
        project = ctx.must_project(channel.project_id or "")
        stopped = chat.stop_channel_sessions(channel_id)
        item = recycle_channel(store, project, channel, actor="human")
        return {"ok": True, "stopped_runtimes": stopped,
                "recycle_item": item}
