"""项目级统一回收站：归档、恢复与永久删除 MissionCrew 资源。"""
from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from ..core.config import projects_dir
from ..core.models import Board, Channel, Project, ProjectResource, Role, Task
from ..core.store import Store
from .documents import (document_resource_url, guideline_library_for,
                        library_for, safe_relative_path)
from .guidelines import delete_guideline, save_guideline
from .resource_urls import (channel_resource_url, dashboard_resource_url,
                            guideline_resource_url, missioncrew_resource_url,
                            skill_resource_url, task_resource_url)
from .skills import (project_skill_library_dir, sync_project_skill_library,
                     write_skill_context)


RESOURCE_TYPES = frozenset({
    "document", "guideline", "skill", "dashboard", "channel", "role",
    "project_resource", "task",
})
_PROJECT_ID_RE = re.compile(r"[\w-]+")
_ITEM_ID_RE = re.compile(r"rb_[0-9]{13}_[0-9a-f]{12}")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class RecycleConflictError(Exception):
    """恢复目标已被新资源占用。"""


def _project_lock(project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


def recycle_bin_url(project_id: str) -> str:
    return missioncrew_resource_url(project_id, "recycle-bin")


def recycle_bin_dir(project_id: str) -> Path:
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("项目 id 只能包含字母、数字、下划线、连字符")
    root = projects_dir() / project_id / "recycle-bin"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _item_dir(project_id: str, item_id: str) -> Path:
    if not _ITEM_ID_RE.fullmatch(item_id):
        raise ValueError("回收站条目 id 不合法")
    return recycle_bin_dir(project_id) / item_id


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".manifest.", suffix=".tmp", delete=False) as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*")
               if path.is_file() and not path.is_symlink())


def _validate_archive_directory(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("回收目录必须是普通目录")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"回收目录不允许符号链接: {path.relative_to(root)}")


def _public_item(manifest: dict) -> dict:
    return {
        key: manifest[key] for key in (
            "id", "project_id", "resource_type", "resource_id", "name",
            "deleted_at", "actor", "size",
        )
    } | {
        "resource_url": recycle_bin_url(str(manifest["project_id"])),
    }


def _load_manifest(project_id: str, item_id: str) -> dict:
    path = _item_dir(project_id, item_id) / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError("回收站条目不存在") from exc
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise ValueError("回收站条目元数据损坏") from exc
    required = {
        "id", "project_id", "resource_type", "resource_id", "name",
        "deleted_at", "actor", "size", "metadata",
    }
    if (not isinstance(value, dict) or not required.issubset(value)
            or value.get("id") != item_id
            or value.get("project_id") != project_id
            or value.get("resource_type") not in RESOURCE_TYPES):
        raise ValueError("回收站条目元数据不合法")
    return value


def _create_item(project_id: str, resource_type: str, resource_id: str,
                 name: str, actor: str, metadata: dict,
                 write_payload: Callable[[Path], int]) -> dict:
    if resource_type not in RESOURCE_TYPES:
        raise ValueError(f"不支持回收的资源类型: {resource_type}")
    with _project_lock(project_id):
        root = recycle_bin_dir(project_id)
        item_id = f"rb_{int(time.time() * 1000):013d}_{uuid.uuid4().hex[:12]}"
        final = root / item_id
        staging = root / f".{item_id}.tmp"
        staging.mkdir()
        try:
            payload = staging / "payload"
            size = write_payload(payload)
            manifest = {
                "id": item_id,
                "project_id": project_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "name": name or resource_id,
                "deleted_at": time.time(),
                "actor": actor,
                "size": int(size),
                "metadata": metadata,
            }
            _atomic_write_json(staging / "manifest.json", manifest)
            staging.rename(final)
            return manifest
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _archive_bytes(project_id: str, resource_type: str, resource_id: str,
                   name: str, actor: str, content: bytes, metadata: dict) -> dict:
    def write(payload: Path) -> int:
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(content)
        return len(content)

    return _create_item(
        project_id, resource_type, resource_id, name, actor, metadata, write)


def _archive_snapshot(project_id: str, resource_type: str, resource_id: str,
                      name: str, actor: str, snapshot: dict) -> dict:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _archive_bytes(
        project_id, resource_type, resource_id, name, actor, encoded,
        {"snapshot": snapshot})


def _archive_directory(project_id: str, resource_type: str, resource_id: str,
                       name: str, actor: str, source: Path,
                       metadata: dict) -> dict:
    _validate_archive_directory(source)

    def write(payload: Path) -> int:
        shutil.copytree(source, payload)
        return _directory_size(payload)

    return _create_item(
        project_id, resource_type, resource_id, name, actor, metadata, write)


