"""统一装配项目准则、Skills 与文档库访问说明。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import threading

from .documents import DocumentLibrary, document_resource_url
from .resource_urls import (guideline_resource_url, missioncrew_project_url,
                            skill_resource_url)
from .skills import (project_skill_library_dir, skill_context_dir,
                     skill_directory_version, write_skill_context)
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


def project_allowed_dirs(project: Project, library: DocumentLibrary,
                         workspace_dir: Path | None = None) -> list[str]:
    """返回项目显式授权给 Runtime 的现存本地目录，解析后去重。"""
    found = []
    seen = set()
    write_guideline_context(project)
    write_skill_context(project)
    context_dirs = ([str(workspace_dir), str(guideline_context_dir(project)),
                     str(skill_context_dir(project))]
                    if workspace_dir is not None
                    else [str(guideline_context_dir(project)),
                          str(skill_context_dir(project))])
    context_dirs.append(str(project_skill_library_dir(project.id)))
    for raw in [*project.repo_paths(), str(library.root), *context_dirs]:
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return found


def _guideline_content_version(doc: GuidelineDocument) -> str:
    return hashlib.sha256(doc.render_markdown().encode("utf-8")).hexdigest()[:16]


def _render_guideline_summary(project_id: str, doc: GuidelineDocument,
                              path: Path) -> str:
    # 索引行只含元信息:正文版本不进公共上下文,正文修改经本轮输入的
    # 「资源更新」提示告知,不触发完整重发(见 project_resource_versions)
    description = " ".join(doc.description.split()) or "（未填写 description）"
    return (f"- `{doc.name}` · {description} · 全文 `{path}` "
            f"· Web `{guideline_resource_url(project_id, doc.name)}`")


def _render_skill_summary(project_id: str, skill: ProjectSkill, path: Path,
                          canonical: Path) -> str:
    description = " ".join(skill.description.split()) or "（未填写 description）"
    return (f"- `{skill.id}` · {skill.name or skill.id} · {description} "
            f"· 入口 `{path}` "
            f"· Web `{skill_resource_url(project_id, skill.id)}`")


def project_resource_versions(project: Project) -> dict[str, str]:
    """已启用准则与 Skill 的正文版本表(键 `guideline:<name>` / `skill:<id>`)。

    会话按轮保存该表;下一轮比对得出哪些正文改了,只在本轮输入里提示一句,
    公共上下文(只含索引)的版本号不受正文影响。"""
    versions = {
        f"guideline:{doc.name}": _guideline_content_version(doc)
        for doc in project.guidelines if doc.enabled
    }
    canonical_skills = project_skill_library_dir(project.id)
    for skill in project.skills:
        if skill.enabled and (canonical_skills / skill.id).is_dir():
            versions[f"skill:{skill.id}"] = skill_directory_version(
                canonical_skills / skill.id)
    return versions


def render_project_context(project: Project, library: DocumentLibrary,
                           workspace_dir: Path | None = None,
                           extra_dirs: list[str] | None = None) -> str:
    """生成 Channel 角色共用的项目上下文。

    extra_dirs 是项目之外额外授权的目录(如 Runtime 工具自有目录),
    追加进"本次可读写目录"清单,让 Agent 知道可以使用。
    """
    canonical_guidelines = write_guideline_context(project)
    guideline_dir = (workspace_dir / "guidelines" if workspace_dir is not None
                     else guideline_context_dir(project))
    guideline_files = {
        name: guideline_dir / path.name for name, path in canonical_guidelines.items()
    }
    legacy_guidelines = []
    if project.charter:
        legacy_guidelines.append(f"## 项目章程（兼容字段）\n{project.charter}")
    if project.dev_guidelines:
        legacy_guidelines.append(f"## 开发准则（兼容字段）\n{project.dev_guidelines}")
    guideline_summaries = [
        _render_guideline_summary(project.id, doc, guideline_files[doc.name])
        for doc in project.guidelines if doc.enabled
    ]
    documents_dir = (workspace_dir / "documents" if workspace_dir is not None
                     else library.root)
    documents_url = document_resource_url(project.id)
    tasks_dir = workspace_dir / "tasks" if workspace_dir is not None else None
    skills_dir = workspace_dir / "skills" if workspace_dir is not None else None
    canonical_skills = project_skill_library_dir(project.id)
    skill_summaries = [
        _render_skill_summary(
            project.id,
            skill,
            (skills_dir / skill.id / "SKILL.md") if skills_dir is not None
            else canonical_skills / skill.id / "SKILL.md",
            canonical_skills / skill.id,
        )
        for skill in project.skills if skill.enabled
    ]
    allowed_dirs = project_allowed_dirs(project, library, workspace_dir)
    for raw in extra_dirs or []:
        if raw and raw not in allowed_dirs:
            allowed_dirs.append(raw)
    project_url = missioncrew_project_url(project.id)
    dirs_section = "\n".join(f"- {path}" for path in allowed_dirs) or "（无本地目录）"
    temp_dir = workspace_dir / "temp" if workspace_dir is not None else None
    return "\n\n".join([
        "# MissionCrew 简介\n"
        "MissionCrew 是本地多 Agent harness，负责装配角色、Runtime/模型、项目上下文、"
        "共享资料以及 Channel 协作；Task 是进入 Channel 的 Issue，不是独立执行流程；"
        "它不是业务代码仓。",
        (f"# MissionCrew 工作区\n目录：{workspace_dir}\n"
         "也可通过环境变量 MISSIONCREW_WORKSPACE 获取。该 `.missioncrew` 目录位于"
         "业务代码仓之外，其中的文件不会进入业务源码或业务代码提交。"
         "项目文档和任务可按需在这里创建、编辑；业务源码仍在当前工作目录或项目代码仓中修改。"
         "协作草稿、报告和普通聊天产生的验证记录写入 `documents/`，不要写入 `tasks/`。")
        if workspace_dir is not None else
        "# MissionCrew 工作区\n（本次执行未提供独立工作区）",
        f"# 项目上下文：{project.name}",
        "# MissionCrew 资源 URL\n"
        f"项目资源前缀：{project_url}\n"
        "聊天中引用平台资源时使用可点击的 Markdown 链接：\n"
        f"- 频道：`{project_url}/channels/<频道 id>`\n"
        f"- 任务：`{project_url}/tasks/<任务 id>`\n"
        f"- 面板：`{project_url}/dashboards/<面板 id>`（内置任务看板 id 为 `tasks`）\n"
        f"- 准则：`{project_url}/guidelines/<name>`\n"
        f"- Skill：`{project_url}/skills/<id>`\n"
        f"- 文档：`{project_url}/documents/<文档库相对路径>`\n"
        f"- 回收站：`{project_url}/recycle-bin`\n"
        "这些 URL 是 Web 标识，不是文件路径；不要把内部 `.missioncrew` 路径或 `file://` 链接发到聊天中。",
        ("# 项目兼容准则\n" + "\n\n".join(legacy_guidelines))
        if legacy_guidelines else "# 项目兼容准则\n（未配置）",
        "# 项目准则索引\n"
        + ("\n".join(guideline_summaries) if guideline_summaries else "（未配置）")
        + f"\n准则 Markdown 目录：{guideline_dir}\n"
        "也可通过环境变量 MISSIONCREW_GUIDELINES_DIR 获取该目录。"
        "先根据 description 判断相关性，仅在任务需要时读取对应的 Markdown 文件；不要预加载全部正文。",
        f"# 项目文档库\n内部读写目录：{documents_dir}\n"
        f"对外资源 URL：{documents_url}/<文档库相对路径>\n"
        "所有角色可在该普通目录中读写正式文档、协作草稿、报告和普通聊天产生的验证记录；"
        "平台会在每次执行后记录 Git 版本。\n"
        "准则和 Skill 正文中的相对 Markdown 链接均以此目录为根；仅在任务需要时读取链接文件，不要预加载。\n"
        f"最终回复引用项目文档时必须写成 `[标题]({documents_url}/路径)`；"
        "不得输出内部读写目录、`.missioncrew` 真实路径或 `file://` 链接。",
        (f"# 项目任务文件\n目录：{tasks_dir}\n"
         "该目录只存放项目 Task 快照，不是草稿或报告目录。可读取全部 Task Markdown；"
         "快照对当前执行只读，创建、编辑、追加状态简报或删除 Task 必须显式调用 Agent Tool，"
         "不要直接创建、修改或删除 `tasks/*.md`。Task 包含标题、简介、正文、状态、"
         "标签和 Channel 绑定；每个 Task 至少绑定一个可用 Channel。回合结束只刷新快照，"
         "不会把文件修改同步回 Task。frontmatter 中的 status_briefs 是平台生成的只读历史。")
        if tasks_dir is not None else "# 项目任务文件\n（本次执行未物化）",
        "# 本次可读写目录\n以下本地代码仓和 MissionCrew workspace 已显式授权，可直接读写：\n"
        + dirs_section,
        "# 文件系统边界\n"
        "只能读写上面列出的目录及其子目录。不要探测或访问授权范围之外的路径，"
        "包括 `/tmp`、`/var/tmp`、其他项目目录、用户主目录中的未授权文件和未作为本地代码仓"
        "显式列出的 MissionCrew 源码目录；命令中的重定向、管道、后台日志和工具自动生成文件"
        "也必须遵守。"
        "若现有命令使用了外部路径，执行前先改写到授权目录，不要先尝试再等待权限批准。\n"
        + (f"临时文件优先放在当前业务仓已有的任务目录（如项目约定的 `.tmp/`、`.e2e/`）；"
           f"没有项目约定时使用 `{temp_dir}`，并在任务完成后清理。"
           if temp_dir is not None else
           "临时文件必须放在当前工作目录内符合项目约定的位置，并在任务完成后清理。"),
        "# 路径访问失败处理\n"
        "上面的可读写目录清单就是本轮完整授权边界。若工具报告 `external_directory`、"
        "`permission denied` 或其他硬路径拒绝：\n"
        "- 不要改为搜索共同父目录、兄弟目录或其他未列出的路径，也不要重复提交同一越界请求；\n"
        "- 先把目标路径与清单逐项对照，再改用清单内的精确绝对路径继续；不要为了定位文件而"
        "对清单外的父目录执行递归 glob；\n"
        "- 文档资源 URL `/resources/<项目>/documents/<相对路径>` 直接映射为"
        " `$MISSIONCREW_DOCUMENTS_DIR/<相对路径>`；不要猜测或搜索 `.missioncrew` 的物理位置；\n"
        "- 若完成任务确实依赖清单外的材料，停止该项访问并在结果中写明被拒绝的精确路径和"
        "已经尝试的授权根目录，交由主控或人类调整材料位置。",
        "# 项目 Skills\n"
        "MissionCrew 注入的项目 Skill 是额外能力，不替代当前代码仓原有的 Agent 配置"
        "或 Skill。代码仓内的 `AGENTS.md`、`CLAUDE.md` 以及 Runtime 原生支持的目录"
        "（如 `.agent/skills`、`.agents/skills`、`.claude/skills`）仍可按原生规则发现"
        "和使用；不要把 `MISSIONCREW_SKILLS_DIR` 当作唯一 Skill 来源。仓内文件仍须位于"
        "本次授权的代码仓目录中。\n"
        + (f"MissionCrew Skill 目录：{skills_dir}\n" if skills_dir is not None else "")
        + ("\n".join(skill_summaries) if skill_summaries else
           "（本项目未配置额外的 MissionCrew Skill）")
        + "\n先根据 description 判断相关性；需要时读取对应 SKILL.md，"
          "并以 Skill 目录为根解析 scripts/、references/、assets/ 等相对文件。",
        "# 准则与 Skill 使用方式\n结合当前任务自行判断哪些条目适用；"
        "准则先看 description、相关时再读全文，不要机械执行无关条目。",
    ])
