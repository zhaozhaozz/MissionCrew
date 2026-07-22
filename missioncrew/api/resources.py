"""项目资源端点:本地路径/git 仓的绑定、刷新与目录浏览。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ..collab.recycle_bin import recycle_project_resource
from ..core.models import ProjectResource
from .context import ApiContext
from .schemas import ResourceAdd


def _git_remotes(path: Path) -> list[dict]:
    """列出 git 仓的全部远程(name+url,fetch/push 去重)。"""
    try:
        proc = subprocess.run(["git", "-C", str(path), "remote", "-v"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    seen: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in seen:
            seen[parts[0]] = parts[1]
    return [{"name": k, "url": v} for k, v in seen.items()]


def _resolve_project_resource(target: str, name: str) -> ProjectResource:
    """解析资源:远程地址 -> git 资源;本地路径若是 git 仓自动绑定其远程。"""
    raw = target.strip()
    if not raw:
        raise HTTPException(400, "资源路径或地址不能为空")
    looks_remote = raw.startswith(("http://", "https://", "git@", "ssh://"))
    if looks_remote:
        base = raw.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "repo"
        return ProjectResource(id=base, kind="git", remote=raw, name=name or base)
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise HTTPException(400, f"本地路径不存在或不是目录: {raw}")
    base = path.name
    if (path / ".git").exists():
        # 自动绑定远程:优先 origin,多远程时退而取第一个;无远程也算 git 资源
        remotes = _git_remotes(path)
        remote = next((r["url"] for r in remotes if r["name"] == "origin"),
                      remotes[0]["url"] if remotes else "")
        return ProjectResource(id=base, kind="git", path=str(path),
                               remote=remote, name=name or base)
    return ProjectResource(id=base, kind="path", path=str(path), name=name or base)


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/fs/dirs")
    def fs_dirs(path: str = "", hidden: bool = False):
        """本地目录浏览(资源路径选择器用):列出子目录;默认从用户主目录开始,
        hidden=true 时包含隐藏目录。"""
        base = Path(path).expanduser() if path.strip() else Path.home()
        try:
            base = base.resolve()
            if not base.is_dir():
                raise HTTPException(400, f"不是目录: {base}")
            dirs = sorted(d.name for d in base.iterdir()
                          if d.is_dir() and (hidden or not d.name.startswith(".")))[:200]
        except PermissionError:
            raise HTTPException(400, f"无权限访问: {base}")
        except OSError as exc:
            raise HTTPException(400, f"无法读取目录: {exc}")
        parent = str(base.parent) if base.parent != base else ""
        is_git = (base / ".git").exists()
        return {"path": str(base), "parent": parent, "dirs": dirs,
                "is_git": is_git,
                "remotes": _git_remotes(base) if is_git else []}

    @app.post("/api/projects/{project_id}/resources")
    def add_resource(project_id: str, body: ResourceAdd):
        project = ctx.must_project(project_id)
        resource = _resolve_project_resource(body.target, body.name)
        taken = {r.id for r in project.repos}
        if resource.id in taken:   # id 冲突时追加序号
            n = 2
            while f"{resource.id}-{n}" in taken:
                n += 1
            resource.id = f"{resource.id}-{n}"
        project.repos.append(resource)
        store.put_project(project)
        store.audit("human", "resource_added",
                    detail=f"project={project_id} resource={resource.id} kind={resource.kind}")
        return resource.__dict__

    @app.post("/api/projects/{project_id}/resources/{resource_id}/refresh")
    def refresh_resource(project_id: str, resource_id: str):
        """重新探测资源的 git 绑定:路径后来 init 了 git、换了远程等场景。"""
        project = ctx.must_project(project_id)
        res = next((r for r in project.repos if r.id == resource_id), None)
        if res is None:
            raise HTTPException(404, "资源不存在")
        if not res.path:
            raise HTTPException(400, "该资源没有本地路径(纯远程 git 资源),无需刷新")
        path = Path(res.path).expanduser()
        if not path.is_dir():
            raise HTTPException(400, f"本地路径已不存在: {res.path}")
        if (path / ".git").exists():
            remotes = _git_remotes(path)
            res.kind = "git"
            res.remote = next((r["url"] for r in remotes if r["name"] == "origin"),
                              remotes[0]["url"] if remotes else "")
        else:
            res.kind, res.remote = "path", ""
        store.put_project(project)
        store.audit("human", "resource_refreshed",
                    detail=f"project={project_id} resource={resource_id} "
                           f"kind={res.kind} remote={res.remote}")
        return res.__dict__

    @app.delete("/api/projects/{project_id}/resources/{resource_id}")
    def delete_resource(project_id: str, resource_id: str):
        project = ctx.must_project(project_id)
        try:
            item = recycle_project_resource(
                store, project, resource_id, actor="human")
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "recycle_item": item}
