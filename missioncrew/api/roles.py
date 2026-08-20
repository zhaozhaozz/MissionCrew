"""角色端点:定义时固定 runtime/model 的角色 CRUD。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.recycle_bin import recycle_role
from ..core.models import ROLE_ABILITIES, Role
from ..runtime import runtime_manager
from .context import MENTION_ID_RE, ApiContext
from .schemas import (RoleEnabledInput, RoleImport, RoleInput, RoleReorder,
                      RoleTemplateImport, RoleTemplateInput, RoleTemplateReorder)


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    def validate_config(body: RoleInput | RoleTemplateInput) -> None:
        """项目角色与全局模板共用同一套固定执行组合校验。"""
        if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "角色 id 只能包含字母、数字、下划线、连字符")
        bad = [c for c in body.capabilities if c not in ROLE_ABILITIES]
        if bad:
            raise HTTPException(
                400, f"未知能力选项: {bad}(可用: {', '.join(sorted(ROLE_ABILITIES))})")
        if not body.runtime_id.strip():
            raise HTTPException(400, "角色必须选择 runtime(定义角色时固定执行组合)")
        backend = store.get_backend(body.runtime_id)
        if backend is None:
            raise HTTPException(400, f"runtime 不存在: {body.runtime_id}")
        if not backend.enabled:
            raise HTTPException(400, f"runtime {body.runtime_id} 已停用,请先在 Runtime 设置页启用")
        known_models = set(backend.models)
        # 空模型 = 显式使用 CLI 默认,总是合法;非空才校验归属。
        if body.model and known_models and body.model not in known_models:
            if body.model not in set(ctx.discovered_models(backend)):
                raise HTTPException(
                    400, f"模型 {body.model or '(CLI 默认)'} 不属于 runtime {body.runtime_id}")
        if body.effort:
            # 工具自报按模型档位时以它为准:grok 这类 CLI 对越界档位不报错而是
            # 静默回落,放过去用户看不到任何提示,只能在保存时拦住。
            allowed = runtime_manager.effort_options(
                backend, body.model, ctx.discovered_model_efforts(backend))
            if not allowed:
                raise HTTPException(400, f"runtime {body.runtime_id} 不支持 effort(推理力度)配置")
            if body.effort not in allowed:
                raise HTTPException(
                    400, f"模型 {body.model or '(CLI 默认)'} 的 effort "
                         f"必须是 {'/'.join(allowed)} 之一")

    def validate_import_roles(
            roles: list[RoleTemplateInput], existing_ids: set[str],
            overwrite_ids: list[str]) -> set[str]:
        """先完整校验批次，再写库；覆盖名单必须与当前数据一致。"""
        ids = [role.id for role in roles]
        if len(ids) != len(set(ids)):
            raise HTTPException(400, "导入列表包含重复角色 id")
        acknowledged = set(overwrite_ids)
        if not acknowledged <= set(ids):
            raise HTTPException(400, "overwrite_ids 只能包含本次导入的角色 id")
        conflicts = set(ids) & existing_ids
        unconfirmed = conflicts - acknowledged
        if unconfirmed:
            names = ", ".join(f"@{role_id}" for role_id in sorted(unconfirmed))
            raise HTTPException(409, f"以下角色会覆盖现有配置，请确认后重试: {names}")
        for role in roles:
            validate_config(role)
        return conflicts

    @app.get("/api/roles")
    def roles():
        return [r.to_dict() for r in store.list_roles()]

    @app.post("/api/roles")
    def save_role(body: RoleInput):
        validate_config(body)
        project = store.get_project(body.project_id)
        if project is None:
            raise HTTPException(400, f"项目不存在: {body.project_id}")
        data = body.model_dump()
        existing = store.get_role(body.project_id, body.id)
        # 临时启停只允许走独立端点；编辑名称、模型等字段时保留实时状态，
        # 避免旧表单保存时覆盖另一个页面刚完成的开关操作。
        data["enabled"] = existing.enabled if existing else True
        if data["sort_order"] is None:   # 编辑保留现有顺序;新角色排到项目末尾
            data["sort_order"] = existing.sort_order if existing else 10 + max(
                (r.sort_order for r in store.list_roles(body.project_id)), default=0)
        role = Role(**data)
        store.put_role(role)
        ctx.role_usage_linkage.role_updated(role, previous=existing)
        store.audit("human", "role_saved", detail=f"project={role.project_id} role={role.id}")
        return store.get_role(role.project_id, role.id).to_dict()

    @app.post("/api/roles/{role_id}/enabled")
    def set_role_enabled(role_id: str, body: RoleEnabledInput):
        project = ctx.must_project(body.project_id)
        role = store.get_role(project.id, role_id)
        if role is None:
            raise HTTPException(404, "角色不存在")
        if not body.enabled and project.orchestrator_role_id == role.id:
            raise HTTPException(409, "不能停用项目主控角色；请先为项目选择其他已启用主控")
        role = store.set_role_enabled_manually(
            project.id, role.id, body.enabled)
        store.audit(
            "human", "role_enabled_changed",
            detail=f"project={project.id} role={role.id} enabled={str(role.enabled).lower()}",
        )
        return role.to_dict()

    @app.post("/api/roles/reorder")
    def reorder_roles(body: RoleReorder):
        ctx.must_project(body.project_id)
        current = {r.id: r for r in store.list_roles(body.project_id)}
        if set(body.ids) != set(current) or len(body.ids) != len(current):
            raise HTTPException(400, "ids 必须恰好包含该项目的全部角色,不重不漏")
        for i, rid in enumerate(body.ids):
            current[rid].sort_order = (i + 1) * 10
            store.put_role(current[rid])
        store.audit("human", "roles_reordered",
                    detail=f"project={body.project_id} order={','.join(body.ids)}")
        return {"ok": True, "ids": body.ids}

    @app.post("/api/roles/import")
    def import_roles(body: RoleImport):
        project = ctx.must_project(body.project_id)
        current = {role.id: role for role in store.list_roles(project.id)}
        overwritten = validate_import_roles(
            body.roles, set(current), body.overwrite_ids)
        next_order = 10 + max((role.sort_order for role in current.values()), default=0)
        imported: list[str] = []
        for item in body.roles:
            previous = current.get(item.id)
            data = item.model_dump(exclude={"sort_order"})
            role = Role(
                **data,
                project_id=project.id,
                # 启停是项目的实时运行状态，不随配置文件迁移。
                enabled=previous.enabled if previous else True,
                sort_order=previous.sort_order if previous else next_order,
            )
            if previous is None:
                next_order += 10
            store.put_role(role)
            ctx.role_usage_linkage.role_updated(role, previous=previous)
            imported.append(role.id)
        store.audit(
            "human", "roles_imported",
            detail=(f"project={project.id} imported={','.join(imported)} "
                    f"overwritten={','.join(sorted(overwritten))}"),
        )
        return {
            "ok": True,
            "imported_ids": imported,
            "overwritten_ids": sorted(overwritten),
        }

    @app.delete("/api/roles/{role_id}")
    def delete_role(role_id: str, project_id: str):
        if store.get_role(project_id, role_id) is None:
            raise HTTPException(404, "角色不存在")
        project = ctx.must_project(project_id)
        if project.orchestrator_role_id == role_id:
            raise HTTPException(409, "不能删除项目主控角色；请先为项目选择其他主控")
        role = store.get_role(project_id, role_id)
        item = recycle_role(store, project, role, actor="human")
        return {"ok": True, "recycle_item": item}

    @app.get("/api/role-templates")
    def role_templates():
        return [role.to_dict() for role in store.list_role_templates()]

    @app.post("/api/role-templates")
    def save_role_template(body: RoleTemplateInput):
        validate_config(body)
        data = body.model_dump()
        data["project_id"] = ""
        if data["sort_order"] is None:
            existing = store.get_role_template(body.id)
            data["sort_order"] = existing.sort_order if existing else 10 + max(
                (role.sort_order for role in store.list_role_templates()), default=0)
        role = Role(**data)
        store.put_role_template(role)
        store.audit("human", "role_template_saved", detail=f"role={role.id}")
        return role.to_dict()

    @app.post("/api/role-templates/reorder")
    def reorder_role_templates(body: RoleTemplateReorder):
        current = {role.id: role for role in store.list_role_templates()}
        if set(body.ids) != set(current) or len(body.ids) != len(current):
            raise HTTPException(400, "ids 必须恰好包含全部全局角色模板,不重不漏")
        for i, role_id in enumerate(body.ids):
            current[role_id].sort_order = (i + 1) * 10
            store.put_role_template(current[role_id])
        store.audit("human", "role_templates_reordered",
                    detail=f"order={','.join(body.ids)}")
        return {"ok": True, "ids": body.ids}

    @app.post("/api/role-templates/import")
    def import_role_templates(body: RoleTemplateImport):
        current = {role.id: role for role in store.list_role_templates()}
        overwritten = validate_import_roles(
            body.roles, set(current), body.overwrite_ids)
        next_order = 10 + max((role.sort_order for role in current.values()), default=0)
        imported: list[str] = []
        for item in body.roles:
            previous = current.get(item.id)
            data = item.model_dump(exclude={"sort_order"})
            role = Role(
                **data,
                project_id="",
                sort_order=previous.sort_order if previous else next_order,
            )
            if previous is None:
                next_order += 10
            store.put_role_template(role)
            imported.append(role.id)
        store.audit(
            "human", "role_templates_imported",
            detail=(f"imported={','.join(imported)} "
                    f"overwritten={','.join(sorted(overwritten))}"),
        )
        return {
            "ok": True,
            "imported_ids": imported,
            "overwritten_ids": sorted(overwritten),
        }

    @app.delete("/api/role-templates/{role_id}")
    def delete_role_template(role_id: str):
        if store.get_role_template(role_id) is None:
            raise HTTPException(404, "全局角色模板不存在")
        if len(store.list_role_templates()) <= 1:
            raise HTTPException(409, "至少保留一个全局角色模板,用于新项目主控")
        store.delete_role_template(role_id)
        store.audit("human", "role_template_deleted", detail=f"role={role_id}")
        return {"ok": True}
