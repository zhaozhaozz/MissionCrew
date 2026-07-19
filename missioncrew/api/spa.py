"""页面服务:静态资源、首页与干净 URL 的 SPA 兜底。必须最后注册(catch-all)。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .context import ApiContext

WEB_DIR = Path(__file__).parent.parent / "web"


def register(app: FastAPI, _ctx: ApiContext) -> None:
    # 前端拆分后的 css/js 走静态资源;挂载在 catch-all 之前,前缀优先匹配
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text()

    # 干净 URL 支持:必须注册在所有 API 路由之后(Starlette 按注册顺序匹配),
    # 非 API 路径一律返回页面,由前端路由还原视图
    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        return (WEB_DIR / "index.html").read_text()
