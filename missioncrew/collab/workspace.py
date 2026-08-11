"""Agent 可见的 MissionCrew 工作区。

每次聊天角色执行都会获得一个独立的 ``.missioncrew`` 目录。
该目录位于平台数据根而不是业务代码仓，集中提供项目文档、准则、Skills、
任务文件和（聊天执行时的）角色隔离频道历史。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..core.config import mc_home, workspaces_dir
from ..core.models import Project, Task
from .documents import document_resource_url
from .project_context import guideline_context_dir, write_guideline_context
from .resource_urls import missioncrew_project_url
from .skills import (skill_context_dir, sync_project_skill_library,
                     write_skill_context)

if TYPE_CHECKING:
    from .documents import DocumentLibrary
    from ..core.store import Store


_SAFE_SEGMENT_RE = re.compile(r"[\w-]+")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}
_WORKSPACE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class AgentWorkspace:
    root: Path
    documents: Path
    guidelines: Path
    skills: Path
    tasks: Path
    channel_history: Path | None = None


def _safe_segment(value: str) -> str:
    if _SAFE_SEGMENT_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"item-{digest}"


def _workspace_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _WORKSPACE_LOCKS_GUARD:
        return _WORKSPACE_LOCKS.setdefault(key, threading.RLock())


def chat_workspace_dir(project_id: str, channel_id: str, role_id: str) -> Path:
    """返回 channel×role 独立的 Agent harness 目录。"""
    channel_name = channel_id.removeprefix(f"{project_id}:")
    return (mc_home() / "agent-workspaces" / _safe_segment(project_id)
            / "channels" / _safe_segment(channel_name) / _safe_segment(role_id)
            / ".missioncrew").resolve()


def platform_history_dir(project_id: str, channel_id: str) -> Path:
    """平台内部的完整历史副本；它不会授权给执行角色。"""
    channel_name = channel_id.removeprefix(f"{project_id}:")
    return (mc_home() / "agent-workspaces" / _safe_segment(project_id)
            / "channels" / _safe_segment(channel_name) / "_platform"
            / ".missioncrew").resolve()


def purge_channel_workspaces(project_id: str, channel_id: str) -> int:
    """删除平台持有的频道历史、角色工作区和默认 Runtime 工作目录。"""
    removed = 0
    channel_name = channel_id.removeprefix(f"{project_id}:")
    workspace = (mc_home() / "agent-workspaces" / _safe_segment(project_id)
                 / "channels" / _safe_segment(channel_name))
    if workspace.is_symlink() or workspace.is_file():
        workspace.unlink()
        removed += 1
    elif workspace.is_dir():
        shutil.rmtree(workspace)
        removed += 1

    # ChatEngine 的默认工作目录使用原始频道 id。内容频道 id 由平台生成，
    # 只包含安全字符；若遇到旧版异常 id，宁可保留也不扩大删除边界。
    if re.fullmatch(r"[\w:-]+", channel_id):
        runtime_dir = mc_home() / "channels" / channel_id
        if runtime_dir.is_symlink() or runtime_dir.is_file():
            runtime_dir.unlink()
            removed += 1
        elif runtime_dir.is_dir():
            shutil.rmtree(runtime_dir)
            removed += 1
    return removed


def _legacy_task_workspace_dir(task_workdir: Path) -> Path:
    """返回旧阶段式 Task 的历史 harness 路径，仅供启动迁移。"""
    return (task_workdir / ".missioncrew").resolve()


def migrate_legacy_workspace_layout() -> int:
    """迁移旧版散落的 history/evidence/link/log，返回处理的路径数。"""
    migrated = 0
    home = mc_home().resolve()

    legacy_history = home / "channel-history"
    if legacy_history.is_dir():
        # 频道消息的事实源是 SQLite；新角色视图会在装配时从数据库完整重建。
        shutil.rmtree(legacy_history)
        migrated += 1

    def _move_logs(source: Path, destination: Path) -> None:
        nonlocal migrated
        for legacy_log in source.glob(".mc_last_output_*.log"):
            adapter = legacy_log.stem.removeprefix(".mc_last_output_")
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"last-output-{adapter}.log"
            if target.exists():
                target.unlink()
            legacy_log.replace(target)
            migrated += 1

    channels = home / "channels"
    if channels.is_dir():
        for channel_dir in channels.iterdir():
            if not channel_dir.is_dir():
                continue
            documents = channel_dir / "documents"
            if documents.is_symlink():
                documents.unlink()
                migrated += 1
            legacy_runtime = (home / "agent-workspaces" / "_legacy" / "channels"
                              / _safe_segment(channel_dir.name) / "_platform"
                              / ".missioncrew" / "runtime")
            _move_logs(channel_dir, legacy_runtime)

    task_workspaces = home / "workspaces"
    if task_workspaces.is_dir():
        for task_dir in task_workspaces.iterdir():
            if not task_dir.is_dir():
                continue
            documents = task_dir / "documents"
            if documents.is_symlink():
                documents.unlink()
                migrated += 1
            harness = _legacy_task_workspace_dir(task_dir)
            legacy_evidence = task_dir / "evidence"
            evidence = harness / "evidence"
            if legacy_evidence.is_dir():
                evidence.parent.mkdir(parents=True, exist_ok=True)
                if not evidence.exists():
                    legacy_evidence.replace(evidence)
                else:
                    for item in legacy_evidence.iterdir():
                        target = evidence / item.name
                        if not target.exists():
                            item.replace(target)
                    try:
                        legacy_evidence.rmdir()
                    except OSError:
                        pass
                migrated += 1
            manifest = evidence / "manifest.json"
            if manifest.is_file():
                try:
                    entries = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeError, OSError):
                    entries = None
                if isinstance(entries, list):
                    changed = False
                    for entry in entries:
                        if (isinstance(entry, dict)
                                and isinstance(entry.get("path"), str)
                                and entry["path"].startswith("evidence/")):
                            entry["path"] = ".missioncrew/" + entry["path"]
                            changed = True
                    if changed:
                        _atomic_write_text(
                            manifest,
                            json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
            _move_logs(task_dir, harness / "runtime")

    # A short-lived layout exposed the project document link as ``docs/``.
    # Only migrate platform-created symlinks; preserve any ordinary directory.
    for workspace_root in (home / "agent-workspaces", home / "workspaces"):
        if not workspace_root.is_dir():
            continue
        for temporary_docs in workspace_root.glob("**/.missioncrew/docs"):
            if not temporary_docs.is_symlink():
                continue
            documents = temporary_docs.parent / "documents"
            if documents.is_symlink() or documents.exists():
                temporary_docs.unlink()
            else:
                temporary_docs.rename(documents)
            migrated += 1
    return migrated


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except FileNotFoundError:
        pass
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_page_context_snapshot(project_id: str, channel_id: str, role_id: str,
                                page_kind: str, page_key: str, content: str) -> Path:
    """把配置页正文写入目标角色工作区，并只向聊天消息暴露文件路径。"""
    if page_kind not in {"guidelines", "docs"}:
        raise ValueError("页面类型只支持 guidelines 或 docs")
    key_digest = hashlib.sha256(page_key.encode("utf-8")).hexdigest()[:12]
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    suffix = Path(page_key).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".md" if page_kind == "guidelines" else ".txt"
    directory = (chat_workspace_dir(project_id, channel_id, role_id)
                 / "page-context" / page_kind)
    path = directory / f"{key_digest}-{content_digest}{suffix}"
    with _workspace_lock(directory):
        _atomic_write_text(path, content)
    return path.resolve()


def _link_directory(link: Path, target: Path) -> None:
    """建立平台生成的目录链接，并修复指向旧位置的链接。"""
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(f"MissionCrew 工作区入口已被普通文件占用：{link}")
    link.symlink_to(target.resolve(), target_is_directory=True)


def _remove_managed_workspace_entry(path: Path) -> None:
    """仅清理平台拥有的 Agent workspace 条目，不触碰链接目标。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _link_managed_directory(link: Path, target: Path) -> bool:
    """把平台拥有的 workspace 目录收敛为共享实时视图入口。"""
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == target.resolve():
        return False
    if link.exists() or link.is_symlink():
        _remove_managed_workspace_entry(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target.resolve(), target_is_directory=True)
    return True


