"""Agent 可见的 MissionCrew 工作区。

每次聊天角色或结构化任务执行都会获得一个独立的 ``.missioncrew`` 目录。
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

from ..core.config import mc_home
from ..core.models import Project, Task, TIER_ORDER, new_id

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


def task_workspace_dir(task_workdir: Path) -> Path:
    """结构化任务工作区本就是平台目录，可直接在其中建立 harness 入口。"""
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
            harness = task_workspace_dir(task_dir)
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


def _render_project_file(project: Project) -> str:
    repos = "\n".join(
        f"- {repo.name or repo.id}: {repo.path or repo.remote or '未配置本地路径'}"
        for repo in project.repos) or "- 未配置"
    return (
        f"# {project.name}\n\n"
        f"- Project ID: `{project.id}`\n"
        f"- Description: {project.description or '未填写'}\n"
        f"- Orchestrator: `@{project.orchestrator_role_id}`\n\n"
        f"## 项目资源\n\n{repos}\n"
    )


def _render_workspace_readme(workspace: AgentWorkspace, *, has_history: bool) -> str:
    history = (
        "- `channel-history.json`：当前频道的完整历史；执行角色看到的是脱敏视图。\n"
        if has_history else "")
    return f"""# MissionCrew Agent Harness Workspace

MissionCrew 是一个本地 Agent harness：它负责装配角色、Runtime/模型、项目上下文、
共享资料和协作/任务流程；它不是当前业务代码仓。

此 `.missioncrew` 目录是平台提供的独立工作区，绝对路径为：
`{workspace.root}`

它位于业务代码仓之外。在这里创建或修改文件不会进入业务代码目录，也不会成为业务
代码提交；源代码仍应在本轮提示词给出的工作目录或项目代码仓中修改。

## 可用内容

- `documents/`：项目版本化文档库，可直接创建和编辑 Markdown 或其他项目文档；平台会在执行后记录版本。
- `tasks/`：项目任务的 Markdown 视图。可新建任务文件，也可编辑既有任务的标题、描述、类型、标签、风险和预算字段；平台会在执行后同步。状态、阶段和审批由平台管理。
- `guidelines/`：项目准则 Markdown 快照；根据 description 判断是否需要读取。
- `skills/`：项目 Skill 文件；结合当前任务按需读取。
- `project.md`：项目简介与资源索引。
{history}
## 新建任务格式

在 `tasks/` 新建任意 `.md` 文件，省略 `id` 和所有系统字段：

```markdown
---
title: 任务标题
task_type: feature
labels: []
risk: normal
security_level: 0
max_tier: null
---

任务描述与验收要求。
```

`task_type` 可选 `feature`、`bug`、`chore`、`research`；`risk` 可选 `low`、
`normal`、`high`；`max_tier` 可为空或 `economy`、`standard`、`expert`。
编辑既有任务时保留 `id` 和 `snapshot_updated_at`，不要修改 `status` 或阶段字段。

