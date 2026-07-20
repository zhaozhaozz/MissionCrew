"""项目准则与 Skill 端点。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException

from ..collab.project_context import write_guideline_context
from ..core.models import GuidelineDocument, ProjectSkill
from .context import MENTION_ID_RE, ApiContext
from .schemas import GuidelineInput, SkillInput


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
        return [s.__dict__ for s in ctx.must_project(project_id).skills]

    @app.post("/api/projects/{project_id}/skills")
    def save_skill(project_id: str, body: SkillInput):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        if not MENTION_ID_RE.fullmatch(body.id):
            raise HTTPException(400, "Skill id 只能包含字母、数字、下划线、连字符")
        skill = ProjectSkill(**body.model_dump(exclude={"actor_role_id"}))
        project.skills = [s for s in project.skills if s.id != skill.id]
        project.skills.append(skill)
        store.put_project(project)
        store.audit(actor, "skill_saved", detail=f"project={project_id} skill={skill.id}")
        return skill.__dict__

    @app.delete("/api/projects/{project_id}/skills/{skill_id}")
    def delete_skill(project_id: str, skill_id: str,
                     actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        before = len(project.skills)
        project.skills = [s for s in project.skills if s.id != skill_id]
        if len(project.skills) == before:
            raise HTTPException(404, "Skill 不存在")
        store.put_project(project)
        store.audit(actor, "skill_deleted", detail=f"project={project_id} skill={skill_id}")
        return {"ok": True}
