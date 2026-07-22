"""准则 Markdown 的 Git 版本存储、项目索引与 Runtime 快照同步。"""
from __future__ import annotations

import re
import threading

from .documents import GuidelineLibrary, guideline_library_for
from .project_context import write_guideline_context
from ..core.models import GuidelineDocument, Project
from ..core.store import Store


_GUIDELINE_NAME_RE = re.compile(r"[\w-]+")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _project_lock(project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


def _validate_name(name: str, label: str = "准则 name") -> str:
    value = str(name or "").strip()
    if not _GUIDELINE_NAME_RE.fullmatch(value):
        raise ValueError(f"{label} 只能包含字母、数字、下划线、连字符")
    return value


def _path(name: str) -> str:
    return f"{_validate_name(name)}.md"


def _read_current(library: GuidelineLibrary, name: str) -> str | None:
    try:
        return library.read(_path(name))
    except FileNotFoundError:
        return None


def sync_guideline_library(store: Store, project: Project) -> GuidelineLibrary:
    """迁移旧数据，之后以 Git 工作树为 Markdown 事实源刷新项目索引。"""
    library = guideline_library_for(project.id)
    with _project_lock(project.id):
        markdown_files = [item for item in library.list_files()
                          if item["path"].endswith(".md") and "/" not in item["path"]]
        if not markdown_files and project.guidelines:
            # 一次性迁移：旧项目只有 Project JSON，没有版本化准则文件。
            for guideline in project.guidelines:
                library.write(
                    _path(guideline.name), guideline.render_markdown(),
                    actor="platform",
                    message=f"Import existing guideline {guideline.name}",
                )
            return library

        enabled_by_name = {item.name: item.enabled for item in project.guidelines}
        indexed: list[GuidelineDocument] = []
        for item in markdown_files:
            markdown = library.read(item["path"])
            guideline = GuidelineDocument.from_markdown(markdown)
            expected_path = _path(guideline.name)
            if item["path"] != expected_path:
                raise ValueError(
                    f"准则文件名 {item['path']} 与 frontmatter name {guideline.name} 不一致")
            guideline.enabled = enabled_by_name.get(guideline.name, True)
            indexed.append(guideline)
        if ([item.to_dict() for item in indexed]
                != [item.to_dict() for item in project.guidelines]):
            project.guidelines = indexed
            store.put_project(project)
            write_guideline_context(project)
    return library


def sync_all_guideline_libraries(store: Store) -> None:
    for project in store.list_projects():
        sync_guideline_library(store, project)


def replace_guideline_library(project: Project, *, actor: str = "human") -> None:
    """兼容项目整对象 API：把显式传入的准则集合写回同一 Git 文件库。"""
    library = guideline_library_for(project.id)
    with _project_lock(project.id):
        desired = {_path(item.name): item.render_markdown()
                   for item in project.guidelines}
        existing = {item["path"] for item in library.list_files()
                    if item["path"].endswith(".md") and "/" not in item["path"]}
        for path in sorted(existing - set(desired)):
            library.delete(path, actor=actor)
        for path, markdown in desired.items():
            if _read_current(library, path.removesuffix(".md")) != markdown:
                library.write(
                    path, markdown, actor=actor,
                    message=f"Update guideline {path.removesuffix('.md')}",
                )


def save_guideline(store: Store, project: Project, markdown: str, *, enabled: bool,
                   actor: str, original_name: str = "", message: str = ""
                   ) -> tuple[GuidelineDocument, str]:
    """保存准则 Markdown；重命名和内容修改均使用共享 Git 文件库。"""
    guideline = GuidelineDocument.from_markdown(markdown, enabled)
    _validate_name(guideline.name)
    original_name = (_validate_name(original_name, "原准则 name")
                     if original_name else "")
    with _project_lock(project.id):
        current = store.get_project(project.id) or project
        library = sync_guideline_library(store, current)
        existing_names = {item.name for item in current.guidelines}
        if (original_name and original_name != guideline.name
                and guideline.name in existing_names):
            raise FileExistsError(f"准则已存在: {guideline.name}")

        source_name = original_name or guideline.name
        if source_name != guideline.name and _read_current(library, source_name) is not None:
            library.rename(
                _path(source_name), _path(guideline.name), actor=actor)

        rendered = guideline.render_markdown()
        revision = ""
        if _read_current(library, guideline.name) != rendered:
            revision = library.write(
                _path(guideline.name), rendered, actor=actor,
                message=(message or f"Update guideline {guideline.name}"),
            )
        if not revision:
            history = library.history(_path(guideline.name), 1)
            revision = history[0]["revision"] if history else ""

        replaced_names = {guideline.name, original_name} - {""}
        current.guidelines = [item for item in current.guidelines
                              if item.name not in replaced_names]
        current.guidelines.append(guideline)
        store.put_project(current)
        write_guideline_context(current)
        store.audit(
            actor, "guideline_saved",
            detail=(f"project={current.id} guideline={guideline.name} "
                    f"revision={revision[:10]}"),
        )
        return guideline, revision


def guideline_history(store: Store, project: Project, guideline_name: str,
                      limit: int = 100) -> list[dict]:
    name = _validate_name(guideline_name)
    if not any(item.name == name for item in project.guidelines):
        raise FileNotFoundError("准则不存在")
    library = sync_guideline_library(store, project)
    return library.history(_path(name), limit)


def read_guideline_version(store: Store, project: Project, guideline_name: str,
                           revision: str) -> dict:
    name = _validate_name(guideline_name)
    current = next((item for item in project.guidelines if item.name == name), None)
    if current is None:
        raise FileNotFoundError("准则不存在")
    library = sync_guideline_library(store, project)
    markdown = library.read_history(_path(name), revision)
    parsed = GuidelineDocument.from_markdown(markdown, current.enabled)
    row = next((item for item in library.history(_path(name), 500)
                if item["revision"] == revision), None)
    if row is None:
        raise FileNotFoundError(f"准则版本不存在: {revision}")
    return {**row, **parsed.to_dict()}


def restore_guideline(store: Store, project: Project, guideline_name: str,
                      revision: str, *, actor: str
                      ) -> tuple[GuidelineDocument, str]:
    current = next((item for item in project.guidelines
                    if item.name == guideline_name), None)
    if current is None:
        raise FileNotFoundError("准则不存在")
    version = read_guideline_version(
        store, project, guideline_name, revision)
    return save_guideline(
        store, project, version["markdown"], enabled=current.enabled,
        actor=actor, original_name=guideline_name,
        message=f"Restore guideline {guideline_name} to {revision[:10]}",
    )


def delete_guideline(store: Store, project: Project, guideline_name: str,
                     *, actor: str) -> str:
    name = _validate_name(guideline_name)
    with _project_lock(project.id):
        current = store.get_project(project.id) or project
        if not any(item.name == name for item in current.guidelines):
            raise FileNotFoundError("准则不存在")
        library = sync_guideline_library(store, current)
        revision = library.delete(_path(name), actor=actor)
        current.guidelines = [item for item in current.guidelines if item.name != name]
        store.put_project(current)
        write_guideline_context(current)
        store.audit(
            actor, "guideline_deleted",
            detail=(f"project={project.id} guideline={name} "
                    f"revision={revision[:10]}"),
        )
        return revision