不要把业务源码或交付物写进 `.missioncrew`。只有项目文档、任务、证据和其他 harness
协作资料属于这里。
"""


def _render_skill(skill) -> str:
    header = yaml.safe_dump(
        {"name": skill.name or skill.id, "description": skill.description},
        allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    body = skill.instructions.rstrip()
    return f"---\n{header}\n---\n" + (f"\n{body}\n" if body else "")


def _render_task(task: Task) -> str:
    current = task.current_stage.name if task.current_stage else ""
    attributes = {
        "id": task.id,
        "title": task.title,
        "task_type": task.task_type,
        "labels": task.labels,
        "risk": task.risk,
        "security_level": task.security_level,
        "max_tier": task.max_tier,
        "status": task.status,
        "current_stage": current,
        "snapshot_updated_at": task.updated_at,
    }
    header = yaml.safe_dump(
        attributes, allow_unicode=True, sort_keys=False,
        default_flow_style=False).strip()
    body = task.description.rstrip()
    return f"---\n{header}\n---\n" + (f"\n{body}\n" if body else "")


def _task_path(directory: Path, task_id: str) -> Path:
    return directory / f"{_safe_segment(task_id)}.md"


def write_task_files(store: Store, project_id: str, directory: Path) -> None:
    """把数据库任务原子写为当前执行者自己的 Markdown 快照。"""
    directory.mkdir(parents=True, exist_ok=True)
    for task in store.list_tasks():
        if task.project_id == project_id:
            _atomic_write_text(_task_path(directory, task.id), _render_task(task))


def _parse_task_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("任务 Markdown 必须以 YAML frontmatter 开头")
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


def _editable_task_values(attributes: dict, description: str) -> dict:
    title = attributes.get("title", "")
    task_type = attributes.get("task_type", "feature")
    labels = attributes.get("labels", [])
    risk = attributes.get("risk", "normal")
    security_level = attributes.get("security_level", 0)
    max_tier = attributes.get("max_tier") or None
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title 必须是非空字符串")
    if task_type not in ("feature", "bug", "chore", "research"):
        raise ValueError("task_type 必须是 feature/bug/chore/research")
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise ValueError("labels 必须是字符串数组")
    if risk not in ("low", "normal", "high"):
        raise ValueError("risk 必须是 low/normal/high")
    if isinstance(security_level, bool) or not isinstance(security_level, int) \
            or security_level < 0:
        raise ValueError("security_level 必须是非负整数")
    if max_tier is not None and max_tier not in TIER_ORDER:
        raise ValueError("max_tier 必须为空或 economy/standard/expert")
    return {
        "title": title.strip(), "description": description,
        "task_type": task_type, "labels": labels, "risk": risk,
        "security_level": security_level, "max_tier": max_tier,
    }


def sync_task_files(store: Store, project_id: str, directory: Path,
                    actor: str) -> list[str]:
    """摄入 Agent 对任务 Markdown 的创建/编辑，返回未能同步的错误。"""
    from ..taskflow import workflow

    if not directory.is_dir():
        return []
    errors: list[str] = []
    for path in sorted(directory.glob("*.md")):
        try:
            attributes, description = _parse_task_file(path)
            values = _editable_task_values(attributes, description)
            raw_id = attributes.get("id")
            if raw_id is not None and not isinstance(raw_id, str):
                raise ValueError("id 必须是字符串或留空")
            task_id = (raw_id or "").strip()
            existing = store.get_task(task_id) if task_id else None
            if task_id and existing is None:
                raise ValueError("未知任务 id；新建任务时请省略 id")
            if existing is not None:
                if existing.project_id != project_id:
                    raise ValueError("任务不属于当前项目")
                snapshot = attributes.get("snapshot_updated_at")
                if not isinstance(snapshot, (int, float)) or isinstance(snapshot, bool):
                    raise ValueError("既有任务缺少有效 snapshot_updated_at")
                if abs(float(snapshot) - existing.updated_at) > 1e-6:
                    raise ValueError("任务已被其他执行更新，请重新读取后再修改")
                changed = any(getattr(existing, key) != value
                              for key, value in values.items())
                if not changed:
                    continue
                pristine = (existing.stage_index == 0
                            and all(stage.status == "pending" and stage.attempts == 0
                                    for stage in existing.stages))
                plan_changed = (existing.task_type != values["task_type"]
                                or existing.risk != values["risk"])
                for key, value in values.items():
                    setattr(existing, key, value)
                if pristine and plan_changed:
                    existing.stages = workflow.build_plan(existing.task_type, existing.risk)
                store.put_task(existing)
                store.audit(actor, "task_workspace_updated", existing.id,
                            f"path={path.name}")
                _atomic_write_text(path, _render_task(existing))
                continue

            task = Task(
                id=new_id("t"), project_id=project_id, **values,
                stages=workflow.build_plan(values["task_type"], values["risk"]),
            )
            store.put_task(task)
            store.audit(actor, "task_workspace_created", task.id,
                        f"source={path.name}")
            target = _task_path(directory, task.id)
            _atomic_write_text(target, _render_task(task))
            if path != target:
                path.unlink()
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
    return errors


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
        _link_directory(workspace.documents, library.root)
        workspace.guidelines.mkdir(parents=True, exist_ok=True)
        enabled_guidelines = {guideline.name: guideline for guideline in project.guidelines
                              if guideline.enabled}
        for guideline in enabled_guidelines.values():
            _atomic_write_text(workspace.guidelines / f"{guideline.name}.md",
                               guideline.render_markdown())
        for stale in workspace.guidelines.glob("*.md"):
            if stale.stem not in enabled_guidelines:
                stale.unlink()
        (root / "guidelines.json").unlink(missing_ok=True)

        workspace.skills.mkdir(parents=True, exist_ok=True)
        enabled_skills = {skill.id: skill for skill in project.skills if skill.enabled}
        for skill in enabled_skills.values():
            skill_dir = workspace.skills / _safe_segment(skill.id)
            skill_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(skill_dir / "SKILL.md", _render_skill(skill))
        for stale in workspace.skills.iterdir():
            if stale.is_dir() and stale.name not in {_safe_segment(v) for v in enabled_skills}:
                for child in stale.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                try:
                    stale.rmdir()
                except OSError:
                    pass

        write_task_files(store, project.id, workspace.tasks)
        _atomic_write_text(root / "project.md", _render_project_file(project))
        _atomic_write_text(root / "README.md",
                           _render_workspace_readme(workspace, has_history=has_history))
        error_file = root / "task-sync-errors.log"
        error_file.unlink(missing_ok=True)
        return workspace, []
