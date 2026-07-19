"""FastAPI 服务:统一任务入口的 Web 形态(聊天 + 看板 + REST API)。

纯本地运行:数据在本地 SQLite,执行是本地 Agent CLI 子进程,无任何云端依赖。
路由按资源域拆分模块,各模块提供 register(app, ctx);spa 的 catch-all
必须最后注册(Starlette 按注册顺序匹配)。
"""
from __future__ import annotations

from fastapi import FastAPI

from . import (backends, boards, chat, documents, guidelines, projects,
               resources, roles, spa, system, tasks)
from .context import ApiContext


def create_app() -> FastAPI:
    app = FastAPI(title="MissionCrew", version="0.2.0")
    ctx = ApiContext.build()
    for module in (system, chat, roles, projects, resources, guidelines,
                   documents, boards, backends, tasks):
        module.register(app, ctx)
    spa.register(app, ctx)   # catch-all 兜底,必须最后
    return app