def _project_workspace_roots(store: Store, project_id: str) -> list[Path]:
    """列出现有 Channel harness，并兼容迁移旧 Task harness。"""
    roots: set[Path] = set()
    channel_root = (mc_home() / "agent-workspaces" / _safe_segment(project_id)
                    / "channels")
    if channel_root.is_dir():
        for root in channel_root.glob("*/*/.missioncrew"):
            if root.is_dir():
                roots.add(root.resolve())
    for task in store.list_tasks():
        if task.project_id != project_id:
            continue
        root = _legacy_task_workspace_dir(workspaces_dir() / task.id)
        if root.is_dir():
            roots.add(root.resolve())
    return sorted(roots, key=str)


def migrate_resource_workspace_links(store: Store) -> int:
    """把历史准则与 Skill 目录替换为项目级实时视图。"""
    changed = 0
    for project in store.list_projects():
        write_guideline_context(project)
        write_skill_context(project)
        for root in _project_workspace_roots(store, project.id):
            targets = {
                "guidelines": guideline_context_dir(project),
                "skills": skill_context_dir(project),
            }
            for name, target in targets.items():
                entry = root / name
                if entry.exists() or entry.is_symlink():
                    changed += int(_link_managed_directory(entry, target))
    return changed


def _render_project_file(project: Project) -> str:
    repos = "\n".join(
        f"- {repo.name or repo.id}: {repo.path or repo.remote or '未配置本地路径'}"
        for repo in project.repos) or "- 未配置"
    return (
        f"# {project.name}\n\n"
        f"- Project ID: `{project.id}`\n"
        f"- Description: {project.description or '未填写'}\n"
        f"- Orchestrator: `@{project.orchestrator_role_id}`\n\n"
        f"## 本地代码仓\n\n{repos}\n"
    )


