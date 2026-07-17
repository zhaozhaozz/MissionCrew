"""FastAPI 服务:统一任务入口的 Web 形态(聊天 + 看板 + REST API)。

纯本地运行:数据在本地 SQLite,执行是本地 Agent CLI 子进程,无任何云端依赖。
"""
from __future__ import annotations

import re
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
from .engine import Engine
from .models import TIER_ORDER, TRAITS, Channel, Project, Role, Rule
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


class RoleInput(BaseModel):
    id: str
    project_id: str
    name: str = ""
    description: str = ""
    required_capabilities: list[str] = []
    traits: list[str] = []
    pinned_backend: Optional[str] = None
    pinned_model: Optional[str] = None
    min_tier: Optional[str] = None
    max_tier: Optional[str] = None
    color: str = ""


class ProjectInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    repos: list[str] = []
    charter: str = ""
    dev_guidelines: str = ""
    skills: list[str] = []
    resources: list[str] = []
    rules_yaml: str = ""     # 验证准则,YAML 列表,服务端解析校验


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
    engine = Engine(store)
    chat = ChatEngine(store)

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
        }

    # ---------------- 聊天 ----------------

    @app.get("/api/chat/channels")
    def channels():
        return [c.to_dict() for c in store.list_channels()]

    @app.post("/api/chat/channels")
    def create_channel(body: ChannelCreate):
        if not body.project_id or store.get_project(body.project_id) is None:
            raise HTTPException(400, "频道必须归属一个已存在的项目")
        # 频道 id 以项目为命名空间,避免多项目下同名冲突
        cid = body.id if body.id.startswith(f"{body.project_id}:") \
            else f"{body.project_id}:{body.id}"
        if store.get_channel(cid):
            raise HTTPException(400, "频道已存在")
        c = Channel(id=cid, name=body.name or body.id,
                    project_id=body.project_id, workdir=body.workdir)
        store.put_channel(c)
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
        return {"traits": TRAITS, "tiers": TIER_ORDER}

    @app.post("/api/roles")
    def save_role(body: RoleInput):
        if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "角色 id 只能包含字母、数字、下划线、连字符")
        if store.get_project(body.project_id) is None:
            raise HTTPException(400, f"项目不存在: {body.project_id}")
        bad = [t for t in body.traits if t not in TRAITS]
        if bad:
            raise HTTPException(400, f"未知偏好标签: {bad}")
        if body.pinned_backend and store.get_backend(body.pinned_backend) is None:
            raise HTTPException(400, f"固定后端不存在: {body.pinned_backend}")
        role = Role(**body.model_dump())
        store.put_role(role)
        store.audit("human", "role_saved", detail=f"project={role.project_id} role={role.id}")
        return role.to_dict()

    @app.delete("/api/roles/{role_id}")
    def delete_role(role_id: str, project_id: str):
        if store.get_role(project_id, role_id) is None:
            raise HTTPException(404, "角色不存在")
        store.delete_role(project_id, role_id)
        store.audit("human", "role_deleted", detail=f"project={project_id} role={role_id}")
        return {"ok": True}

    @app.post("/api/projects")
    def save_project(body: ProjectInput):
        if not body.id.strip():
            raise HTTPException(400, "项目 id 不能为空")
        try:
            raw_rules = yaml.safe_load(body.rules_yaml) or [] if body.rules_yaml.strip() else []
            if not isinstance(raw_rules, list):
                raise ValueError("rules 必须是 YAML 列表")
            rules = [Rule(**r) for r in raw_rules]
        except (yaml.YAMLError, TypeError, ValueError) as e:
            raise HTTPException(400, f"验证准则解析失败: {e}")
        data = body.model_dump()
        data.pop("rules_yaml")
        is_new = store.get_project(body.id) is None
        project = Project(**data, rules=rules)
        store.put_project(project)
        if is_new:  # 新项目自动获得自己的默认角色和 general 频道
            seed_mod.init_project(store, project.id)
        store.audit("human", "project_saved", detail=f"project={project.id} new={is_new}")
        return project.to_dict()

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        if store.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在")
        store.delete_project(project_id)
        store.audit("human", "project_deleted", detail=f"project={project_id}")
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
            })
        # 注册表里的非内置工具(mock/自定义)也列出来
        for b in registered.values():
            rows.append({
                "binary": b.adapter, "adapter": b.adapter, "id": b.id,
                "installed": True, "path": b.binary_path, "version": b.version,
                "registered": True, "enabled": b.enabled,
                "models": [m.get("name") or "(默认)" for m in b.models],
            })
        return rows

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
        return {"found": [i["id"] for i in report if i["installed"]],
                "added": added, "updated": updated}

    @app.delete("/api/backends/{backend_id}")
    def delete_backend(backend_id: str):
        if store.get_backend(backend_id) is None:
            raise HTTPException(404, "后端不存在")
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

    return app
