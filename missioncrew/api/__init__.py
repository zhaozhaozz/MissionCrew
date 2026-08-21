"""FastAPI 服务:统一任务入口的 Web 形态(聊天 + 看板 + REST API)。

纯本地运行:数据在本地 SQLite,执行是本地 Agent CLI 子进程,无任何云端依赖。
路由按资源域拆分模块,各模块提供 register(app, ctx);spa 的 catch-all
必须最后注册(Starlette 按注册顺序匹配)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import (agent_tools, automations, backends, boards, chat, documents,
               guidelines, projects, recycle_bin, resources, roles, spa, system,
               tasks)
from .context import ApiContext


def create_app() -> FastAPI:
    ctx = ApiContext.build()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from ..runtime import runtime_manager
        runtime_manager.set_usage_refresh_handler(
            ctx.role_usage_linkage.request_refresh)
        runtime_manager.start_idle_reaper()
        ctx.role_usage_linkage.start()
        ctx.automation_scheduler.start()
        try:
            yield
        finally:
            runtime_manager.set_usage_refresh_handler(None)
            ctx.automation_scheduler.stop()
            ctx.role_usage_linkage.stop()
            runtime_manager.shutdown()

    app = FastAPI(title="MissionCrew", version="0.2.0", lifespan=lifespan)
    for module in (system, chat, agent_tools, roles, projects, resources, guidelines,
                   documents, boards, recycle_bin, backends, tasks, automations):
        module.register(app, ctx)
    spa.register(app, ctx)   # catch-all 兜底,必须最后
    return app
