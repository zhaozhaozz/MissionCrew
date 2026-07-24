"""项目准则与 Skill 端点。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from ..collab.content_channels import ensure_content_channel, rebind_content_channel
from ..collab.guidelines import (_GUIDELINE_NAME_RE, guideline_history,
                                 read_guideline_version,
                                 restore_guideline,
                                 save_guideline as save_guideline_document,
                                 sync_guideline_library)
from ..collab.recycle_bin import recycle_guideline, recycle_skill
from ..collab.resource_urls import guideline_resource_url, skill_resource_url
from ..collab.skills import (import_skill_folder, import_skill_zip,
                             project_skill_library_dir,
                             read_skill_file, save_project_skill,
                             save_project_skill_markdown, skill_library_info,
                             sync_project_skill_library)
from ..core.models import ProjectSkill
from .context import MENTION_ID_RE, ApiContext
from .diffutil import (_NonTextDocumentError, _decode_pure_text,
                       build_diff_ops, build_text_diff)
from .schemas import (GuidelineCompare, GuidelineInput, GuidelineRestore,
                      SkillFolderImport, SkillInput)


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/projects/{project_id}/guidelines")
    def list_guidelines(project_id: str):
        project = ctx.must_project(project_id)
        sync_guideline_library(store, project)
        for guideline in project.guidelines:
            ensure_content_channel(
                store, project, "guidelines", guideline.name, guideline.name)
        return [{**g.to_dict(),
                 "resource_url": guideline_resource_url(project_id, g.name)}
                for g in project.guidelines]

    @app.post("/api/projects/{project_id}/guidelines")
    def save_guideline(project_id: str, body: GuidelineInput):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        try:
            guideline, revision = save_guideline_document(
                store, project, body.markdown, enabled=body.enabled, actor=actor,
                original_name=(body.original_name or "").strip())
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        rebind_content_channel(
            store, project, "guidelines", body.original_name or "",
            guideline.name, guideline.name)
        ensure_content_channel(
            store, project, "guidelines", guideline.name, guideline.name)
        return {**guideline.to_dict(),
                "resource_url": guideline_resource_url(project_id, guideline.name),
                "revision": revision}

    @app.get("/api/projects/{project_id}/guidelines/{guideline_name}/history")
    def list_guideline_history(project_id: str, guideline_name: str,
                               limit: int = 100):
        project = ctx.must_project(project_id)
        try:
            return guideline_history(store, project, guideline_name, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/projects/{project_id}/guidelines/{guideline_name}/history/{revision}")
    def get_guideline_history_version(project_id: str, guideline_name: str,
                                      revision: str):
        project = ctx.must_project(project_id)
        try:
            return read_guideline_version(
                store, project, guideline_name, revision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/projects/{project_id}/guidelines/{guideline_name}/compare")
    def compare_guideline_versions(project_id: str, guideline_name: str,
                                   body: GuidelineCompare):
        """比较同一准则的两个历史版本，返回 unified diff 与行级操作。"""
        project = ctx.must_project(project_id)
        if not _GUIDELINE_NAME_RE.fullmatch(guideline_name):
            raise HTTPException(400, "准则 name 只能包含字母、数字、下划线、连字符")
        if not any(item.name == guideline_name for item in project.guidelines):
            raise HTTPException(404, "准则不存在")
        library = sync_guideline_library(store, project)
        path = f"{guideline_name}.md"
        try:
            before = _decode_pure_text(
                library.read_history(path, body.from_revision).encode("utf-8"))
            after = _decode_pure_text(
                library.read_history(path, body.to_revision).encode("utf-8"))
        except (UnicodeDecodeError, _NonTextDocumentError) as exc:
            raise HTTPException(415, "二进制或非 UTF-8 内容不能比较版本") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        label_a = f"{path}@{body.from_revision[:10]}"
        label_b = f"{path}@{body.to_revision[:10]}"
        return {
            "guideline": guideline_name,
            "from_revision": body.from_revision,
            "to_revision": body.to_revision,
            **build_text_diff(before, after, label_a, label_b),
            "ops": build_diff_ops(before, after),
        }

    @app.post("/api/projects/{project_id}/guidelines/{guideline_name}/restore")
    def restore_guideline_version(project_id: str, guideline_name: str,
                                  body: GuidelineRestore):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, body.actor_role_id)
        try:
            guideline, restored = restore_guideline(
                store, project, guideline_name, body.revision, actor=actor)
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        store.audit(
            actor, "guideline_restored",
            detail=(f"project={project_id} guideline={guideline.name} "
                    f"from={body.revision[:10]} revision={restored[:10]}"),
        )
        return {**guideline.to_dict(),
                "resource_url": guideline_resource_url(project_id, guideline.name),
                "revision": restored}

    @app.delete("/api/projects/{project_id}/guidelines/{guideline_name}")
    def delete_guideline(project_id: str, guideline_name: str,
                         actor_role_id: Optional[str] = None):
        project = ctx.must_project(project_id)
        actor = ctx.validate_orchestrator_actor(project, actor_role_id)
        try:
            item = recycle_guideline(
                store, project, guideline_name, actor=actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "revision": item["revision"],
                "recycle_item": item}

    @app.get("/api/projects/{project_id}/skills")
    def list_skills(project_id: str):
        project = ctx.must_project(project_id)
        project, _ = sync_project_skill_library(store, project)
        for skill in project.skills:
            ensure_content_channel(store, project, "skills", skill.id, skill.name or skill.id)
        return [{**s.__dict__, "resource_url": skill_resource_url(project_id, s.id)}
                for s in project.skills]

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
                saved = save_project_skill_markdown(
                    store, project, body.id, body.markdown,
                    enabled=body.enabled, actor=actor)
                ensure_content_channel(
                    store, project, "skills", saved.id, saved.name or saved.id)
                return {**saved.__dict__,
                        "resource_url": skill_resource_url(project_id, saved.id)}
            skill = ProjectSkill(**body.model_dump(exclude={"actor_role_id", "markdown"}))
            saved = save_project_skill(store, project, skill, actor=actor)
            ensure_content_channel(
                store, project, "skills", saved.id, saved.name or saved.id)
            return {**saved.__dict__,
                    "resource_url": skill_resource_url(project_id, saved.id)}
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
        return {"path": path, "content": content, "truncated": truncated,
                "resource_url": skill_resource_url(project_id, skill_id, path)}

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
            item = recycle_skill(store, project, skill_id, actor=actor)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Skill 不存在") from exc
        return {"ok": True, "recycle_item": item}
