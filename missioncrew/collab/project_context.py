"""统一装配项目准则、Skills 与文档库访问说明。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from .documents import DocumentLibrary
from ..core.config import projects_dir
from ..core.models import GuidelineDocument, Project, ProjectSkill


def guideline_context_path(project: Project) -> Path:
    """返回 Runtime 按需读取的准则全文 JSON 路径。"""
    return (projects_dir() / project.id / "runtime-context" / "guidelines.json").resolve()


def write_guideline_context(project: Project) -> Path:
    """原子物化已启用准则全文；公共 Prompt 只携带摘要和内容版本。"""
    path = guideline_context_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc in project.guidelines:
        if not doc.enabled:
            continue
        rows.append({
            "id": doc.id,
            "title": doc.title,
            "summary": doc.summary,
            "content_version": hashlib.sha256(doc.content.encode("utf-8")).hexdigest()[:16],
            "content": doc.content,
        })
    serialized = json.dumps({
        "schema_version": 1,
        "project_id": project.id,
        "guideline_count": len(rows),
        "guidelines": rows,
    }, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == serialized:
            return path
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".guidelines.", suffix=".tmp", delete=False) as handle:
        handle.write(serialized)
        temporary = Path(handle.name)
    temporary.replace(path)
    return path


def project_allowed_dirs(project: Project, library: DocumentLibrary) -> list[str]:
    """返回项目显式授权给 Runtime 的现存本地目录，解析后去重。"""
    found = []
    seen = set()
    guideline_dir = write_guideline_context(project).parent
    for raw in [*project.repo_paths(), str(library.root), str(guideline_dir)]:
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return found


def _render_guideline_summary(doc: GuidelineDocument) -> str:
    content_version = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()[:16]
    summary = " ".join(doc.summary.split()) or "（未填写摘要）"
    return (f"- `{doc.id}` · {doc.title or doc.id} · {summary} "
            f"· 内容版本 `{content_version}`")


def _render_skill(skill: ProjectSkill) -> str:
    head = skill.name or skill.id
    body = "\n\n".join(x for x in (
        skill.description.strip(), skill.instructions.strip(),
    ) if x)
    return f"## Skill: {head} (`{skill.id}`)\n{body}".rstrip()


def render_project_context(project: Project, library: DocumentLibrary) -> str:
    """生成聊天与结构化任务共用的项目上下文。"""
    guideline_file = write_guideline_context(project)
    legacy_guidelines = []
    if project.charter:
        legacy_guidelines.append(f"## 项目章程（兼容字段）\n{project.charter}")
    if project.dev_guidelines:
        legacy_guidelines.append(f"## 开发准则（兼容字段）\n{project.dev_guidelines}")
    guideline_summaries = [
        _render_guideline_summary(doc)
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
        "# 项目准则摘要\n"
        + ("\n".join(guideline_summaries) if guideline_summaries else "（未配置）")
        + f"\n完整准则 JSON：{guideline_file}\n"
        "也可通过环境变量 MISSIONCREW_GUIDELINES_FILE 获取该路径。"
        "先根据摘要判断相关性，仅在任务需要时读取对应条目的 content；不要预加载全部正文。",
        f"# 项目文档库\n目录：{library.root}\n"
        "所有角色可在该普通目录中读写文档；平台会在每次执行后记录 Git 版本。\n"
        "准则和 Skill 正文中的相对 Markdown 链接均以此目录为根；仅在任务需要时读取链接文件，不要预加载。",
        "# 项目可读写目录\n以下目录由项目资源列表显式授权，可直接读写：\n" + dirs_section,
        ("# 项目 Skills\n"
         + "\n\n".join(skills)) if skills else
        "# 项目 Skills\n（未配置）",
        "# 准则与 Skill 使用方式\n结合当前任务自行判断哪些条目适用；"
        "准则先看摘要、相关时再读全文，不要机械执行无关条目。",
    ])
