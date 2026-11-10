"""FastAPI 服务:统一任务入口的 Web 形态(聊天 + 看板 + REST API)。

纯本地运行:数据在本地 SQLite,执行是本地 Agent CLI 子进程,无任何云端依赖。
路由按资源域拆分模块,各模块提供 register(app, ctx);spa 的 catch-all
必须最后注册(Starlette 按注册顺序匹配)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from .. import __version__
from . import (agent_tools, automations, backends, boards, chat, credentials,
               documents, github, guidelines, projects, recycle_bin, resources, roles,
               spa, system, tasks, uploads, webhook_sources)
from ..collab.credentials import refresh_credential_contexts
from .context import ApiContext


def create_app(webhook_host: Optional[str] = None,
               webhook_port: Optional[int] = None) -> FastAPI:
    """webhook_port 为空或 0 时不启动专用 webhook 端口(测试与旁路进程默认如此)。"""
    ctx = ApiContext.build()
    if webhook_port:
        from .webhooks import WebhookListener
        ctx.webhook_listener = WebhookListener(
            ctx, webhook_host or "127.0.0.1", webhook_port)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from ..runtime import runtime_manager
        runtime_manager.set_usage_refresh_handler(
            ctx.role_usage_linkage.request_refresh)
        runtime_manager.start_idle_reaper()
        # 凭据目录与 gh 配置按库里的环境重写一遍:升级迁移(单值凭据改成环境)改的是库,
        # 各项目目录里还是旧文件,不等到下一次派发才更新
        refresh_credential_contexts(ctx.store, "")
        ctx.role_usage_linkage.start()
        ctx.automation_scheduler.start()
        ctx.github_poller.start()
        ctx.chat.callbacks.start()
        if ctx.webhook_listener:
            ctx.webhook_listener.start()
        try:
            yield
        finally:
            runtime_manager.set_usage_refresh_handler(None)
            if ctx.webhook_listener:
                ctx.webhook_listener.stop()
            # 先停轮询再停调度:调度器 stop 会关掉脚本执行池,轮询不能再往里投事件
            ctx.github_poller.stop()
            ctx.automation_scheduler.stop()
            ctx.chat.callbacks.stop()
            ctx.role_usage_linkage.stop()
            # 先等聊天执行池收尾再关 runtime:工作线程可能仍在驱动 Agent,
            # 且它按调用时的环境变量写频道历史,不能活过应用生命周期
            ctx.chat.shutdown()
            runtime_manager.shutdown()

    app = FastAPI(title="MissionCrew", version=__version__, lifespan=lifespan)
    # JSON/JS 文本响应压缩率高,弱网(远程隧道)访问时显著缩短加载时间
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    for module in (system, chat, agent_tools, roles, projects, resources, guidelines,
                   documents, boards, recycle_bin, backends, tasks, automations,
                   webhook_sources, credentials, github, uploads):
        module.register(app, ctx)
    spa.register(app, ctx)   # catch-all 兜底,必须最后
    return app
