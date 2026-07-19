"""FastAPI 服务:统一任务入口的 Web 形态(聊天 + 看板 + REST API)。

纯本地运行:数据在本地 SQLite,执行是本地 Agent CLI 子进程,无任何云端依赖。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import adapters
from . import seed as seed_mod
from .chat import ChatEngine
from .config import db_path
from .documents import archive_library, library_for, safe_relative_path
from .engine import Engine
from .models import (BOARD_WIDGET_TYPES, ROLE_ABILITIES, TIER_ORDER, Board, BoardWidget, Channel,
                     GuidelineDocument, Project, ProjectResource, ProjectSkill, Role, Rule)
from .store import Store

WEB_DIR = Path(__file__).parent / "web"
MENTION_ID_RE = re.compile(r"[\w-]+")


class TaskCreate(BaseModel):
    project_id: str
    title: str
    description: str = ""
    task_type: str = "feature"
    labels: list[str] = []
    risk: str = "normal"
    security_level: int = 0
    max_tier: Optional[str] = None


class ApprovalInput(BaseModel):
    approver: str = "human"
    decision: str = "approved"
    note: str = ""
    stage: Optional[str] = None


class MessageInput(BaseModel):
    author: str = "human"
    content: str


class ChannelCreate(BaseModel):
    id: str
    name: str = ""
    project_id: Optional[str] = None
    workdir: Optional[str] = None
    purpose: str = ""
    actor_role_id: Optional[str] = None


class RoleInput(BaseModel):
    id: str
    project_id: str
    runtime_id: str = ""       # 空 = 不固定,由平台按能力自动路由
    model: str = ""
    name: str = ""
    description: str = ""
    capabilities: list[str] = []   # 固定能力选项(ROLE_ABILITIES)
    preference: str = ""           # 偏好:自由文本(风格/领域,如前端/后端)
    color: str = ""


class GuidelineInput(BaseModel):
    id: str
    title: str = ""
    content: str = ""
    file_refs: list[str] = []
    enabled: bool = True
    actor_role_id: Optional[str] = None


class SkillInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    instructions: str = ""
    file_refs: list[str] = []
    runtime_ids: list[str] = []
    adapters: list[str] = []
    runtime_instructions: dict[str, str] = {}
    enabled: bool = True
    actor_role_id: Optional[str] = None


class ProjectInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    repos: Optional[list[dict | str]] = None   # None = 保留;资源经专用端点管理
    charter: str = ""
    dev_guidelines: Optional[str] = None       # 已由准则文档替代;None = 保留
    orchestrator_role_id: Optional[str] = None
    guidelines: Optional[list[dict]] = None
    skills: Optional[list[dict | str]] = None
    resources: Optional[list[str]] = None
    required_env: Optional[str] = None
    rules_yaml: Optional[str] = None  # 验证准则,YAML 列表；None 表示更新时保留


class ResourceAdd(BaseModel):
    target: str          # 本地路径或 git 远程地址
    name: str = ""


class DocumentWrite(BaseModel):
    content: str
    actor: str = "human"
    message: str = ""


class DocumentRestore(BaseModel):
    path: str
    revision: str
    actor: str = "human"


class BoardInput(BaseModel):
    id: str
    name: str = ""
    description: Optional[str] = None    # None = 更新时保留现值
    layout: Optional[list[dict]] = None  # None = 更新时保留现有布局
    actor_role_id: Optional[str] = None


class WidgetDataInput(BaseModel):
    widgets: list[dict] = []


class BackendInput(BaseModel):
    id: str
    name: Optional[str] = None
    model: Optional[str] = None
    tier: Optional[str] = None
    cost_per_run: Optional[float] = None
    security_level: Optional[int] = None
    capabilities: Optional[list[str]] = None
    models: Optional[list[dict]] = None   # 模型阶梯 [{name, tier, cost}]
    enabled: Optional[bool] = None


def create_app() -> FastAPI:
    app = FastAPI(title="MissionCrew", version="0.2.0")
    store = Store(db_path())
    seed_mod.ensure_role_bindings(store)
    seed_mod.migrate_project_fields(store)
    engine = Engine(store)
    chat = ChatEngine(store)
    # 更新互斥与"更新中"标记:与 ChatEngine 共享同一集合,更新期间不派发该后端
    updating_backends: set[str] = set()
    updating_guard = threading.Lock()
    chat.updating_backends = updating_backends
    # runtime 模型目录缓存:发现可能要起进程(codex/opencode/ACP),10 分钟内复用
    model_catalog_cache: dict[str, tuple[float, list[str]]] = {}
    model_catalog_guard = threading.Lock()

    def discovered_models(backend, refresh: bool = False) -> list[str]:
        import time as _time
        with model_catalog_guard:
            cached = model_catalog_cache.get(backend.id)
            if cached and not refresh and _time.time() - cached[0] < 600:
                return cached[1]
        models = adapters.list_runtime_models(backend)
        with model_catalog_guard:
            model_catalog_cache[backend.id] = (_time.time(), models)
        return models

    def must_project(project_id: str) -> Project:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        return project

    def validate_orchestrator_actor(project: Project, actor_role_id: Optional[str]) -> str:
        """人类请求无需角色身份；以角色身份调用时只允许项目主控。"""
        if actor_role_id and actor_role_id != project.orchestrator_role_id:
            raise HTTPException(403, f"只有项目主控 @{project.orchestrator_role_id} 可以执行此操作")
        return actor_role_id or "human"

    def namespaced_id(project_id: str, raw_id: str, kind: str) -> str:
        short_id = raw_id.removeprefix(f"{project_id}:")
        if not short_id or not MENTION_ID_RE.fullmatch(short_id):
            raise HTTPException(400, f"{kind} id 只能包含字母、数字、下划线、连字符")
        return f"{project_id}:{short_id}"

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text()

    @app.get("/api/overview")
    def overview():
        return {
            "projects": [p.to_dict() for p in store.list_projects()],
            "backends": [b.to_dict() for b in store.list_backends()],
            "tasks": [t.to_dict() for t in store.list_tasks()],
            "roles": [r.to_dict() for r in store.list_roles()],
            "channels": [c.to_dict() for c in store.list_channels()],
            "boards": [b.to_dict() for b in store.list_boards()],
        }

    # ---------------- 聊天 ----------------

    @app.get("/api/chat/channels")
    def channels():
        return [c.to_dict() for c in store.list_channels()]

    @app.post("/api/chat/channels")
    def create_channel(body: ChannelCreate):
        if not body.project_id or store.get_project(body.project_id) is None:
            raise HTTPException(400, "频道必须归属一个已存在的项目")
        project = must_project(body.project_id)
        actor = validate_orchestrator_actor(project, body.actor_role_id)
        # 频道 id 以项目为命名空间,避免多项目下同名冲突
        cid = namespaced_id(body.project_id, body.id, "频道")
        if store.get_channel(cid):
            raise HTTPException(400, "频道已存在")
        c = Channel(id=cid, name=body.name or body.id,
                    project_id=body.project_id, workdir=body.workdir,
                    purpose=body.purpose, created_by_role_id=body.actor_role_id or "")
        store.put_channel(c)
        store.audit(actor, "channel_created", detail=f"project={body.project_id} channel={cid}")
        return c.to_dict()

    @app.get("/api/chat/{channel_id}/messages")
    def messages(channel_id: str, after_id: int = 0):
        if store.get_channel(channel_id) is None:
            raise HTTPException(404, "频道不存在")
        return {
            "messages": store.list_messages(channel_id, after_id),
            "active_runs": store.active_chat_runs(channel_id),
        }

    @app.post("/api/chat/{channel_id}/messages")
    def post_message(channel_id: str, body: MessageInput):
        try:
            msg_id = chat.post(channel_id, body.author, body.content)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"id": msg_id}

    # ---------------- 配置管理(设置页) ----------------

    @app.get("/api/roles")
    def roles():
        return [r.to_dict() for r in store.list_roles()]

    @app.get("/api/traits")
    def traits():
        return {"abilities": ROLE_ABILITIES, "tiers": TIER_ORDER,
                "board_widget_types": sorted(BOARD_WIDGET_TYPES)}

    @app.post("/api/roles")
    def save_role(body: RoleInput):
        if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "角色 id 只能包含字母、数字、下划线、连字符")
        if store.get_project(body.project_id) is None:
            raise HTTPException(400, f"项目不存在: {body.project_id}")
        bad = [c for c in body.capabilities if c not in ROLE_ABILITIES]
        if bad:
            raise HTTPException(400, f"未知能力选项: {bad}(可用: {', '.join(sorted(ROLE_ABILITIES))})")
        backend = store.get_backend(body.runtime_id) if body.runtime_id else None
        if body.runtime_id and backend is None:
            raise HTTPException(400, f"固定 runtime 不存在: {body.runtime_id}")
        if backend is None:   # 自动路由:模型随 runtime 决定,不做模型校验
            role = Role(**body.model_dump())
            store.put_role(role)
            store.audit("human", "role_saved",
                        detail=f"project={role.project_id} role={role.id} (自动路由)")
            return role.to_dict()
        known_models = {str(m.get("name", "")) for m in backend.models}
        if known_models and body.model not in known_models:
            # 配置阶梯之外:再查 runtime 动态发现的模型目录(仿 Multica 从 runtime 取)
            if body.model not in set(discovered_models(backend)):
                raise HTTPException(
                    400, f"模型 {body.model or '(CLI 默认)'} 不属于 runtime {body.runtime_id}")
        role = Role(**body.model_dump())
        store.put_role(role)
        store.audit("human", "role_saved", detail=f"project={role.project_id} role={role.id}")
        return role.to_dict()

    @app.delete("/api/roles/{role_id}")
    def delete_role(role_id: str, project_id: str):
        if store.get_role(project_id, role_id) is None:
            raise HTTPException(404, "角色不存在")
        project = must_project(project_id)
        if project.orchestrator_role_id == role_id:
            raise HTTPException(409, "不能删除项目主控角色；请先为项目选择其他主控")
        store.delete_role(project_id, role_id)
        store.audit("human", "role_deleted", detail=f"project={project_id} role={role_id}")
        return {"ok": True}

    @app.post("/api/projects")
    def save_project(body: ProjectInput):
        if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "项目 id 只能包含字母、数字、下划线、连字符")
        existing = store.get_project(body.id)
        try:
            if body.rules_yaml is None:
                raw_rules = [r.__dict__ for r in existing.rules] if existing else []
            else:
                raw_rules = (yaml.safe_load(body.rules_yaml) or []) \
                    if body.rules_yaml.strip() else []
            if not isinstance(raw_rules, list):
                raise ValueError("rules 必须是 YAML 列表")
            rules = [Rule(**r) for r in raw_rules]
        except (yaml.YAMLError, TypeError, ValueError) as e:
            raise HTTPException(400, f"验证准则解析失败: {e}")
        data = body.model_dump()
        data.pop("rules_yaml")
        is_new = existing is None
        if is_new and not seed_mod.has_enabled_runtime(store):
            raise HTTPException(400, "请先检测并启用至少一个 runtime,再创建项目角色")
        orchestrator = body.orchestrator_role_id or (
            existing.orchestrator_role_id if existing else "lead")
        if existing and store.get_role(body.id, orchestrator) is None:
            raise HTTPException(400, f"主控角色不属于当前项目: @{orchestrator}")
        if is_new and orchestrator != "lead":
            raise HTTPException(400, "新项目请先创建角色，再修改主控角色")
        data["orchestrator_role_id"] = orchestrator
        data["repos"] = (body.repos if body.repos is not None else
                         ([r.__dict__ for r in existing.repos] if existing else []))
        data["dev_guidelines"] = (body.dev_guidelines if body.dev_guidelines is not None
                                  else (existing.dev_guidelines if existing else ""))
        data["guidelines"] = (body.guidelines if body.guidelines is not None else
                              ([g.__dict__ for g in existing.guidelines] if existing else []))
        data["skills"] = (body.skills if body.skills is not None else
                          ([s.__dict__ for s in existing.skills] if existing else []))
        data["resources"] = (body.resources if body.resources is not None else
                             (existing.resources if existing else []))
        data["required_env"] = (body.required_env if body.required_env is not None else
                                (existing.required_env if existing else None))
        data["rules"] = [r.__dict__ for r in rules]
        try:
            project = Project.from_dict(data)
        except TypeError as exc:
            raise HTTPException(400, f"项目准则或 Skill 格式不合法: {exc}")
        for doc in project.guidelines:
            if not MENTION_ID_RE.fullmatch(doc.id):
                raise HTTPException(400, f"准则 id 不合法: {doc.id}")
            try:
                [safe_relative_path(ref) for ref in doc.file_refs]
            except ValueError as exc:
                raise HTTPException(400, f"准则 {doc.id} 的文件引用不合法: {exc}")
        for skill in project.skills:
            if not MENTION_ID_RE.fullmatch(skill.id):
                raise HTTPException(400, f"Skill id 不合法: {skill.id}")
            unknown = [runtime_id for runtime_id in skill.runtime_ids
                       if store.get_backend(runtime_id) is None]
            if unknown:
                raise HTTPException(400, f"Skill {skill.id} 引用了不存在的 Runtime: {unknown}")
            try:
                [safe_relative_path(ref) for ref in skill.file_refs]
            except ValueError as exc:
                raise HTTPException(400, f"Skill {skill.id} 的文件引用不合法: {exc}")
        store.put_project(project)
        if is_new:  # 新项目自动获得自己的默认角色和 general 频道
            seed_mod.init_project(store, project.id)
            library_for(project.id)
        store.audit("human", "project_saved", detail=f"project={project.id} new={is_new}")
        return project.to_dict()

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        if store.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在")
        archive = archive_library(project_id)
        store.delete_project(project_id)
        store.audit("human", "project_deleted",
                    detail=f"project={project_id} documents_archive={archive}")
        return {"ok": True, "documents_archive": archive}

    # ---------------- 项目准则与 Skills ----------------

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

    # ---------------- 项目资源(本地路径 / git 仓) ----------------

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

    @app.post("/api/projects/{project_id}/resources")
    def add_resource(project_id: str, body: ResourceAdd):
        project = must_project(project_id)
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
        project = must_project(project_id)
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
        project = must_project(project_id)
        before = len(project.repos)
        project.repos = [r for r in project.repos if r.id != resource_id]
        if len(project.repos) == before:
            raise HTTPException(404, "资源不存在")
        store.put_project(project)
        store.audit("human", "resource_removed",
                    detail=f"project={project_id} resource={resource_id}")
        return {"ok": True}

    @app.get("/api/projects/{project_id}/guidelines")
    def list_guidelines(project_id: str):
        return [g.__dict__ for g in must_project(project_id).guidelines]

    @app.post("/api/projects/{project_id}/guidelines")
    def save_guideline(project_id: str, body: GuidelineInput):
        project = must_project(project_id)
        actor = validate_orchestrator_actor(project, body.actor_role_id)
        if not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "准则 id 只能包含字母、数字、下划线、连字符")
        try:
            [safe_relative_path(ref) for ref in body.file_refs]
        except ValueError as exc:
            raise HTTPException(400, f"准则文件引用不合法: {exc}")
        guideline = GuidelineDocument(**body.model_dump(exclude={"actor_role_id"}))
        project.guidelines = [g for g in project.guidelines if g.id != guideline.id]
        project.guidelines.append(guideline)
        store.put_project(project)
        store.audit(actor, "guideline_saved",
                    detail=f"project={project_id} guideline={guideline.id}")
        return guideline.__dict__

    @app.delete("/api/projects/{project_id}/guidelines/{guideline_id}")
    def delete_guideline(project_id: str, guideline_id: str,
                         actor_role_id: Optional[str] = None):
        project = must_project(project_id)
        actor = validate_orchestrator_actor(project, actor_role_id)
        before = len(project.guidelines)
        project.guidelines = [g for g in project.guidelines if g.id != guideline_id]
        if len(project.guidelines) == before:
            raise HTTPException(404, "准则不存在")
        store.put_project(project)
        store.audit(actor, "guideline_deleted",
                    detail=f"project={project_id} guideline={guideline_id}")
        return {"ok": True}

    @app.get("/api/projects/{project_id}/skills")
    def list_skills(project_id: str):
        return [s.__dict__ for s in must_project(project_id).skills]

    @app.post("/api/projects/{project_id}/skills")
    def save_skill(project_id: str, body: SkillInput):
        project = must_project(project_id)
        actor = validate_orchestrator_actor(project, body.actor_role_id)
        if not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "Skill id 只能包含字母、数字、下划线、连字符")
        unknown = [runtime_id for runtime_id in body.runtime_ids
                   if store.get_backend(runtime_id) is None]
        if unknown:
            raise HTTPException(400, f"Skill 引用了不存在的 Runtime: {unknown}")
        try:
            [safe_relative_path(ref) for ref in body.file_refs]
        except ValueError as exc:
            raise HTTPException(400, f"Skill 文件引用不合法: {exc}")
        skill = ProjectSkill(**body.model_dump(exclude={"actor_role_id"}))
        project.skills = [s for s in project.skills if s.id != skill.id]
        project.skills.append(skill)
        store.put_project(project)
        store.audit(actor, "skill_saved", detail=f"project={project_id} skill={skill.id}")
        return skill.__dict__

    @app.delete("/api/projects/{project_id}/skills/{skill_id}")
    def delete_skill(project_id: str, skill_id: str,
                     actor_role_id: Optional[str] = None):
        project = must_project(project_id)
        actor = validate_orchestrator_actor(project, actor_role_id)
        before = len(project.skills)
        project.skills = [s for s in project.skills if s.id != skill_id]
        if len(project.skills) == before:
            raise HTTPException(404, "Skill 不存在")
        store.put_project(project)
        store.audit(actor, "skill_deleted", detail=f"project={project_id} skill={skill_id}")
        return {"ok": True}

    # ---------------- 版本化文档库 ----------------

    @app.get("/api/projects/{project_id}/documents")
    def list_documents(project_id: str):
        must_project(project_id)
        library = library_for(project_id)
        return {"root": str(library.root), "files": library.list_files(),
                "history": library.history(limit=20)}

    @app.get("/api/projects/{project_id}/documents/history")
    def document_history(project_id: str, path: Optional[str] = None, limit: int = 100):
        must_project(project_id)
        try:
            return library_for(project_id).history(path, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/projects/{project_id}/documents/file/{file_path:path}")
    def read_document(project_id: str, file_path: str, revision: Optional[str] = None):
        must_project(project_id)
        try:
            content = library_for(project_id).read(file_path, revision)
        except UnicodeDecodeError:   # 注意:它是 ValueError 子类,必须先捕获
            raise HTTPException(415, "二进制或非 UTF-8 文件,无法在线查看(可直接在文档库目录中操作)")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        return {"path": file_path, "revision": revision, "content": content}

    @app.post("/api/projects/{project_id}/documents/restore")
    def restore_document(project_id: str, body: DocumentRestore):
        """把文件恢复到历史版本(作为新版本提交,历史保持完整)。"""
        must_project(project_id)
        try:
            revision = library_for(project_id).restore(body.path, body.revision, body.actor)
        except UnicodeDecodeError:   # ValueError 子类,先捕获
            raise HTTPException(415, "二进制文件请直接在文档库目录中恢复")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        store.audit(body.actor, "document_restored",
                    detail=f"project={project_id} path={body.path} "
                           f"from={body.revision[:10]} new={revision[:10]}")
        return {"path": body.path, "revision": revision}

    @app.put("/api/projects/{project_id}/documents/file/{file_path:path}")
    def write_document(project_id: str, file_path: str, body: DocumentWrite):
        must_project(project_id)
        try:
            revision = library_for(project_id).write(
                file_path, body.content, body.actor, body.message)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        store.audit(body.actor, "document_saved",
                    detail=f"project={project_id} path={file_path} revision={revision}")
        return {"path": file_path, "revision": revision}

    @app.delete("/api/projects/{project_id}/documents/file/{file_path:path}")
    def delete_document(project_id: str, file_path: str, actor: str = "human"):
        must_project(project_id)
        try:
            revision = library_for(project_id).delete(file_path, actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        store.audit(actor, "document_deleted",
                    detail=f"project={project_id} path={file_path} revision={revision}")
        return {"ok": True, "revision": revision}

    # ---------------- 自定义面板 ----------------

    @app.get("/api/projects/{project_id}/boards")
    def list_boards(project_id: str):
        must_project(project_id)
        return [board.to_dict() for board in store.list_boards(project_id)]

    def _resolve_widget_source(project_id: str, source: dict):
        """解析卡片数据源:卡片是通用展示原语,领域数据从平台实时取。"""
        kind = source.get("from")
        if kind == "tasks":
            want_status = source.get("status") or []
            want_labels = source.get("labels") or []
            want_type = source.get("task_type") or []
            rows = []
            for t in store.list_tasks():
                if t.project_id != project_id:
                    continue
                if want_status and t.status not in want_status:
                    continue
                if want_type and t.task_type not in want_type:
                    continue
                if want_labels and not set(t.labels) & set(want_labels):
                    continue
                stage = t.current_stage.name if t.current_stage else "-"
                rows.append({"id": t.id, "标题": t.title, "状态": t.status,
                             "类型": t.task_type, "风险": t.risk, "阶段": stage,
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
        must_project(project_id)
        resolved = {}
        for w in body.widgets:
            source = ((w.get("content") or {}).get("source")) or None
            if isinstance(source, dict) and w.get("id"):
                resolved[w["id"]] = _resolve_widget_source(project_id, source)
        return resolved

    @app.post("/api/projects/{project_id}/boards")
    def save_board(project_id: str, body: BoardInput):
        project = must_project(project_id)
        actor = validate_orchestrator_actor(project, body.actor_role_id)
        board_id = namespaced_id(project_id, body.id, "面板")
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
        return board.to_dict()

    @app.delete("/api/projects/{project_id}/boards/{board_id}")
    def delete_board(project_id: str, board_id: str,
                     actor_role_id: Optional[str] = None):
        project = must_project(project_id)
        actor = validate_orchestrator_actor(project, actor_role_id)
        full_id = namespaced_id(project_id, board_id, "面板")
        board = store.get_board(full_id)
        if board is None or board.project_id != project_id:
            raise HTTPException(404, "面板不存在")
        store.delete_board(full_id)
        store.audit(actor, "board_deleted", detail=f"project={project_id} board={full_id}")
        return {"ok": True}

    @app.post("/api/backends")
    def update_backend(body: BackendInput):
        b = store.get_backend(body.id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        if body.tier is not None and body.tier not in TIER_ORDER:
            raise HTTPException(400, f"档位必须是 {TIER_ORDER} 之一")
        if body.models is not None:
            for m in body.models:
                if m.get("tier") not in TIER_ORDER:
                    raise HTTPException(400, f"模型 {m.get('name') or '(默认)'} 的档位必须是 {TIER_ORDER} 之一")
                try:
                    m["cost"] = float(m.get("cost", 1.0))
                except (TypeError, ValueError):
                    raise HTTPException(400, f"模型 {m.get('name') or '(默认)'} 的成本必须是数字")
                m["name"] = str(m.get("name", ""))
            model_names = {m["name"] for m in body.models}
            invalid_roles = [r for r in store.list_roles()
                             if r.runtime_id == b.id and model_names and r.model not in model_names]
            if invalid_roles:
                names = ", ".join(f"{r.project_id}/@{r.id}" for r in invalid_roles)
                raise HTTPException(400, f"模型仍被角色使用,请先修改角色: {names}")
        for field in ("name", "model", "tier", "cost_per_run", "security_level",
                      "capabilities", "models", "enabled"):
            v = getattr(body, field)
            if v is not None:
                setattr(b, field, v)
        store.put_backend(b)
        store.audit("human", "backend_updated", detail=f"backend={b.id}")
        return b.to_dict()

    @app.get("/api/backends/tools")
    def tools():
        """支持的工具矩阵 + 安装/注册状态(仿 Multica Runtime 页;不探测版本,快速渲染)。"""
        report = adapters.detect_report(with_version=False)
        registered = {b.id: b for b in store.list_backends()}
        project_names = {p.id: p.name for p in store.list_projects()}
        role_users: dict[str, list[dict]] = {}
        for role in store.list_roles():
            if not role.runtime_id:
                continue
            role_users.setdefault(role.runtime_id, []).append({
                "id": role.id,
                "name": role.name,
                "project_id": role.project_id,
                "project_name": project_names.get(role.project_id, role.project_id),
                "model": role.model,
            })
        for users in role_users.values():
            users.sort(key=lambda r: (r["project_name"], r["id"]))

        def usage(backend_id: str) -> dict:
            users = role_users.get(backend_id, [])
            return {"role_count": len(users), "role_users": users}

        rows = []
        for item in report:
            b = registered.pop(item["id"], None)
            rows.append({
                **item,
                "registered": b is not None,
                "enabled": b.enabled if b else False,
                "version": (b.version if b else "") or "",
                "path": (b.binary_path if b and b.binary_path else item["path"]),
                "models": [m.get("name") or "(默认)" for m in (b.models if b else [])],
                "updatable": bool(b and adapters.update_plan(b)),
                **usage(item["id"]),
            })
        # 注册表里的非内置工具(mock/自定义)也列出来
        for b in registered.values():
            rows.append({
                "binary": b.adapter, "adapter": b.adapter, "id": b.id,
                "installed": True, "path": b.binary_path, "version": b.version,
                "registered": True, "enabled": b.enabled,
                "models": [m.get("name") or "(默认)" for m in b.models],
                "updatable": bool(adapters.update_plan(b)),
                **usage(b.id),
            })
        return rows

    @app.get("/api/backends/{backend_id}/models")
    def backend_models(backend_id: str, refresh: bool = False):
        """runtime 可用模型:向工具本体动态查询(缓存 10 分钟),
        configured 为工具的模型阶梯(带档位/成本),discovered 为 runtime 目录。"""
        b = store.get_backend(backend_id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        return {
            "configured": [{"name": m.get("name", ""), "tier": m.get("tier", ""),
                            "cost": m.get("cost")} for m in b.models],
            "discovered": discovered_models(b, refresh=refresh),
        }

    @app.post("/api/backends/check_updates")
    def check_updates():
        """并行查询各工具的最新发布版本,与已装版本比对(仅注册且已安装的工具)。"""
        backends = [b for b in store.list_backends()
                    if b.adapter in adapters.UPDATE_SPECS and b.binary_path]
        with ThreadPoolExecutor(max_workers=8) as pool:
            latest = dict(zip(
                (b.id for b in backends),
                pool.map(lambda b: adapters.fetch_latest_version(b.adapter), backends),
            ))
        results = []
        for b in backends:
            lv = latest.get(b.id, "")
            results.append({
                "id": b.id, "installed": b.version, "latest": lv,
                "update_available": adapters.is_newer(lv, b.version),
                "updatable": adapters.update_plan(b) is not None,
            })
        return results

    @app.post("/api/backends/{backend_id}/update")
    def do_update(backend_id: str):
        """执行工具更新(自更新命令或 npm),完成后重新探测版本入库。

        互斥:同一后端同时只允许一个更新(并发 npm install -g 会写坏全局安装);
        更新期间该后端不再被派发新的聊天执行(见 ChatEngine.updating_backends)。
        """
        b = store.get_backend(backend_id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        with updating_guard:
            if backend_id in updating_backends:
                raise HTTPException(409, "该工具正在更新中,请等待完成")
            updating_backends.add(backend_id)
        try:
            ok, log = adapters.run_update(b)
            old_version = b.version
            # 更新可长达数分钟:重取最新记录,只补检测字段,
            # 避免过期快照覆盖窗口期内的配额扣减/启停等修改
            fresh = store.get_backend(backend_id)
            if fresh is None:
                raise HTTPException(409, "后端在更新期间被删除")
            binary = Path(fresh.binary_path).name if fresh.binary_path else fresh.adapter
            new_path = shutil.which(binary)
            if new_path:   # 原生更新器可能切换版本目录
                fresh.binary_path = new_path
                fresh.version = adapters._cli_version(binary)
                store.put_backend(fresh)
            store.audit("human", "backend_update", detail=(
                f"backend={backend_id} ok={ok} {old_version} -> {fresh.version}"))
            return {"ok": ok, "old_version": old_version, "version": fresh.version,
                    "log": log[-1500:]}
        finally:
            with updating_guard:
                updating_backends.discard(backend_id)

    @app.post("/api/backends/detect")
    def detect():
        report = adapters.detect_report()   # 含版本探测
        added, updated = [], []
        for b in adapters.detect_backends(report):
            existing = store.get_backend(b.id)
            if existing is None:
                store.put_backend(b)
                added.append(b.id)
            else:  # 只刷新检测信息,保留用户的启停/配额/模型阶梯调整
                existing.binary_path, existing.version = b.binary_path, b.version
                if not existing.models:
                    existing.models = b.models
                store.put_backend(existing)
                updated.append(b.id)
        seed_mod.ensure_default_project(store)
        seed_mod.ensure_role_bindings(store)
        return {"found": [i["id"] for i in report if i["installed"]],
                "added": added, "updated": updated}

    @app.delete("/api/backends/{backend_id}")
    def delete_backend(backend_id: str):
        if store.get_backend(backend_id) is None:
            raise HTTPException(404, "后端不存在")
        users = [r for r in store.list_roles() if r.runtime_id == backend_id]
        if users:
            names = ", ".join(f"{r.project_id}/@{r.id}" for r in users)
            raise HTTPException(409, f"runtime 仍被角色使用,请先修改角色: {names}")
        store.delete_backend(backend_id)
        store.audit("human", "backend_deleted", detail=f"backend={backend_id}")
        return {"ok": True}

    @app.delete("/api/chat/channels/{channel_id}")
    def delete_channel(channel_id: str):
        if store.get_channel(channel_id) is None:
            raise HTTPException(404, "频道不存在")
        store.delete_channel(channel_id)
        store.audit("human", "channel_deleted", detail=f"channel={channel_id}")
        return {"ok": True}

    # ---------------- 任务 ----------------

    @app.post("/api/tasks")
    def create_task(body: TaskCreate):
        try:
            t = engine.create_task(body.project_id, body.title, body.description,
                                   body.task_type, body.labels, body.risk,
                                   body.security_level, body.max_tier)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return t.to_dict()

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        t = store.get_task(task_id)
        if t is None:
            raise HTTPException(404, "任务不存在")
        return {
            "task": t.to_dict(),
            "evidence": store.list_evidence(task_id),
            "runs": store.list_runs(task_id),
            "approvals": store.list_approvals(task_id),
            "audit": store.list_audit(task_id, 100),
        }

    @app.post("/api/tasks/{task_id}/advance")
    def advance(task_id: str):
        try:
            reports = engine.run(task_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"messages": [r.message for r in reports],
                "task": reports[-1].task.to_dict() if reports else None}

    @app.post("/api/tasks/{task_id}/approve")
    def approve(task_id: str, body: ApprovalInput):
        try:
            engine.approve(task_id, body.approver, body.decision, body.note, body.stage)
            reports = engine.run(task_id) if body.decision == "approved" else []
        except ValueError as e:
            raise HTTPException(400, str(e))
        t = store.get_task(task_id)
        return {"messages": [r.message for r in reports],
                "task": t.to_dict() if t else None}

    # 干净 URL 支持:必须注册在所有 API 路由之后(Starlette 按注册顺序匹配),
    # 非 API 路径一律返回页面,由前端路由还原视图
    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        return (WEB_DIR / "index.html").read_text()

    return app
