"""项目准则与 Skill 端点。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from ..collab.project_context import write_guideline_context
from ..collab.skills import (delete_project_skill, import_skill_folder,
                             import_skill_zip, project_skill_library_dir,
                             read_skill_file, save_project_skill,
                             save_project_skill_markdown, skill_library_info,
                             sync_project_skill_library)
from ..core.models import GuidelineDocument, ProjectSkill
from .context import MENTION_ID_RE, ApiContext
from .schemas import GuidelineInput, SkillFolderImport, SkillInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/projects/{project_id}/guidelines")
    def list_guidelines(project_id: str):
        return [g.to_dict() for g in ctx.must_project(project_id).guidelines]

    @app.post("/api/projects/{project_id}/guidelines")
    def save_guideline(project_id: str, body: GuidelineInput):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        try:
            guideline = GuidelineDocument.from_markdown(body.markdown, body.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not MENTION_ID_RE.fullmatch(guideline.name):
            raise HTTPException(400, "准则 name 只能包含字母、数字、下划线、连字符")
        original_name = (body.original_name or "").strip()
        if original_name and not MENTION_ID_RE.fullmatch(original_name):
            raise HTTPException(400, "原准则 name 无效")
        replaced_names = {guideline.name, original_name} - {""}
        project.guidelines = [g for g in project.guidelines if g.name not in replaced_names]
        project.guidelines.append(guideline)
        store.put_project(project)
        write_guideline_context(project)
        store.audit(actor, "guideline_saved",
                    detail=f"project={project_id} guideline={guideline.name}")
        return guideline.to_dict()

    @app.delete("/api/projects/{project_id}/guidelines/{guideline_name}")
    def delete_guideline(project_id: str, guideline_name: str,
                         actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        before = len(project.guidelines)
        project.guidelines = [g for g in project.guidelines if g.name != guideline_name]
        if len(project.guidelines) == before:
            raise HTTPException(404, "准则不存在")
        store.put_project(project)
        write_guideline_context(project)
        store.audit(actor, "guideline_deleted",
                    detail=f"project={project_id} guideline={guideline_name}")
        return {"ok": True}

    @app.get("/api/projects/{project_id}/skills")
    def list_skills(project_id: str):
        project = ctx.must_project(project_id)
        project, _ = sync_project_skill_library(store, project)
        return [s.__dict__ for s in project.skills]

    @app.get("/api/projects/{project_id}/skills/library")
    def get_skill_library(project_id: str):
        return skill_library_info(store, ctx.must_project(project_id))

    @app.post("/api/projects/{project_id}/skills")
    def save_skill(project_id: str, body: SkillInput):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        if not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "Skill id 只能包含字母、数字、下划线、连字符")
        try:
            if body.markdown is not None:
                return save_project_skill_markdown(
                    store, project, body.id, body.markdown,
                    enabled=body.enabled, actor=actor).__dict__
            skill = ProjectSkill(**body.model_dump(exclude={"actor_role_id", "markdown"}))
            return save_project_skill(store, project, skill, actor=actor).__dict__
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/skills/{skill_id}/file")
    def read_skill_file_endpoint(project_id: str, skill_id: str, path: str):
        project = ctx.must_project(project_id)
        if not MENTION_ID_RE.fullmatch(skill_id):
            raise HTTPException(400, "Skill id 不合法")
        directory = project_skill_library_dir(project.id) / skill_id
        if not directory.is_dir():
            raise HTTPException(404, "Skill 不存在")
        try:
            content, truncated = read_skill_file(directory, path)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"path": path, "content": content, "truncated": truncated}

    @app.post("/api/projects/{project_id}/skills/import-folder")
    def import_skills_from_folder(project_id: str, body: SkillFolderImport):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        try:
            return import_skill_folder(
                store, project, body.path, overwrite=body.overwrite, actor=actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{project_id}/skills/import-zip")
    async def import_skills_from_zip(project_id: str, request: Request,
                                     overwrite: bool = False,
                                     actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        try:
            return import_skill_zip(
                store, project, await request.body(), overwrite=overwrite, actor=actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{project_id}/skills/rescan")
    def rescan_skill_library(project_id: str,
                             actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        ctx.validate_orchestrator_actor(project, actor_role_id)
        return skill_library_info(store, project)

    @app.delete("/api/projects/{project_id}/skills/{skill_id}")
    def delete_skill(project_id: str, skill_id: str,
                     actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        if not MENTION_ID_RE.fullmatch(skill_id):
            raise HTTPException(400, "Skill id 不合法")
        try:
            archive = delete_project_skill(
                store, project, skill_id, actor=actor)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Skill 不存在") from exc
        return {"ok": True, "archive": archive}