def archive_replaced_skill(project: Project, skill_id: str, source: Path,
                           *, actor: str, enabled: bool = True,
                           reason: str = "overwrite") -> dict:
    """导入覆盖 Skill 时把旧完整包送入同一项目回收站。"""
    skill = next((item for item in project.skills if item.id == skill_id), None)
    if skill is not None:
        enabled = skill.enabled
    manifest = _archive_directory(
        project.id, "skill", skill_id,
        (skill.name if skill else skill_id) or skill_id, actor, source,
        {"id": skill_id, "enabled": enabled, "reason": reason})
    return _public_item(manifest)


def migrate_legacy_skill_trash(store: Store) -> int:
    """把旧 ``.skill-trash`` 内容迁入统一回收站，返回迁移包数量。"""
    migrated = 0
    for project in store.list_projects():
        legacy_root = projects_dir() / project.id / ".skill-trash"
        if not legacy_root.is_dir() or legacy_root.is_symlink():
            continue
        for timestamp_dir in sorted(legacy_root.iterdir()):
            if not timestamp_dir.is_dir() or timestamp_dir.is_symlink():
                continue
            for skill_dir in sorted(timestamp_dir.iterdir()):
                if not skill_dir.is_dir() or skill_dir.is_symlink():
                    continue
                archive_replaced_skill(
                    project, skill_dir.name, skill_dir, actor="platform",
                    reason="legacy_skill_trash")
                shutil.rmtree(skill_dir)
                migrated += 1
            try:
                timestamp_dir.rmdir()
            except OSError:
                pass
        try:
            legacy_root.rmdir()
        except OSError:
            pass
    return migrated


def _discard(project_id: str, item_id: str) -> None:
    directory = _item_dir(project_id, item_id)
    if directory.is_symlink() or not directory.is_dir():
        raise FileNotFoundError("回收站条目不存在")
    shutil.rmtree(directory)


def list_recycle_items(project_id: str) -> list[dict]:
    with _project_lock(project_id):
        items = []
        for directory in recycle_bin_dir(project_id).iterdir():
            if directory.name.startswith(".") or not directory.is_dir() \
                    or directory.is_symlink():
                continue
            try:
                items.append(_public_item(_load_manifest(project_id, directory.name)))
            except (FileNotFoundError, ValueError):
                continue
        return sorted(items, key=lambda item: item["deleted_at"], reverse=True)


def recycle_document(store: Store, project: Project, path: str, *, actor: str) -> dict:
    relative = safe_relative_path(path)
    library = library_for(project.id)
    content = library.read_bytes(relative)
    manifest = _archive_bytes(
        project.id, "document", relative, relative.rsplit("/", 1)[-1], actor,
        content, {"path": relative})
    try:
        revision = library.delete(relative, actor=actor)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "document_recycled",
                detail=(f"project={project.id} path={relative} item={manifest['id']} "
                        f"revision={revision}"))
    return {**_public_item(manifest), "revision": revision}


def recycle_guideline(store: Store, project: Project, name: str, *, actor: str) -> dict:
    guideline = next((item for item in project.guidelines if item.name == name), None)
    if guideline is None:
        raise FileNotFoundError("准则不存在")
    markdown = guideline_library_for(project.id).read(f"{name}.md").encode("utf-8")
    manifest = _archive_bytes(
        project.id, "guideline", name, name, actor, markdown,
        {"name": name, "enabled": guideline.enabled})
    try:
        revision = delete_guideline(store, project, name, actor=actor)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "guideline_recycled",
                detail=(f"project={project.id} guideline={name} "
                        f"item={manifest['id']} revision={revision}"))
    return {**_public_item(manifest), "revision": revision}


def recycle_skill(store: Store, project: Project, skill_id: str, *, actor: str) -> dict:
    project, _ = sync_project_skill_library(store, project)
    skill = next((item for item in project.skills if item.id == skill_id), None)
    directory = project_skill_library_dir(project.id) / skill_id
    if skill is None or not directory.is_dir() or directory.is_symlink():
        raise FileNotFoundError("Skill 不存在")
    manifest = _archive_directory(
        project.id, "skill", skill_id, skill.name or skill_id, actor, directory,
        {"id": skill_id, "enabled": skill.enabled})
    try:
        shutil.rmtree(directory)
        current = store.get_project(project.id) or project
        current.skills = [item for item in current.skills if item.id != skill_id]
        store.put_project(current)
        sync_project_skill_library(store, current, audit=False)
    except Exception:
        if not directory.exists():
            shutil.copytree(_item_dir(project.id, manifest["id"]) / "payload", directory)
        rollback = store.get_project(project.id) or project
        rollback, _ = sync_project_skill_library(store, rollback, audit=False)
        restored = next(
            (item for item in rollback.skills if item.id == skill_id), None)
        if restored is not None:
            restored.enabled = skill.enabled
            store.put_project(rollback)
            write_skill_context(rollback)
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "skill_recycled",
                detail=f"project={project.id} skill={skill_id} item={manifest['id']}")
    return _public_item(manifest)


