"""角色端点:定义时固定 runtime/model 的角色 CRUD。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..core.models import ROLE_ABILITIES, Role
from .context import MENTION_ID_RE, ApiContext
from .schemas import RoleInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/roles")
    def roles():
        return [r.to_dict() for r in store.list_roles()]

    @app.post("/api/roles")
    def save_role(body: RoleInput):
        if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "角色 id 只能包含字母、数字、下划线、连字符")
        if store.get_project(body.project_id) is None:
            raise HTTPException(400, f"项目不存在: {body.project_id}")
        bad = [c for c in body.capabilities if c not in ROLE_ABILITIES]
        if bad:
            raise HTTPException(400, f"未知能力选项: {bad}(可用: {', '.join(sorted(ROLE_ABILITIES))})")
        if not body.runtime_id.strip():
            raise HTTPException(400, "角色必须选择 runtime(定义角色时固定执行组合)")
        backend = store.get_backend(body.runtime_id)
        if backend is None:
            raise HTTPException(400, f"runtime 不存在: {body.runtime_id}")
        if not backend.enabled:
            raise HTTPException(400, f"runtime {body.runtime_id} 已停用,"
                                     "请先在 Runtime 设置页启用")
        known_models = {str(m.get("name", "")) for m in backend.models}
        # 空模型 = 显式使用 CLI 默认,总是合法;非空才校验归属
        if body.model and known_models and body.model not in known_models:
            # 配置阶梯之外:再查 runtime 动态发现的模型目录(仿 Multica 从 runtime 取)
            if body.model not in set(ctx.discovered_models(backend)):
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
        project = ctx.must_project(project_id)
        if project.orchestrator_role_id == role_id:
            raise HTTPException(409, "不能删除项目主控角色；请先为项目选择其他主控")
        store.delete_role(project_id, role_id)
        store.audit("human", "role_deleted", detail=f"project={project_id} role={role_id}")
        return {"ok": True}
