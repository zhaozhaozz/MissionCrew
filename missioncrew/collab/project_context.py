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


def _render_guideline_summary(doc: GuidelineDocument) -> str:
    # 索引行只含元信息:全文路径与 Web URL 按目录规则推导,一次说明;正文版本
    # 不进公共上下文,正文修改经本轮输入的「资源更新」提示告知,不触发完整重发
    description = " ".join(doc.description.split()) or "（未填写 description）"
    return f"- `{doc.name}` · {description}"


def _render_skill_summary(skill: ProjectSkill) -> str:
    description = " ".join(skill.description.split()) or "（未填写 description）"
    name = (skill.name or "").strip()
    if not name or name == skill.id or name == description:
        return f"- `{skill.id}` · {description}"
    return f"- `{skill.id}` · {name} · {description}"


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
    """生成 Channel 角色共用的三段:必须遵守的规则、项目资料索引、工作区一览。

    身份、工作流和 Agent Tool 由 chat.py 装配;这里只放与角色无关的项目规则。
    extra_dirs 是项目之外额外授权的目录(如 Runtime 工具自有目录)。
    """
    write_guideline_context(project)
    guideline_dir = (workspace_dir / "guidelines" if workspace_dir is not None
                     else guideline_context_dir(project))
    documents_dir = (workspace_dir / "documents" if workspace_dir is not None
                     else library.root)
    skills_dir = workspace_dir / "skills" if workspace_dir is not None else None
    documents_url = document_resource_url(project.id)
    project_url = missioncrew_project_url(project.id)
    allowed_dirs = project_allowed_dirs(project, library, workspace_dir)
    for raw in extra_dirs or []:
        if raw and raw not in allowed_dirs:
            allowed_dirs.append(raw)
    dirs_section = "\n".join(f"  - {path}" for path in allowed_dirs) or "  （无本地目录）"
    guideline_summaries = [
        _render_guideline_summary(doc) for doc in project.guidelines if doc.enabled
    ]
    skill_summaries = [
        _render_skill_summary(skill) for skill in project.skills if skill.enabled
    ]
    legacy = []
    if project.charter:
        legacy.append(f"## 项目章程\n{project.charter}")
    if project.dev_guidelines:
        legacy.append(f"## 开发准则\n{project.dev_guidelines}")

    rules = (
        "# 必须遵守\n"
        "- 可读写目录(以下本地代码仓和 MissionCrew 工作区已显式授权,可直接读写):\n"
        f"{dirs_section}\n"
        "- 文件系统边界:只能读写上面列出的目录及其子目录。不要探测或访问授权范围之外的路径,"
        "包括 `/tmp`、`/var/tmp`、其他项目目录、用户主目录中的未授权文件和未作为本地代码仓"
        "显式列出的 MissionCrew 源码目录;命令中的重定向、管道、后台日志和工具自动生成文件"
        "也必须遵守。若现有命令使用了外部路径,执行前先改写到授权目录,不要先尝试再等待权限批准。\n"
        "- 路径访问失败处理:上面的可读写目录清单就是本轮完整授权边界。若工具报告 `external_directory`、"
        "`permission denied` 或其他硬路径拒绝,不要改为搜索共同父目录、兄弟目录或其他未列出的路径,"
        "也不要重复提交同一越界请求;先把目标路径与清单逐项对照,再改用清单内的精确绝对路径继续。"
        f"文档资源 URL `{documents_url}/<相对路径>` 直接映射为 `$MISSIONCREW_DOCUMENTS_DIR/<相对路径>`,"
        "不要猜测或搜索 `.missioncrew` 的物理位置。若完成任务确实依赖清单外的材料,停止该项访问,"
        "在结果中写明被拒绝的精确路径和已经尝试的授权根目录,交由主控或人类调整。\n"
        "- 沙箱只读路径处理:清单内的路径若报 `Read-only file system`,且落在授权目录下的 `.git`、`.codex`、"
        "`.agents` 或 git worktree 对应的主仓 `.git/worktrees/<名字>`,那是 Runtime 沙箱对这些位置的只读保护,"
        "不是环境只读挂载,同目录其他文件仍可写。此时带一句理由申请提升权限,原样重跑同一命令,平台按审批"
        "策略处理并留下记录;不要据此判定环境只读而放弃,也不要改走远端 API、复制到别处再提交或其他绕过"
        "沙箱的做法;只有申请被拒绝时才按上一条停止并报告。\n"
        "- 引用规则:最终回复引用项目文档时必须写成 `[标题](" + documents_url + "/路径)`;"
        "不得输出内部读写目录、`.missioncrew` 真实路径或 `file://` 链接。其他资源的 URL:"
        f"频道 `{project_url}/channels/<频道 id>`、任务 `{project_url}/tasks/<任务 id>`、"
        f"面板 `{project_url}/dashboards/<面板 id>`(内置任务看板 id 为 `tasks`)、"
        f"准则 `{project_url}/guidelines/<name>`、Skill `{project_url}/skills/<id>`、"
        f"回收站 `{project_url}/recycle-bin`;这些 URL 是 Web 标识,不是文件路径。\n"
        "- 工作区文件不进业务仓:`.missioncrew` 工作区位于业务代码仓之外,其中的文件不会进入"
        "业务源码或业务代码提交;协作草稿、报告和普通聊天产生的验证记录写入 `documents/`,"
        "不要写入 `tasks/`,平台会在每次执行后为文档库记录 Git 版本。\n"
        "- Task 快照只读:`tasks/` 里的 Markdown 是平台生成的快照。Task 包含标题、简介、正文、状态、"
        "标签和 Channel 绑定,每个 Task 至少绑定一个可用 Channel;快照对当前执行只读,创建、编辑、"
        "追加状态简报或删除 Task 必须显式调用 Agent Tool,回合结束只刷新快照,不会把文件修改同步回 Task。\n"
        "- Skill 来源:MissionCrew 注入的项目 Skill 是额外能力,不替代当前代码仓原有的 Agent 配置或 Skill。"
        "代码仓内的 `AGENTS.md`、`CLAUDE.md` 以及 Runtime 原生支持的目录(如 `.agent/skills`、"
        "`.agents/skills`、`.claude/skills`)仍可按原生规则发现和使用;不要把 `MISSIONCREW_SKILLS_DIR` "
        "当作唯一 Skill 来源。仓内文件仍须位于本次授权的代码仓目录中。"
    )
    resources = (
        "# 项目资料\n"
        + ("\n\n".join(legacy) + "\n" if legacy else "")
        + f"## 准则索引(name · 适用场景;全文 `{guideline_dir}/<name>.md`,"
        f"环境变量 MISSIONCREW_GUIDELINES_DIR;Web `{project_url}/guidelines/<name>`)\n"
        + ("\n".join(guideline_summaries) if guideline_summaries else "（未配置）")
        + "\n## Skill 索引(id · 名称 · 适用场景;入口 "
        + (f"`{skills_dir}/<id>/SKILL.md`,环境变量 MISSIONCREW_SKILLS_DIR" if skills_dir is not None
           else "见 Skill 库")
        + f";Web `{project_url}/skills/<id>`)\n"
        + ("\n".join(skill_summaries) if skill_summaries else "（本项目未配置额外的 MissionCrew Skill）")
        + "\n先根据 description 判断相关性,仅在任务需要时读取全文;准则和 Skill 正文中的相对 Markdown "
        "链接以文档库目录为根,仅在任务需要时读取链接文件,不要预加载,不要机械执行无关条目;"
        "Skill 以其目录为根解析 scripts/、references/、assets/ 等相对文件。"
    )
    overview = (
        "# 工作区一览\n"
        + (f"根目录:`{workspace_dir}`(环境变量 MISSIONCREW_WORKSPACE);下面都是它的子项:\n"
           "- `documents/` 项目文档库读写入口(MISSIONCREW_DOCUMENTS_DIR)\n"
           "- `tasks/` Task 只读快照(MISSIONCREW_TASKS_DIR)\n"
           "- `guidelines/` 已启用准则全文(MISSIONCREW_GUIDELINES_DIR)\n"
           "- `skills/` 已启用 Skill(MISSIONCREW_SKILLS_DIR)\n"
           f"- `channel-history.json` 频道完整历史(MISSIONCREW_CHANNEL_HISTORY,"
           f"`{workspace_dir / 'channel-history.json'}`;执行角色为脱敏视图),"
           "仅在最近对话不足以完成任务时按需读取\n"
           f"- `manual.md` 完整手册(MISSIONCREW_MANUAL,`{workspace_dir / 'manual.md'}`):"
           "本区块只保留必须时刻遵守的规则,涉及文档发布、Task、频道、面板与数据源、准则/Skill 格式、"
           "自动化、回收站、配置页协作的操作细节,先读手册对应章节再动手\n"
           "- `project.md` 项目简介;`temp/` 临时目录\n"
           if workspace_dir is not None else
           f"（本次执行未提供独立工作区;文档库目录 `{documents_dir}`）\n")
        + "MissionCrew 是本地多 Agent harness(装配角色、Runtime/模型、项目上下文、共享资料与 "
        "Channel 协作的平台),Task 是进入 Channel 的 Issue,不是独立执行流程;它不是业务代码仓。"
    )
    return "\n\n".join([rules, resources, overview])
