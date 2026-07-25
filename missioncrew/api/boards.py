"""自定义面板端点:面板 CRUD 与卡片数据源实时解析。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException

from ..collab.documents import library_for
from ..collab.recycle_bin import recycle_dashboard
from ..collab.resource_urls import dashboard_resource_url
from ..core.models import BOARD_WIDGET_TYPES, Board, BoardWidget
from .context import MENTION_ID_RE, ApiContext
from .schemas import BoardInput, WidgetDataInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/projects/{project_id}/boards")
    def list_boards(project_id: str):
        ctx.must_project(project_id)
        return [{**board.to_dict(),
                 "resource_url": dashboard_resource_url(project_id, board.id)}
                for board in store.list_boards(project_id)]

    def _resolve_widget_source(project_id: str, source: dict):
        """解析卡片数据源:卡片是通用展示原语,领域数据从平台实时取。"""
        kind = source.get("from")
        if kind == "tasks":
            want_status = source.get("status") or []
            want_labels = source.get("labels") or []
            want_channels = source.get("channel_ids") or []
            rows = []
            for t in store.list_tasks():
                if t.project_id != project_id:
                    continue
                if t.archived and not source.get("include_archived", False):
                    continue
                if want_status and t.status not in want_status:
                    continue
                if want_channels and not set(t.channel_ids) & set(want_channels):
                    continue
                if want_labels and not set(t.labels) & set(want_labels):
                    continue
                rows.append({"id": t.id, "标题": t.title, "状态": t.status,
                             "简介": t.summary,
                             "频道": ", ".join(t.channel_ids),
                             "标签": ", ".join(t.labels)})
            return {"rows": rows}
        if kind == "audit":
            actions = source.get("actions") or []
            limit = min(int(source.get("limit", 30)), 200)
            rows = []
            for a in store.list_audit(limit=200):
                if actions and a["action"] not in actions:
                    continue
                if project_id not in (a.get("detail") or "") and a.get("task_id", "") == "":
                    # 审计明细里带项目标记的才算本项目(任务审计经 task_id 关联)
                    if f"project={project_id}" not in (a.get("detail") or ""):
                        continue
                rows.append({"text": f"{a['actor']} {a['action']} {a['detail']}"[:200]})
                if len(rows) >= limit:
                    break
            return {"items": rows}
        if kind == "document":
            path = str(source.get("path", ""))
            try:
                text = library_for(project_id).read(path)
            except (ValueError, FileNotFoundError, UnicodeDecodeError) as exc:
                return {"error": f"文档不可读: {exc}"}
            return {"markdown": text, "text": text}
        if kind == "messages":
            raw = str(source.get("channel", ""))
            cid = raw if raw.startswith(f"{project_id}:") else f"{project_id}:{raw}"
            channel = store.get_channel(cid) or store.get_channel(raw)
            if channel is None or channel.project_id != project_id:
                return {"error": f"频道不存在: {raw}"}
            limit = min(int(source.get("limit", 20)), 100)
            return {"items": [
                {"text": f"[{m['author']}] {m['content'][:160]}"}
                for m in store.recent_messages(channel.id, limit)]}
        return {"error": f"未知数据源: {kind}"}

    @app.post("/api/projects/{project_id}/widget_data")
    def widget_data(project_id: str, body: WidgetDataInput):
        """批量解析面板卡片的数据源(保存的面板与编辑预览共用)。"""
        ctx.must_project(project_id)
        resolved = {}
        for w in body.widgets:
            source = ((w.get("content") or {}).get("source")) or None
            if isinstance(source, dict) and w.get("id"):
                resolved[w["id"]] = _resolve_widget_source(project_id, source)
        return resolved

    @app.post("/api/projects/{project_id}/boards")
    def save_board(project_id: str, body: BoardInput):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        board_id = ctx.namespaced_id(project_id, body.id, "面板")
        widgets = None
        if body.layout is not None:
            widgets = []
            seen = set()
            try:
                for raw in body.layout:
                    widget = BoardWidget(**raw)
                    if not MENTION_ID_RE.fullmatch(widget.id) or widget.id in seen:
                        raise ValueError("组件 id 必须合法且不能重复")
                    if widget.x < 0 or widget.y < 0 or not 1 <= widget.width <= 12 \
                            or not 1 <= widget.height <= 100:
                        raise ValueError("组件位置必须非负，宽度为 1..12，高度为 1..100")
                    if widget.type not in BOARD_WIDGET_TYPES:
                        raise ValueError(f"未知组件类型 {widget.type},"
                                         f"可用: {', '.join(sorted(BOARD_WIDGET_TYPES))}")
                    seen.add(widget.id)
                    widgets.append(widget)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"面板布局不合法: {exc}")
        board = store.get_board(board_id) or Board(
            id=board_id, project_id=project_id,
            created_by_role_id=body.actor_role_id or "",
        )
        if board.project_id != project_id:
            raise HTTPException(400, "面板不属于当前项目")
        board.name = body.name or board.name or body.id
        if body.description is not None:   # 缺省保留,避免只改名时清空
            board.description = body.description
        if widgets is not None:
            board.layout = widgets
        store.put_board(board)
        store.audit(actor, "board_saved", detail=f"project={project_id} board={board_id}")
        return {**board.to_dict(),
                "resource_url": dashboard_resource_url(project_id, board.id)}

    @app.delete("/api/projects/{project_id}/boards/{board_id}")
    def delete_board(project_id: str, board_id: str,
                     actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        full_id = ctx.namespaced_id(project_id, board_id, "面板")
        board = store.get_board(full_id)
        if board is None or board.project_id != project_id:
            raise HTTPException(404, "面板不存在")
        item = recycle_dashboard(store, project, full_id, actor=actor)
        return {"ok": True, "recycle_item": item}
