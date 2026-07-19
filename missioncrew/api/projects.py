"""项目端点:创建/更新(含验证准则 YAML)与删除。"""
from __future__ import annotations

import yaml
from fastapi import FastAPI, HTTPException

from ..collab.documents import archive_library, library_for, safe_relative_path
from ..core import seed as seed_mod
from ..core.models import Project, Rule
from .context import MENTION_ID_RE, ApiContext
from .schemas import ProjectInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

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