def recycle_dashboard(store: Store, project: Project, board_id: str,
                      *, actor: str) -> dict:
    board = store.get_board(board_id)
    if board is None or board.project_id != project.id:
        raise FileNotFoundError("面板不存在")
    manifest = _archive_snapshot(
        project.id, "dashboard", board.id, board.name or board.id, actor,
        board.to_dict())
    try:
        store.delete_board(board.id)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "dashboard_recycled",
                detail=f"project={project.id} board={board.id} item={manifest['id']}")
    return _public_item(manifest)


def recycle_task(store: Store, project: Project, task: Task, *, actor: str) -> dict:
    """把 Task 与追加式状态简报作为一个可恢复快照移入回收站。"""
    if task.project_id != project.id:
        raise ValueError("任务不属于当前项目")
    snapshot = {
        "task": task.to_dict(),
        "briefs": store.list_task_briefs(task.id, limit=None),
    }
    manifest = _archive_snapshot(
        project.id, "task", task.id, task.title or task.id, actor, snapshot)
    try:
        store.delete_task(task.id)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(
        actor, "task_recycled", task.id,
        f"project={project.id} task={task.id} item={manifest['id']}")
    return _public_item(manifest)


def recycle_channel(store: Store, project: Project, channel: Channel,
                    *, actor: str) -> dict:
    if channel.project_id != project.id:
        raise ValueError("频道不属于当前项目")
    manifest = _archive_snapshot(
        project.id, "channel", channel.id, channel.name or channel.id, actor,
        channel.to_dict())
    try:
        store.delete_channel(channel.id)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "channel_recycled",
                detail=f"project={project.id} channel={channel.id} item={manifest['id']}")
    return _public_item(manifest)


def recycle_role(store: Store, project: Project, role: Role, *, actor: str) -> dict:
    if role.project_id != project.id:
        raise ValueError("角色不属于当前项目")
    manifest = _archive_snapshot(
        project.id, "role", role.id, role.name or role.id, actor, role.to_dict())
    try:
        store.delete_role(project.id, role.id)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "role_recycled",
                detail=f"project={project.id} role={role.id} item={manifest['id']}")
    return _public_item(manifest)


def recycle_project_resource(store: Store, project: Project, resource_id: str,
                             *, actor: str) -> dict:
    resource = next((item for item in project.repos if item.id == resource_id), None)
    if resource is None:
        raise FileNotFoundError("项目资源不存在")
    snapshot = dict(resource.__dict__)
    manifest = _archive_snapshot(
        project.id, "project_resource", resource.id,
        resource.name or resource.id, actor, snapshot)
    project.repos = [item for item in project.repos if item.id != resource_id]
    try:
        store.put_project(project)
    except Exception:
        _discard(project.id, manifest["id"])
        raise
    store.audit(actor, "project_resource_recycled",
                detail=f"project={project.id} resource={resource_id} item={manifest['id']}")
    return _public_item(manifest)


def _restore_document(store: Store, project: Project, manifest: dict,
                      payload: Path, actor: str) -> str:
    path = str(manifest["metadata"]["path"])
    try:
        library_for(project.id).write_bytes(
            path, payload.read_bytes(), actor=actor,
            message=f"Restore {path} from recycle bin", overwrite=False)
    except FileExistsError as exc:
        raise RecycleConflictError(str(exc)) from exc
    return document_resource_url(project.id, path)


def _restore_guideline(store: Store, project: Project, manifest: dict,
                       payload: Path, actor: str) -> str:
    name = str(manifest["metadata"]["name"])
    current = store.get_project(project.id) or project
    if any(item.name == name for item in current.guidelines):
        raise RecycleConflictError(f"准则已存在: {name}")
    save_guideline(
        store, current, payload.read_text(encoding="utf-8"),
        enabled=bool(manifest["metadata"].get("enabled", True)), actor=actor)
    return guideline_resource_url(project.id, name)


