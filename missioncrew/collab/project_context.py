"""统一装配项目准则、文档库和按 Runtime 过滤的 Skills。"""
from __future__ import annotations

from pathlib import Path

from .documents import DocumentLibrary
from ..core.models import Backend, GuidelineDocument, Project, ProjectSkill

_MAX_REF_CHARS = 32_000
_MAX_CONTEXT_REF_CHARS = 96_000


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


def _render_refs(refs: list[str], library: DocumentLibrary, budget: list[int]) -> str:
    chunks = []
    for ref in refs:
        if budget[0] <= 0:
            chunks.append("- 其余文件引用因上下文大小限制未展开，可直接从文档库目录读取。")
            break
        try:
            content = library.read(ref)
        except (FileNotFoundError, UnicodeDecodeError, ValueError):
            chunks.append(f"### 引用文件: {ref}\n（文件不存在、不是 UTF-8 文本或路径无效）")
            continue
        excerpt = content[:min(_MAX_REF_CHARS, budget[0])]
        budget[0] -= len(excerpt)
        suffix = "\n（内容过长，已截断；完整文件请从文档库读取。）" if len(excerpt) < len(content) else ""
        chunks.append(f"### 引用文件: {ref}\n{excerpt}{suffix}")
    return "\n\n".join(chunks)


def _render_guideline(doc: GuidelineDocument, library: DocumentLibrary,
                      budget: list[int]) -> str:
    body = doc.content.strip()
    refs = _render_refs(doc.file_refs, library, budget)
    return f"## {doc.title or doc.id}\n" + "\n\n".join(x for x in (body, refs) if x)


def _render_skill(skill: ProjectSkill, backend: Backend, library: DocumentLibrary,
                  budget: list[int]) -> str:
    head = skill.name or skill.id
    body = "\n\n".join(x for x in (
        skill.description.strip(), skill.instructions_for(backend).strip(),
        _render_refs(skill.file_refs, library, budget),
    ) if x)
    return f"## Skill: {head} (`{skill.id}`)\n{body}".rstrip()


def render_project_context(project: Project, backend: Backend,
                           library: DocumentLibrary) -> str:
    """生成聊天与结构化任务共用的项目上下文。"""
    budget = [_MAX_CONTEXT_REF_CHARS]
    guidelines = []
    if project.charter:
        guidelines.append(f"## 项目章程（兼容字段）\n{project.charter}")
    if project.dev_guidelines:
        guidelines.append(f"## 开发准则（兼容字段）\n{project.dev_guidelines}")
    guidelines.extend(
        _render_guideline(doc, library, budget)
        for doc in project.guidelines if doc.enabled
    )
    skills = [
        _render_skill(skill, backend, library, budget)
        for skill in project.skills if skill.applies_to(backend)
    ]
    allowed_dirs = project_allowed_dirs(project, library)
    dirs_section = "\n".join(f"- {path}" for path in allowed_dirs) or "（无本地目录）"
    return "\n\n".join([
        f"# 项目上下文：{project.name}",
        ("# 项目准则文档\n" + "\n\n".join(guidelines))
        if guidelines else "# 项目准则文档\n（未配置）",
        f"# 项目文档库\n目录：{library.root}\n"
        "所有角色可在该普通目录中读写文档；平台会在每次执行后记录 Git 版本。",
        "# 项目可读写目录\n以下目录由项目资源列表显式授权，可直接读写：\n" + dirs_section,
        (f"# 当前 Runtime 可用 Skills（{backend.id}/{backend.adapter}）\n"
         + "\n\n".join(skills)) if skills else
        f"# 当前 Runtime 可用 Skills（{backend.id}/{backend.adapter}）\n（无匹配 Skill）",
    ])
