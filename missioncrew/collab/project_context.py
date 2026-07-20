"""统一装配项目准则、Skills 与文档库访问说明。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import threading

from .documents import DocumentLibrary
from ..core.config import projects_dir
from ..core.models import GuidelineDocument, Project, ProjectSkill


_GUIDELINE_FILE_NAME_RE = re.compile(r"[\w-]+")
_GUIDELINE_CONTEXT_LOCKS: dict[str, threading.RLock] = {}
_GUIDELINE_CONTEXT_LOCKS_GUARD = threading.Lock()


def _guideline_context_lock(project_id: str) -> threading.RLock:
    with _GUIDELINE_CONTEXT_LOCKS_GUARD:
        return _GUIDELINE_CONTEXT_LOCKS.setdefault(project_id, threading.RLock())


def guideline_context_dir(project: Project) -> Path:
    """返回 Runtime 按需读取的准则 Markdown 目录。"""
    return (projects_dir() / project.id / "runtime-context" / "guidelines").resolve()


def _guideline_file_path(project: Project, doc: GuidelineDocument) -> Path:
    # API 会校验 name；哈希兜底旧持久化中的异常值，避免生成路径逃逸。
    filename = (doc.name if _GUIDELINE_FILE_NAME_RE.fullmatch(doc.name)
                else hashlib.sha256(doc.name.encode("utf-8")).hexdigest()[:16])
    return guideline_context_dir(project) / f"{filename}.md"


def _atomic_write_text(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except FileNotFoundError:
        pass
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".guideline.", suffix=".tmp", delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_guideline_context(project: Project) -> dict[str, Path]:
    """把每篇已启用准则原子物化为带 Skill 风格 frontmatter 的 Markdown。"""
    directory = guideline_context_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    with _guideline_context_lock(project.id):
        paths = {
            doc.name: _guideline_file_path(project, doc)
            for doc in project.guidelines if doc.enabled
        }
        for doc in project.guidelines:
            if doc.enabled:
                _atomic_write_text(paths[doc.name], doc.render_markdown())

        expected = {path.resolve() for path in paths.values()}
        for stale in directory.glob("*.md"):
            if stale.resolve() not in expected:
                stale.unlink()
        for temporary in directory.glob(".guideline.*.tmp"):
            temporary.unlink()

        # f77c010 的短期 JSON 物化格式不再使用；目录只保留 Markdown。
        obsolete_json = directory.parent / "guidelines.json"
        obsolete_json.unlink(missing_ok=True)
        return paths


def project_allowed_dirs(project: Project, library: DocumentLibrary) -> list[str]:
    """返回项目显式授权给 Runtime 的现存本地目录，解析后去重。"""
    found = []
    seen = set()
    write_guideline_context(project)
    guideline_dir = guideline_context_dir(project)
    for raw in [*project.repo_paths(), str(library.root), str(guideline_dir)]:
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return found


def _render_guideline_summary(doc: GuidelineDocument, path: Path) -> str:
    content_version = hashlib.sha256(
        doc.render_markdown().encode("utf-8")).hexdigest()[:16]
    description = " ".join(doc.description.split()) or "（未填写 description）"
    return (f"- `{doc.name}` · {description} "
            f"· 内容版本 `{content_version}` · 全文 `{path}`")


def _render_skill(skill: ProjectSkill) -> str:
    head = skill.name or skill.id
    body = "\n\n".join(x for x in (
        skill.description.strip(), skill.instructions.strip(),
    ) if x)
    return f"## Skill: {head} (`{skill.id}`)\n{body}".rstrip()


def render_project_context(project: Project, library: DocumentLibrary) -> str:
    """生成聊天与结构化任务共用的项目上下文。"""
    guideline_files = write_guideline_context(project)
    legacy_guidelines = []
    if project.charter:
        legacy_guidelines.append(f"## 项目章程（兼容字段）\n{project.charter}")
    if project.dev_guidelines:
        legacy_guidelines.append(f"## 开发准则（兼容字段）\n{project.dev_guidelines}")
    guideline_summaries = [
        _render_guideline_summary(doc, guideline_files[doc.name])
        for doc in project.guidelines if doc.enabled
    ]
    skills = [
        _render_skill(skill)
        for skill in project.skills if skill.enabled
    ]
    allowed_dirs = project_allowed_dirs(project, library)
    dirs_section = "\n".join(f"- {path}" for path in allowed_dirs) or "（无本地目录）"
    return "\n\n".join([
        f"# 项目上下文：{project.name}",
        ("# 项目兼容准则\n" + "\n\n".join(legacy_guidelines))
        if legacy_guidelines else "# 项目兼容准则\n（未配置）",
        "# 项目准则索引\n"
        + ("\n".join(guideline_summaries) if guideline_summaries else "（未配置）")
        + f"\n准则 Markdown 目录：{guideline_context_dir(project)}\n"
        "也可通过环境变量 MISSIONCREW_GUIDELINES_DIR 获取该目录。"
        "先根据 description 判断相关性，仅在任务需要时读取对应的 Markdown 文件；不要预加载全部正文。",
        f"# 项目文档库\n目录：{library.root}\n"
        "所有角色可在该普通目录中读写文档；平台会在每次执行后记录 Git 版本。\n"
        "准则和 Skill 正文中的相对 Markdown 链接均以此目录为根；仅在任务需要时读取链接文件，不要预加载。",
        "# 项目可读写目录\n以下目录由项目资源列表显式授权，可直接读写：\n" + dirs_section,
        ("# 项目 Skills\n"
         + "\n\n".join(skills)) if skills else
        "# 项目 Skills\n（未配置）",
        "# 准则与 Skill 使用方式\n结合当前任务自行判断哪些条目适用；"
        "准则先看 description、相关时再读全文，不要机械执行无关条目。",
    ])
