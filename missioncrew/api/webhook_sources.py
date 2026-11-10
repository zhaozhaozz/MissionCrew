"""全局 webhook 来源端点:来源维护、订阅脚本一览、密钥读取/轮换与 GitHub 轮询状态。

来源是平台级资源(不属于某个项目),只由人在全局设置页维护;Agent 只能在
automation.save 里引用已有来源。密钥只经 /secret 端点给人看,列表里不带。
GitHub 轮询来源(kind=github)没有投递地址与密钥可配,列表里带它的轮询状态。
每个来源收到的事件留有投递记录(/deliveries),人在页面上查,Agent 用
webhook.history 查。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.github_events import poll_status
from ..collab.webhooks import (delete_webhook_source, public_delivery,
                               rotate_webhook_source_secret, save_webhook_source,
                               source_subscribers, webhook_path)
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
        # 轮询来源没有投递地址:投递由平台查询 GitHub 生成,改为带上轮询状态
        data["webhook_path"] = webhook_path(source) if source.kind == "webhook" else ""
        if source.kind == "github":
            data["poll"] = poll_status(store, source)
        data["delivery_count"] = store.count_webhook_deliveries(source.id)
        data["automations"] = [
            {"id": item.id, "project_id": item.project_id, "name": item.name,
             # GitHub 轮询来源:各脚本订阅的仓库与事件,来源轮询的是它们的并集
             **({"github_repos": item.github_repos, "github_events": item.github_events}
                if source.kind == "github" else {})}
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
                path=body.path, enabled=body.enabled, kind=body.kind,
                github=body.github)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if source.kind == "github":
            ctx.github_poller.poke()   # 新来源或改过的配置马上建立基线,不等下一个 tick
        return _source_data(source)

    @app.post("/api/webhook-sources/{source_id}/poll")
    def poll(source_id: str):
        """立即轮询一个 GitHub 来源(不等到点),返回带最新轮询状态的来源。"""
        source = _must_source(source_id)
        if source.kind != "github":
            raise HTTPException(400, "只有 GitHub 轮询来源可以立即检查")
        if not source.enabled:
            raise HTTPException(409, "来源已停用,启用后才会轮询")
        ctx.github_poller.poll(source.id)
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

    @app.get("/api/webhook-sources/{source_id}/deliveries")
    def deliveries(source_id: str, limit: int = 50, before_id: int = 0,
                   event: str = "", action: str = "", repository: str = ""):
        """来源的投递记录摘要,新的在前;before_id 向前翻页,其余参数过滤。"""
        source = _must_source(source_id)
        rows = store.list_webhook_deliveries(
            source.id, limit=limit, before_id=before_id, event=event.strip(),
            action=action.strip(), repository=repository.strip())
        return {"deliveries": [public_delivery(row) for row in rows],
                "total": store.count_webhook_deliveries(source.id)}

    @app.get("/api/webhook-sources/{source_id}/deliveries/{delivery_id}")
    def delivery(source_id: str, delivery_id: int):
        """单条投递:摘要加事件全文(与脚本读到的事件文件同形)。"""
        source = _must_source(source_id)
        row = store.get_webhook_delivery(delivery_id)
        if row is None or row["source_id"] != source.id:
            raise HTTPException(404, "投递记录不存在")
        return {**public_delivery(row), "event_file": row["event_data"]}

    @app.get("/api/webhook-sources/{source_id}/secret")
    def secret(source_id: str):
        return {"secret": _must_source(source_id).secret}

    @app.post("/api/webhook-sources/{source_id}/secret")
    def rotate_secret(source_id: str):
        source = rotate_webhook_source_secret(store, _must_source(source_id))
        return {"secret": source.secret}