def _render_workspace_readme(workspace: AgentWorkspace, project_id: str,
                             *, has_history: bool) -> str:
    history = (
        "- `channel-history.json`：当前频道的完整历史；执行角色看到的是脱敏视图。\n"
        if has_history else "")
    mutation = (
        """## 修改 MissionCrew 资源

当前聊天角色必须使用 Prompt 中的 MissionCrew Agent Tool 发布文档、创建或修改任务、
发送消息、创建频道，以及保存或删除面板、文档、准则或 Skill。平台资源删除后进入统一
回收站；主控可列出、恢复或永久删除回收项。工具会在当前回合返回结构化错误并记录角色
审计。`documents/` 和 `tasks/` 中的直接写入同步只用于旧会话兼容。

不要读取或打印 `.agent-tool-token`；使用 Agent Tool CLI，它会自行读取令牌文件。
平台会把该短期 capability 确定绑定到当前 Run，调用时不要传 `--run-id`。
"""
        if has_history else ""
    )
    return f"""# MissionCrew Agent Harness Workspace

MissionCrew 是一个本地 Agent harness：它负责装配角色、Runtime/模型、项目上下文、
共享资料和 Channel 协作；它不是当前业务代码仓。

此 `.missioncrew` 目录是平台提供的独立工作区，绝对路径为：
`{workspace.root}`

它位于业务代码仓之外。在这里创建或修改文件不会进入业务代码目录，也不会成为业务
代码提交；源代码仍应在本轮提示词给出的工作目录或项目代码仓中修改。

## 可用内容

- `documents/`：项目版本化文档库的读取入口；协作草稿、报告和普通聊天产生的验证记录也归入该文档库。
- `tasks/`：项目 Task 的 Markdown 快照；Task 是可编辑 Issue，状态简报是只读快照。
- `guidelines/`：已启用项目准则的共享实时视图；根据 description 判断是否需要读取。
- `skills/`：已启用项目 Skill 的共享实时视图；先读 SKILL.md，再按需使用同目录 scripts/、references/、assets/ 等文件。
- `project.md`：项目简介与资源索引。
{history}
{mutation}
## 文件系统边界

只能读写 Prompt 中“本次可读写目录”列出的路径及其子目录。禁止访问 `/tmp`、
`/var/tmp`、其他项目目录和未授权的用户文件；Shell 重定向、后台日志和工具自动生成
文件同样受此限制。临时文件优先放在业务仓已有的任务目录；没有项目约定时使用
`{workspace.root / "temp"}`，任务结束后清理。不要先尝试外部路径再等待权限批准。

## 对外引用

`.missioncrew` 下的目录是 Runtime 内部读写入口。向频道回复 MissionCrew 资源时，
使用 `{missioncrew_project_url(project_id)}/<资源类型>/<稳定标识>`；频道、任务、面板、
准则、Skill、文档、回收站的类型依次为 `channels`、`tasks`、`dashboards`、
`guidelines`、`skills`、`documents`、`recycle-bin`。

向频道回复项目文档时，使用
`{document_resource_url(project_id)}/<文档库相对路径>`，例如
`[设计说明]({document_resource_url(project_id)}/specs/design.md)`。不要在回复中输出
本工作区绝对路径、`.missioncrew` 真实路径或 `file://` 链接。

## Task 修改

`tasks/` 中的 Markdown 全部是平台生成的只读快照。创建、更新、追加简报或删除 Task
必须显式调用 Prompt 提供的 Agent Tool；回合结束时平台只刷新快照，不会把文件修改
同步回 Task。正式项目文档和协作草稿应通过 `document.publish` 写入 `documents/`。
"""


