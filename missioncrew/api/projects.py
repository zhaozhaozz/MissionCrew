"""项目端点:创建、更新与删除。"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, HTTPException

from ..collab import board_sources
from ..collab.documents import archive_library, library_for
from ..collab.guidelines import replace_guideline_library
from ..collab.project_context import write_guideline_context
from ..collab.skills import materialize_project_skills, sync_project_skill_library
from ..core import seed as seed_mod
from ..core.models import DEFAULT_MAX_CHAIN_RUNS, Project
from .context import MENTION_ID_RE, ApiContext
from .schemas import ProjectInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.post("/api/projects")
    def save_project(body: ProjectInput):
        if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "项目 id 只能包含字母、数字、下划线、连字符")
        existing = store.get_project(body.id)
        data = body.model_dump()
        is_new = existing is None
        new_roles = None
        if is_new:
            try:
                new_roles = seed_mod.project_roles_from_templates(store, body.id)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc))
        # None = 保留现值(新项目取模板首项);空字符串 = 无主控模式
        if body.orchestrator_role_id is None:
            orchestrator = (existing.orchestrator_role_id if existing
                            else new_roles[0].id)
        else:
            orchestrator = body.orchestrator_role_id.strip()
        if orchestrator and existing:
            orchestrator_role = store.get_role(body.id, orchestrator)
            if orchestrator_role is None:
                raise HTTPException(400, f"主控角色不属于当前项目: @{orchestrator}")
            if not orchestrator_role.enabled:
                raise HTTPException(400, f"主控角色已停用，请先启用: @{orchestrator}")
        if (orchestrator and is_new
                and orchestrator not in {role.id for role in new_roles}):
            raise HTTPException(400, f"主控角色不属于全局角色模板: @{orchestrator}")
        data["orchestrator_role_id"] = orchestrator
        data["max_chain_runs"] = (body.max_chain_runs if body.max_chain_runs is not None
                                  else (existing.max_chain_runs if existing
                                        else DEFAULT_MAX_CHAIN_RUNS))
        data["repos"] = (body.repos if body.repos is not None else
                         ([r.__dict__ for r in existing.repos] if existing else []))
        data["dev_guidelines"] = (body.dev_guidelines if body.dev_guidelines is not None
                                  else (existing.dev_guidelines if existing else ""))
        # 总览返回的准则/Skill 是只带元信息与内容指纹的精简对象;整对象回传时
        # 按主键回填现有全文,避免把正文清空。引用不存在的条目按格式错误处理。
        def restore_thin_items(items, full_by_key, key, fingerprint_key,
                               content_keys, kind):
            resolved = []
            for item in items:
                thin = (isinstance(item, dict) and fingerprint_key in item
                        and not any(k in item for k in content_keys))
                if not thin:
                    resolved.append(item)
                    continue
                full = full_by_key.get(str(item.get(key, "")))
                if full is None:
                    raise HTTPException(
                        400, f"{kind}精简对象引用了不存在的条目,无法还原正文: "
                             f"{item.get(key)}")
                resolved.append({**full, "enabled": bool(item.get("enabled", True))})
            return resolved

        data["guidelines"] = (
            restore_thin_items(
                body.guidelines,
                {g.name: g.to_dict() for g in existing.guidelines} if existing else {},
                "name", "markdown_fingerprint", ("markdown", "content"), "准则")
            if body.guidelines is not None else
            ([g.to_dict() for g in existing.guidelines] if existing else []))
        data["skills"] = (
            restore_thin_items(
                body.skills,
                {s.id: dict(s.__dict__) for s in existing.skills} if existing else {},
                "id", "instructions_fingerprint", ("instructions",), "Skill")
            if body.skills is not None else
            ([s.__dict__ for s in existing.skills] if existing else []))
        data["resources"] = (body.resources if body.resources is not None else
                             (existing.resources if existing else []))
        data["required_env"] = (body.required_env if body.required_env is not None else
                                (existing.required_env if existing else None))
        data["task_auto_rules"] = (
            body.task_auto_rules if body.task_auto_rules is not None else
            ([asdict(rule) for rule in existing.task_auto_rules]
             if existing else []))
        if body.task_board_filters is not None:
            try:
                data["task_board_filters"] = board_sources.validate_filters(
                    body.task_board_filters)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        else:
            data["task_board_filters"] = (
                existing.task_board_filters if existing else [])
        if body.task_board_group_by is not None:
            try:
                data["task_board_group_by"] = board_sources.validate_group_by(
                    body.task_board_group_by)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        else:
            data["task_board_group_by"] = (
                existing.task_board_group_by if existing else "")
        try:
            project = Project.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"项目准则或 Skill 格式不合法: {exc}")
        for doc in project.guidelines:
            if not MENTION_ID_RE.fullmatch(doc.name):
                raise HTTPException(400, f"准则 name 不合法: {doc.name}")
        for skill in project.skills:
            if not MENTION_ID_RE.fullmatch(skill.id):
                raise HTTPException(400, f"Skill id 不合法: {skill.id}")
        if body.guidelines is not None:
            replace_guideline_library(project, actor="human")
        store.put_project(project)
        write_guideline_context(project)
        materialize_project_skills(project, overwrite=body.skills is not None)
        sync_project_skill_library(store, project)
        if is_new:  # 新项目复制当前全局角色模板并获得自己的 general 频道
            seed_mod.init_project(store, project.id, new_roles)
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
