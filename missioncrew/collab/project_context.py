"""统一装配项目准则、Skills 与文档库访问说明。"""
from __future__ import annotations

from pathlib import Path

from .documents import DocumentLibrary
from ..core.models import GuidelineDocument, Project, ProjectSkill


def project_allowed_dirs(project: Project, library: DocumentLibrary) -> list[str]:
    """返回项目显式授权给 Runtime 的现存本地目录，解析后去重。"""
    found = []
    seen = set()
    for raw in [*project.repo_paths(), str(library.root)]:
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return found


def _render_guideline(doc: GuidelineDocument) -> str:
    return f"## {doc.title or doc.id}\n{doc.content.strip()}".rstrip()


def _render_skill(skill: ProjectSkill) -> str:
    head = skill.name or skill.id
    body = "\n\n".join(x for x in (
        skill.description.strip(), skill.instructions.strip(),
    ) if x)
    return f"## Skill: {head} (`{skill.id}`)\n{body}".rstrip()


def render_project_context(project: Project, library: DocumentLibrary) -> str:
    """生成聊天与结构化任务共用的项目上下文。"""
    guidelines = []
    if project.charter:
        guidelines.append(f"## 项目章程（兼容字段）\n{project.charter}")
    if project.dev_guidelines:
        guidelines.append(f"## 开发准则（兼容字段）\n{project.dev_guidelines}")
    guidelines.extend(
        _render_guideline(doc)
        for doc in project.guidelines if doc.enabled
    )
    skills = [
        _render_skill(skill)
        for skill in project.skills if skill.enabled
    ]
    allowed_dirs = project_allowed_dirs(project, library)
    dirs_section = "\n".join(f"- {path}" for path in allowed_dirs) or "（无本地目录）"
    return "\n\n".join([
        f"# 项目上下文：{project.name}",
        ("# 项目准则文档\n" + "\n\n".join(guidelines))
        if guidelines else "# 项目准则文档\n（未配置）",
        f"# 项目文档库\n目录：{library.root}\n"
        "所有角色可在该普通目录中读写文档；平台会在每次执行后记录 Git 版本。\n"
        "准则和 Skill 正文中的相对 Markdown 链接均以此目录为根；仅在任务需要时读取链接文件，不要预加载。",
        "# 项目可读写目录\n以下目录由项目资源列表显式授权，可直接读写：\n" + dirs_section,
        ("# 项目 Skills\n"
         + "\n\n".join(skills)) if skills else
        "# 项目 Skills\n（未配置）",
        "# 准则与 Skill 使用方式\n结合当前任务自行判断哪些条目适用；不要机械执行无关条目。",
    ])
