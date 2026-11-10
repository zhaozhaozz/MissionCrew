"""GitHub 身份端点:核对全局或项目生效的 `GITHUB_TOKEN` 凭据是谁、有什么权限。

token 本身经凭据端点保存(只写不读);这里只返回核对结果,不含 token。核对要
访问 api.github.com,结果在进程内缓存,`refresh=true` 强制重新核对。
"""
from __future__ import annotations

from fastapi import FastAPI

from ..collab.github import github_status
from .context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/github")
    def global_identity(refresh: bool = False):
        return github_status(store, "", refresh=refresh)

    @app.get("/api/projects/{project_id}/github")
    def project_identity(project_id: str, refresh: bool = False):
        ctx.must_project(project_id)
        return github_status(store, project_id, refresh=refresh)
