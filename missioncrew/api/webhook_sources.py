"""全局 webhook 来源端点:来源维护、订阅脚本一览与密钥读取/轮换。

来源是平台级资源(不属于某个项目),只由人在全局设置页维护;Agent 只能在
automation.save 里引用已有来源。密钥只经 /secret 端点给人看,列表里不带。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.webhooks import (delete_webhook_source, rotate_webhook_source_secret,
                               save_webhook_source, source_subscribers, webhook_path)
from ..core.models import WebhookSource
from .context import ApiContext
from .schemas import WebhookSourceInput
from .webhooks import webhook_listener_status


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    def _must_source(source_id: str) -> WebhookSource:
        source = store.get_webhook_source(source_id)
        if source is None:
            raise HTTPException(404, "webhook 来源不存在")
        return source

    def _source_data(source: WebhookSource) -> dict:
        data = source.public_dict()
        data["webhook_path"] = webhook_path(source)
        data["automations"] = [
            {"id": item.id, "project_id": item.project_id, "name": item.name}
            for item in source_subscribers(store, source.id)]
        return data

    @app.get("/api/webhook-sources")
    def list_sources():
        return {"sources": [_source_data(item) for item in store.list_webhook_sources()],
                "listener": webhook_listener_status(ctx)}

    @app.post("/api/webhook-sources")
    def save(body: WebhookSourceInput):
        try:
            source, _created = save_webhook_source(
                store, id=body.id, name=body.name, description=body.description,
                path=body.path, enabled=body.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _source_data(source)

    @app.delete("/api/webhook-sources/{source_id}")
    def delete(source_id: str):
        try:
            delete_webhook_source(store, source_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"deleted": True}

    @app.get("/api/webhook-sources/{source_id}/secret")
    def secret(source_id: str):
        return {"secret": _must_source(source_id).secret}

    @app.post("/api/webhook-sources/{source_id}/secret")
    def rotate_secret(source_id: str):
        source = rotate_webhook_source_secret(store, _must_source(source_id))
        return {"secret": source.secret}