def _render_task(task: Task, briefs: list[dict] | None = None) -> str:
    attributes = {
        "id": task.id,
        "title": task.title,
        "summary": task.summary,
        "status": task.status,
        "labels": task.labels,
        "channel_ids": task.channel_ids,
        "snapshot_updated_at": task.updated_at,
        "status_briefs": [
            {key: brief.get(key) for key in
             ("id", "status", "author", "author_type", "created_at", "content")}
            for brief in (briefs or [])
        ],
    }
    header = yaml.safe_dump(
        attributes, allow_unicode=True, sort_keys=False,
        default_flow_style=False).strip()
    body = task.body.rstrip()
    return f"---\n{header}\n---\n" + (f"\n{body}\n" if body else "")


def _task_path(directory: Path, task_id: str) -> Path:
    return directory / f"{_safe_segment(task_id)}.md"


def write_task_files(store: Store, project_id: str, directory: Path) -> None:
    """把数据库任务原子写为当前执行者自己的 Markdown 快照。"""
    directory.mkdir(parents=True, exist_ok=True)
    tasks = {
        task.id: task for task in store.list_tasks()
        if task.project_id == project_id and not task.archived
    }
    # 只清理事实源中已不存在的平台快照。未识别文件不会反向同步成 Task，
    # 也不会在刷新时静默删除，避免升级后破坏旧工作区遗留内容。
    for path in directory.glob("*.md"):
        try:
            parsed = _parse_task_file(path)
            if parsed is None:
                continue
            attributes, _body = parsed
        except (OSError, UnicodeError, ValueError):
            continue
        task_id = attributes.get("id")
        generated = (
            isinstance(task_id, str)
            and "snapshot_updated_at" in attributes
            and "status_briefs" in attributes
            and path == _task_path(directory, task_id)
        )
        if generated and task_id not in tasks:
            path.unlink(missing_ok=True)
    for task in tasks.values():
        _atomic_write_text(
            _task_path(directory, task.id),
            _render_task(task, store.list_task_briefs(task.id)),
        )


def _parse_task_file(path: Path) -> tuple[dict, str] | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(("---\n", "---\r\n")):
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("Task YAML frontmatter 缺少结束分隔符 ---")
    try:
        attributes = yaml.safe_load(match.group("header")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter 不是有效 YAML：{exc}") from exc
    if not isinstance(attributes, dict):
        raise ValueError("frontmatter 必须是对象")
    body = text[match.end():]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return attributes, body.rstrip()


def prepare_agent_workspace(store: Store, project: Project,
                            library: DocumentLibrary, root: Path,
                            *, has_history: bool = False) -> tuple[AgentWorkspace, list[str]]:
    """创建/刷新一个 Agent 可见的隔离 ``.missioncrew`` 工作区。"""
    root = root.resolve()
    workspace = AgentWorkspace(
        root=root,
        documents=root / "documents",
        guidelines=root / "guidelines",
        skills=root / "skills",
        tasks=root / "tasks",
        channel_history=(root / "channel-history.json") if has_history else None,
    )
    with _workspace_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        project, _ = sync_project_skill_library(store, project)
        temporary_docs = root / "docs"
        if temporary_docs.is_symlink():
            if workspace.documents.is_symlink() or workspace.documents.exists():
                temporary_docs.unlink()
            else:
                temporary_docs.rename(workspace.documents)
        _link_directory(workspace.documents, library.root)
        write_guideline_context(project)
        _link_managed_directory(
            workspace.guidelines, guideline_context_dir(project))
        (root / "guidelines.json").unlink(missing_ok=True)

        write_skill_context(project)
        _link_managed_directory(workspace.skills, skill_context_dir(project))

        write_task_files(store, project.id, workspace.tasks)
        _atomic_write_text(root / "project.md", _render_project_file(project))
        _atomic_write_text(
            root / "README.md",
            _render_workspace_readme(workspace, project.id, has_history=has_history))
        error_file = root / "task-sync-errors.log"
        error_file.unlink(missing_ok=True)
        return workspace, []