def _restore_skill(store: Store, project: Project, manifest: dict,
                   payload: Path, actor: str) -> str:
    skill_id = str(manifest["metadata"]["id"])
    target = project_skill_library_dir(project.id) / skill_id
    if target.exists() or target.is_symlink():
        raise RecycleConflictError(f"Skill 已存在: {skill_id}")
    previous = store.get_project(project.id) or project
    previous_skills = copy.deepcopy(previous.skills)
    shutil.copytree(payload, target)
    try:
        current = store.get_project(project.id) or project
        current, _ = sync_project_skill_library(store, current, audit=False)
        if not any(item.id == skill_id for item in current.skills):
            raise ValueError("回收站中的 Skill 包不再满足当前校验要求")
        restored = next(item for item in current.skills if item.id == skill_id)
        restored.enabled = bool(manifest["metadata"].get("enabled", True))
        store.put_project(current)
        write_skill_context(current)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        rollback = store.get_project(project.id) or project
        rollback.skills = previous_skills
        store.put_project(rollback)
        write_skill_context(rollback)
        raise
    return skill_resource_url(project.id, skill_id)


def _restore_snapshot(store: Store, project: Project, manifest: dict) -> str:
    resource_type = str(manifest["resource_type"])
    snapshot = manifest["metadata"].get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("回收站快照不合法")
    if resource_type == "dashboard":
        board = Board.from_dict(snapshot)
        if store.get_board(board.id):
            raise RecycleConflictError(f"面板已存在: {board.id}")
        store.put_board(board)
        return dashboard_resource_url(project.id, board.id)
    if resource_type == "task":
        task_data = snapshot.get("task")
        briefs = snapshot.get("briefs", [])
        if not isinstance(task_data, dict) or not isinstance(briefs, list):
            raise ValueError("回收站中的 Task 快照不合法")
        task = Task.from_dict(task_data)
        if task.project_id != project.id:
            raise ValueError("回收站中的 Task 不属于当前项目")
        if store.get_task(task.id):
            raise RecycleConflictError(f"Task 已存在: {task.id}")
        store.restore_task(task, briefs)
        return task_resource_url(project.id, task.id)
    if resource_type == "channel":
        channel = Channel.from_dict(snapshot)
        if store.get_channel(channel.id):
            raise RecycleConflictError(f"频道已存在: {channel.id}")
        store.put_channel(channel)
        return channel_resource_url(project.id, channel.id)
    if resource_type == "role":
        role = Role.from_dict(snapshot)
        if store.get_role(project.id, role.id):
            raise RecycleConflictError(f"角色已存在: {role.id}")
        store.put_role(role)
        return recycle_bin_url(project.id)
    if resource_type == "project_resource":
        current = store.get_project(project.id) or project
        resource = ProjectResource.from_dict(snapshot)
        if any(item.id == resource.id for item in current.repos):
            raise RecycleConflictError(f"项目资源已存在: {resource.id}")
        current.repos.append(resource)
        store.put_project(current)
        return recycle_bin_url(project.id)
    raise ValueError(f"不支持恢复资源类型: {resource_type}")


def restore_recycle_item(store: Store, project: Project, item_id: str,
                         *, actor: str) -> dict:
    with _project_lock(project.id):
        manifest = _load_manifest(project.id, item_id)
        payload = _item_dir(project.id, item_id) / "payload"
        resource_type = str(manifest["resource_type"])
        if resource_type == "document":
            restored_url = _restore_document(store, project, manifest, payload, actor)
        elif resource_type == "guideline":
            restored_url = _restore_guideline(store, project, manifest, payload, actor)
        elif resource_type == "skill":
            restored_url = _restore_skill(store, project, manifest, payload, actor)
        else:
            restored_url = _restore_snapshot(store, project, manifest)
        _discard(project.id, item_id)
        store.audit(actor, "recycle_item_restored",
                    detail=(f"project={project.id} item={item_id} "
                            f"type={resource_type} resource={manifest['resource_id']}"))
        return {**_public_item(manifest), "restored_resource_url": restored_url}


def purge_recycle_item(store: Store, project: Project, item_id: str,
                       *, actor: str) -> dict:
    with _project_lock(project.id):
        manifest = _load_manifest(project.id, item_id)
        _discard(project.id, item_id)
        store.audit(actor, "recycle_item_purged",
                    detail=(f"project={project.id} item={item_id} "
                            f"type={manifest['resource_type']} "
                            f"resource={manifest['resource_id']}"))
        return _public_item(manifest)


def empty_recycle_bin(store: Store, project: Project, *, actor: str) -> int:
    with _project_lock(project.id):
        items = list_recycle_items(project.id)
        for item in items:
            _discard(project.id, str(item["id"]))
        if items:
            store.audit(actor, "recycle_bin_emptied",
                        detail=f"project={project.id} count={len(items)}")
        return len(items)
