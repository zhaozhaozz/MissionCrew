"""统一装配项目准则、Skills 与文档库访问说明。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import threading

from .credentials import credential_context_dir
from .github import github_cli_dir
from .documents import DocumentLibrary, document_resource_url
from .platform_docs import platform_docs_dir
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
    if workspace_dir is not None:
        context_dirs.append(str(credential_context_dir(project.id)))   # 工作区 credentials/ 的链接目标
        # 项目有 GitHub 身份时 gh 从这里读登录状态(GH_CONFIG_DIR);没有时目录不存在,下面自动跳过
        context_dirs.append(str(github_cli_dir(project.id)))
    if workspace_dir is not None:
        context_dirs.append(str(platform_docs_dir()))   # 工作区 platform-docs/ 的链接目标
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
    description = " ".join(doc.description.split()) or "(no description)"
    return f"- `{doc.name}` · {description}"


def _render_skill_summary(skill: ProjectSkill) -> str:
    description = " ".join(skill.description.split()) or "(no description)"
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
    dirs_section = "\n".join(f"  - {path}" for path in allowed_dirs) or "  (no local directories)"
    guideline_summaries = [
        _render_guideline_summary(doc) for doc in project.guidelines if doc.enabled
    ]
    skill_summaries = [
        _render_skill_summary(skill) for skill in project.skills if skill.enabled
    ]
    legacy = []
    if project.charter:
        legacy.append(f"## Project Charter\n{project.charter}")
    if project.dev_guidelines:
        legacy.append(f"## Development Guidelines\n{project.dev_guidelines}")

    rules = (
        "# Must Follow\n"
        "- Read/write directories (these local repositories and MissionCrew workspace paths are "
        "explicitly authorized for direct reading and writing):\n"
        f"{dirs_section}\n"
        "- File system boundary: read and write only the directories listed above and their "
        "subdirectories. Do not probe or access paths outside them, including `/tmp`, `/var/tmp`, "
        "other project directories, unauthorized files in the user's home directory, and MissionCrew "
        "source directories not explicitly listed as local repositories; redirections, pipes, "
        "background logs and files generated by tools in your commands must follow this too. If an "
        "existing command uses an outside path, rewrite it to an authorized directory before running "
        "it; do not try first and then wait for permission approval.\n"
        "- Path access failures: the read/write list above is the complete authorization boundary for "
        "this turn. If a tool reports `external_directory`, `permission denied` or another hard path "
        "denial, do not switch to searching common parent directories, sibling directories or other "
        "unlisted paths, and do not resubmit the same out-of-bounds request; compare the target path "
        "against the list entry by entry, then continue with the exact absolute path from the list. "
        f"The document resource URL `{documents_url}/<relative path>` maps directly to "
        "`$MISSIONCREW_DOCUMENTS_DIR/<relative path>`; do not guess or search for the physical "
        "location of `.missioncrew`. If the task truly depends on material outside the list, stop "
        "accessing it and state in your result the exact denied path and the authorized roots you "
        "tried, so the orchestrator or a human can adjust.\n"
        "- Sandbox read-only paths: if a listed path reports `Read-only file system` and lies in "
        "`.git`, `.codex` or `.agents` under an authorized directory, or in the main repository's "
        "`.git/worktrees/<name>` for a git worktree, that is the runtime sandbox's read-only "
        "protection for those locations, not a read-only mount of the environment; other files in "
        "the same directory stay writable. Request elevated permission with a one-sentence reason and "
        "rerun the same command unchanged; the platform handles it under its approval policy and keeps "
        "a record. Do not conclude that the environment is read-only and give up; only if the request "
        "is denied, stop and report as described above.\n"
        # 多角色(与人类)可能同时改同一个仓:改代码一律进自己的 worktree,互不踩 checkout
        "- Code changes go in your own git worktree: several roles and humans may work in the same "
        "repository at the same time, so never edit files in a shared checkout directly. Before "
        "changing files in a git repository, create a worktree on a new branch (`git worktree add "
        "-b <branch> <path>`) and make all edits, builds and commits there; follow the repository's "
        "own worktree convention if it has one, otherwise place it at "
        "`<repository>/.worktrees/<name>` so it stays inside the authorized directories. If the "
        "working directory is already a worktree created for this task, keep working in it. Reading "
        "code and running checks that change no files need no worktree. State the worktree path and "
        "branch in your final reply; committing, merging back and cleanup follow the brief and the "
        "project guidelines (details in section 3 of the manual).\n"
        "- Linking: a final reply that references project documents must write them as "
        "`[title](" + documents_url + "/path)`; never output internal read/write directories, the "
        "real `.missioncrew` path or `file://` links. URLs of other resources: channel "
        f"`{project_url}/channels/<channel id>`, task `{project_url}/tasks/<task id>`, "
        f"dashboard `{project_url}/dashboards/<dashboard id>` (the built-in task board id is "
        f"`tasks`), guideline `{project_url}/guidelines/<name>`, skill `{project_url}/skills/<id>`, "
        f"recycle bin `{project_url}/recycle-bin`, project settings `{project_url}/settings`; "
        "these URLs are web identifiers, not file paths.\n"
        "- Workspace files stay out of business repositories: the `.missioncrew` workspace lives "
        "outside the business repositories, and its files never enter business source code or "
        "commits. Write collaboration drafts, reports and verification records from ordinary chats "
        "into `documents/`, not `tasks/`; the platform records a Git version of the document library "
        "after every run.\n"
        "- Task snapshots are read-only: the Markdown files in `tasks/` are snapshots generated by the "
        "platform, refreshed when a turn is assembled and right after your own `task.*` calls; file "
        "edits are never synced back to Tasks. Creating, editing, appending status briefs to or "
        "deleting a Task must explicitly call the Agent Tool.\n"
        "- Skill sources: project skills injected by MissionCrew are extra capabilities and do not "
        "replace the repository's own agent configuration or skills. The repository's `AGENTS.md`, "
        "`CLAUDE.md` and the directories the runtime natively supports (such as `.agent/skills`, "
        "`.agents/skills`, `.claude/skills`) can still be discovered and used under their native "
        "rules; do not treat `MISSIONCREW_SKILLS_DIR` as the only skill source. Files in the "
        "repository must still lie within the repository directories authorized for this run."
    )
    resources = (
        "# Project Materials\n"
        + ("\n\n".join(legacy) + "\n" if legacy else "")
        + f"## Guideline Index (name · when to use; full text `{guideline_dir}/<name>.md`, "
        f"env MISSIONCREW_GUIDELINES_DIR; Web `{project_url}/guidelines/<name>`)\n"
        + ("\n".join(guideline_summaries) if guideline_summaries else "(not configured)")
        + "\n## Skill Index (id · name · when to use; entry "
        + (f"`{skills_dir}/<id>/SKILL.md`, env MISSIONCREW_SKILLS_DIR" if skills_dir is not None
           else "see the skill library")
        + f"; Web `{project_url}/skills/<id>`)\n"
        + ("\n".join(skill_summaries) if skill_summaries
           else "(this project has no extra MissionCrew skills)")
        + "\nJudge relevance from the description first and read the full text only when the task "
        "needs it. Relative Markdown links in guidelines and skills resolve against the document "
        "library directory; read linked files only when the task needs them, do not preload, and do "
        "not mechanically follow unrelated entries. A skill resolves relative files such as "
        "scripts/, references/ and assets/ against its own directory."
    )
    overview = (
        "# Workspace Overview\n"
        + (f"Root: `{workspace_dir}` (env MISSIONCREW_WORKSPACE); everything below lives in it:\n"
           "- `documents/` read/write entry to the project document library "
           "(MISSIONCREW_DOCUMENTS_DIR)\n"
           "- `tasks/` read-only Task snapshots (MISSIONCREW_TASKS_DIR)\n"
           "- `guidelines/` full text of enabled guidelines (MISSIONCREW_GUIDELINES_DIR)\n"
           "- `skills/` enabled skills (MISSIONCREW_SKILLS_DIR)\n"
           f"- `channel-history.json` full channel history (MISSIONCREW_CHANNEL_HISTORY, "
           f"`{workspace_dir / 'channel-history.json'}`; executors get a redacted view); "
           "read it only when the recent conversation is not enough for the task\n"
           f"- `manual.md` the full manual (MISSIONCREW_MANUAL, `{workspace_dir / 'manual.md'}`): "
           "this block keeps only the rules that always apply; for the details of document "
           "publishing, Tasks, channels, dashboards and data sources, guideline/skill formats, "
           "automation, the recycle bin and config-page collaboration, read the matching manual "
           "section before acting\n"
           "- `platform-docs/` MissionCrew's own documentation (MISSIONCREW_PLATFORM_DOCS_DIR): "
           "`README.md` there lists every document and what it covers; search it when you need "
           "details of a platform mechanism that the manual does not give\n"
           "- `project.md` project summary; `temp/` temporary directory\n"
           if workspace_dir is not None else
           f"(no dedicated workspace for this run; document library directory `{documents_dir}`)\n")
        + "MissionCrew is a local multi-agent harness (a platform that assembles roles, "
        "runtimes/models, project context, shared materials and channel collaboration); a Task is an "
        "issue that enters a channel, not a separate execution flow; MissionCrew is not the business "
        "repository."
    )
    return "\n\n".join([rules, resources, overview])
