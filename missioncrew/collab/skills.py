"""完整项目 Skill 目录的发现、导入与运行时适配。"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml

from ..core.config import projects_dir
from ..core.models import Project, ProjectSkill
from .resource_urls import skill_resource_url

if TYPE_CHECKING:
    from ..core.store import Store


MAX_SKILL_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_SKILL_FILES = 5000
MAX_SKILL_FILE_BYTES = 25 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 200 * 1024 * 1024
MAX_SKILL_FILE_PREVIEW_BYTES = 512 * 1024

_ID_RE = re.compile(r"[\w-]+")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _project_lock(project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


def project_skill_library_dir(project_id: str) -> Path:
    """返回用户可直接投放完整 Skill 目录的项目路径。"""
    if not _ID_RE.fullmatch(project_id):
        raise ValueError("项目 id 只能包含字母、数字、下划线、连字符")
    root = projects_dir() / project_id / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def skill_context_dir(project: Project) -> Path:
    """返回 Runtime 只暴露已启用 Skill 的共享目录视图。"""
    return (projects_dir() / project.id / "runtime-context" / "skills").resolve()


def _remove_context_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def write_skill_context(project: Project) -> Path:
    """刷新项目级 Skill 视图；所有 Agent workspace 共享这个入口。"""
    root = project_skill_library_dir(project.id)
    directory = skill_context_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    with _project_lock(project.id):
        expected = {
            skill.id for skill in project.skills
            if (skill.enabled and _ID_RE.fullmatch(skill.id)
                and (root / skill.id).is_dir()
                and not (root / skill.id).is_symlink())
        }
        for stale in directory.iterdir():
            if stale.name not in expected:
                _remove_context_entry(stale)
        for skill_id in sorted(expected):
            target = root / skill_id
            link = directory / skill_id
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            if link.exists() or link.is_symlink():
                _remove_context_entry(link)
            link.symlink_to(target.resolve(), target_is_directory=True)
    return directory


def _initialization_marker(project_id: str) -> Path:
    return projects_dir() / project_id / ".skills-initialized"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def render_skill_markdown(skill: ProjectSkill, existing_markdown: str = "") -> str:
    """把旧文本 Skill 迁移成通用 SKILL.md；name/description 与后端同源。"""
    name = (skill.name or skill.id).strip()
    description = (skill.description or name).strip()
    extra_attributes = {}
    match = _FRONTMATTER_RE.match(existing_markdown)
    if match:
        try:
            current = yaml.safe_load(match.group("header")) or {}
        except yaml.YAMLError:
            current = {}
        if isinstance(current, dict):
            extra_attributes = {
                key: value for key, value in current.items()
                if key not in {"name", "description"}
            }
    header = yaml.safe_dump(
        {"name": name, "description": description, **extra_attributes},
        allow_unicode=True, sort_keys=False, default_flow_style=False,
    ).strip()
    body = skill.instructions.rstrip()
    return f"---\n{header}\n---\n" + (f"\n{body}\n" if body else "")


def parse_skill_markdown(skill_id: str, markdown: str,
                         *, enabled: bool = True) -> ProjectSkill:
    """读取标准 Skill 文件头；额外属性保留在原文件中但不进入索引。"""
    match = _FRONTMATTER_RE.match(markdown)
    if not match:
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头")
    try:
        attributes = yaml.safe_load(match.group("header")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter 不是有效 YAML：{exc}") from exc
    if not isinstance(attributes, dict):
        raise ValueError("SKILL.md frontmatter 必须是属性对象")
    if "name" not in attributes or "description" not in attributes:
        raise ValueError("SKILL.md frontmatter 必须包含 name 和 description")
    name, description = attributes["name"], attributes["description"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("SKILL.md frontmatter 的 name 必须是非空字符串")
    if not isinstance(description, str):
        raise ValueError("SKILL.md frontmatter 的 description 必须是字符串")
    body = markdown[match.end():]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return ProjectSkill(
        id=skill_id, name=name.strip(), description=description.strip(),
        instructions=body.rstrip(), enabled=enabled,
    )


def _skill_file(directory: Path) -> Path | None:
    candidates = [path for path in directory.iterdir()
                  if path.is_file() and not path.is_symlink()
                  and path.name.lower() == "skill.md"]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _validate_tree(directory: Path) -> tuple[list[str], int]:
    """拒绝可逃离 Skill 根的链接，并限制意外超大导入。"""
    files: list[str] = []
    total = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"不允许符号链接：{path.relative_to(directory)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"不支持的文件类型：{path.relative_to(directory)}")
        size = path.stat().st_size
        if size > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"单个文件超过 25 MiB：{path.relative_to(directory)}")
        files.append(path.relative_to(directory).as_posix())
        total += size
        if len(files) > MAX_SKILL_FILES:
            raise ValueError(f"文件数量超过限制 {MAX_SKILL_FILES}")
        if total > MAX_SKILL_TOTAL_BYTES:
            raise ValueError("文件总大小超过 200 MiB")
    return files, total


def read_skill_file(directory: Path, relative: str) -> tuple[str, bool]:
    """读取 Skill 目录内单个文本文件，返回 (内容, 是否截断)。"""
    files, _ = _validate_tree(directory)
    if relative not in files:
        raise FileNotFoundError("文件不存在")
    data = (directory / relative).read_bytes()
    truncated = len(data) > MAX_SKILL_FILE_PREVIEW_BYTES
    if truncated:
        data = data[:MAX_SKILL_FILE_PREVIEW_BYTES]
    if b"\x00" in data:
        raise ValueError("二进制文件不支持在线查看")
    return data.decode("utf-8", errors="replace"), truncated


def skill_directory_version(directory: Path) -> str:
    digest = hashlib.sha256()
    files, _ = _validate_tree(directory)
    for relative in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with (directory / relative).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _initialize_legacy_skills(project: Project, root: Path) -> None:
    marker = _initialization_marker(project.id)
    if marker.exists():
        return
    for skill in project.skills:
        if not _ID_RE.fullmatch(skill.id):
            continue
        directory = root / skill.id
        if directory.exists():
            continue
        directory.mkdir(parents=True)
        _atomic_write_text(directory / "SKILL.md", render_skill_markdown(skill))
    _atomic_write_text(marker, "MissionCrew project skill library initialized.\n")


def materialize_project_skills(project: Project, *, overwrite: bool = False) -> Path:
    """把项目定义中的文本 Skill 写入完整目录，供 CLI/项目 YAML 兼容使用。"""
    with _project_lock(project.id):
        root = project_skill_library_dir(project.id)
        _initialize_legacy_skills(project, root)
        for skill in project.skills:
            if not _ID_RE.fullmatch(skill.id):
                continue
            directory = root / skill.id
            target = directory / "SKILL.md"
            if target.exists() and not overwrite:
                continue
            directory.mkdir(parents=True, exist_ok=True)
            existing = target.read_text(encoding="utf-8") if target.is_file() else ""
            _atomic_write_text(target, render_skill_markdown(skill, existing))
        return root


def sync_project_skill_library(store: Store, project: Project,
                               *, audit: bool = True) -> tuple[Project, list[str]]:
    """扫描直接子目录并把合法 SKILL.md 元数据同步到 Project。"""
    with _project_lock(project.id):
        root = project_skill_library_dir(project.id)
        _initialize_legacy_skills(project, root)
        existing = {skill.id: skill for skill in project.skills}
        discovered: list[ProjectSkill] = []
        issues: list[str] = []
        for directory in sorted(root.iterdir(), key=lambda path: path.name.lower()):
            if directory.name.startswith("."):
                continue
            if directory.is_symlink() or not directory.is_dir():
                issues.append(f"{directory.name}: Skill 必须是普通目录")
                continue
            skill_id = directory.name
            if not _ID_RE.fullmatch(skill_id):
                issues.append(f"{skill_id}: 目录名只能包含字母、数字、下划线、连字符")
                continue
            skill_file = _skill_file(directory)
            if skill_file is None:
                issues.append(f"{skill_id}: 必须且只能包含一个 SKILL.md（文件名大小写可兼容）")
                continue
            canonical_file = directory / "SKILL.md"
            if skill_file != canonical_file:
                skill_file.rename(canonical_file)
                skill_file = canonical_file
            try:
                _validate_tree(directory)
                skill = parse_skill_markdown(
                    skill_id, skill_file.read_text(encoding="utf-8"),
                    enabled=existing.get(skill_id, ProjectSkill(skill_id)).enabled,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                issues.append(f"{skill_id}: {exc}")
                continue
            discovered.append(skill)

        before = [asdict(skill) for skill in project.skills]
        after = [asdict(skill) for skill in discovered]
        if before != after:
            old_ids = {skill["id"] for skill in before}
            new_ids = {skill["id"] for skill in after}
            project.skills = discovered
            store.put_project(project)
            if audit:
                store.audit(
                    "platform", "skill_library_synced",
                    detail=(f"project={project.id} added={sorted(new_ids - old_ids)} "
                            f"removed={sorted(old_ids - new_ids)}"),
                )
        write_skill_context(project)
        return project, issues


def sync_all_project_skill_libraries(store: Store) -> int:
    changed = 0
    for project in store.list_projects():
        before = [asdict(skill) for skill in project.skills]
        sync_project_skill_library(store, project)
        if before != [asdict(skill) for skill in project.skills]:
            changed += 1
    return changed


def skill_library_info(store: Store, project: Project) -> dict:
    project, issues = sync_project_skill_library(store, project)
    root = project_skill_library_dir(project.id)
    skills = []
    for skill in project.skills:
        directory = root / skill.id
        files, total = _validate_tree(directory)
        skill_file = _skill_file(directory) or directory / "SKILL.md"
        skills.append({
            **asdict(skill),
            "resource_url": skill_resource_url(project.id, skill.id),
            "path": str(directory),
            "skill_file": str(skill_file),
            "markdown": (skill_file.read_text(encoding="utf-8")
                         if skill_file.is_file() else ""),
            "files": files,
            "file_count": len(files),
            "total_bytes": total,
            "content_version": skill_directory_version(directory),
        })
    return {"path": str(root), "skills": skills, "issues": issues}


def _prepare_skill_save_target(root: Path, skill_id: str) -> Path:
    directory = root / skill_id
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("Skill 保存目标必须是项目 Skill 根下的普通目录")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_project_skill(store: Store, project: Project, skill: ProjectSkill,
                       *, actor: str) -> ProjectSkill:
    with _project_lock(project.id):
        root = project_skill_library_dir(project.id)
        _initialize_legacy_skills(project, root)
        directory = _prepare_skill_save_target(root, skill.id)
        existing_file = _skill_file(directory)
        target = directory / "SKILL.md"
        existing_markdown = (existing_file.read_text(encoding="utf-8")
                             if existing_file is not None else "")
        if existing_file is not None and existing_file != target:
            existing_file.unlink()
        _atomic_write_text(target, render_skill_markdown(skill, existing_markdown))
        project, _ = sync_project_skill_library(store, project, audit=False)
        saved = next(item for item in project.skills if item.id == skill.id)
        saved.enabled = skill.enabled
        store.put_project(project)
        write_skill_context(project)
        store.audit(actor, "skill_saved", detail=f"project={project.id} skill={skill.id}")
        return saved


def save_project_skill_markdown(store: Store, project: Project, skill_id: str,
                                markdown: str, *, enabled: bool,
                                actor: str) -> ProjectSkill:
    """按完整 SKILL.md 原文保存；frontmatter 与附加属性原样保留。"""
    if not _ID_RE.fullmatch(skill_id):
        raise ValueError("Skill id 只能包含字母、数字、下划线、连字符")
    parse_skill_markdown(skill_id, markdown, enabled=enabled)
    with _project_lock(project.id):
        root = project_skill_library_dir(project.id)
        _initialize_legacy_skills(project, root)
        directory = _prepare_skill_save_target(root, skill_id)
        existing_file = _skill_file(directory)
        target = directory / "SKILL.md"
        if existing_file is not None and existing_file != target:
            existing_file.unlink()
        _atomic_write_text(target, markdown if markdown.endswith("\n") else markdown + "\n")
        project, _ = sync_project_skill_library(store, project, audit=False)
        saved = next(item for item in project.skills if item.id == skill_id)
        saved.enabled = enabled
        store.put_project(project)
        write_skill_context(project)
        store.audit(actor, "skill_saved", detail=f"project={project.id} skill={skill_id}")
        return saved


def _discover_skill_directories(source: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    found: list[tuple[str, Path]] = []
    issues: list[str] = []
    by_id: dict[str, list[Path]] = {}
    root_skill = _skill_file(source)
    candidates = [root_skill] if root_skill is not None else [
        path for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
        and path.name.lower() == "skill.md"
    ]
    for skill_file in sorted(candidates, key=lambda path: path.as_posix().lower()):
        directory = skill_file.parent
        relative = directory.relative_to(source).as_posix() or "."
        try:
            _validate_tree(directory)
            parsed = parse_skill_markdown(
                directory.name, skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            issues.append(f"{relative}: {exc}")
            continue
        skill_id = directory.name if _ID_RE.fullmatch(directory.name) else parsed.name
        if not _ID_RE.fullmatch(skill_id):
            issues.append(
                f"{relative}: 目录名与 frontmatter name 都不能作为合法 Skill id")
            continue
        by_id.setdefault(skill_id, []).append(directory)
    for skill_id, directories in by_id.items():
        if len(directories) > 1:
            paths = ", ".join(path.relative_to(source).as_posix() for path in directories)
            issues.append(f"{skill_id}: 导入源中存在重复 id（{paths}）")
            continue
        found.append((skill_id, directories[0]))
    return found, issues


def _copy_skill_directory(source: Path, destination: Path) -> None:
    _validate_tree(source)
    shutil.copytree(source, destination)
    imported_file = _skill_file(destination)
    if imported_file is None:
        raise ValueError(f"{source.name}: 无法确定 SKILL.md")
    canonical = destination / "SKILL.md"
    if imported_file != canonical:
        imported_file.rename(canonical)


def _install_discovered(store: Store, project: Project, source: Path,
                        *, overwrite: bool, actor: str) -> dict:
    with _project_lock(project.id):
        project, scan_issues = sync_project_skill_library(store, project)
        root = project_skill_library_dir(project.id)
        found, issues = _discover_skill_directories(source)
        issues = [*scan_issues, *issues]
        if not found:
            raise ValueError("没有检测到可导入的有效 SKILL.md" +
                             ("；" + "；".join(issues) if issues else ""))
        conflicts = sorted(skill_id for skill_id, _ in found if (root / skill_id).exists())
        if conflicts and not overwrite:
            return {
                "needs_confirmation": True,
                "conflicts": conflicts,
                "imported": [],
                "issues": issues,
                "path": str(root),
            }

        project_root = projects_dir() / project.id
        with tempfile.TemporaryDirectory(
                dir=project_root, prefix=".skill-import-") as temporary_name:
            staging = Path(temporary_name)
            for skill_id, directory in found:
                _copy_skill_directory(directory, staging / skill_id)
            archives = []
            for skill_id, _ in found:
                destination = root / skill_id
                if destination.exists():
                    # 延迟导入避免 recycle_bin 的恢复逻辑与本模块形成导入环。
                    from .recycle_bin import archive_replaced_skill
                    archived = archive_replaced_skill(
                        project, skill_id, destination, actor=actor)
                    shutil.rmtree(destination)
                    archives.append(archived["id"])
                (staging / skill_id).rename(destination)

        project, sync_issues = sync_project_skill_library(store, project, audit=False)
        imported = sorted(skill_id for skill_id, _ in found)
        store.audit(
            actor, "skills_imported",
            detail=(f"project={project.id} skills={imported} overwrite={overwrite} "
                    f"archives={json.dumps(archives, ensure_ascii=False)}"),
        )
        return {
            "needs_confirmation": False,
            "conflicts": conflicts,
            "imported": imported,
            "issues": [*issues, *sync_issues],
            "path": str(root),
        }


def import_skill_folder(store: Store, project: Project, source: str,
                        *, overwrite: bool = False, actor: str = "human") -> dict:
    path = Path(source).expanduser().resolve()
    root = project_skill_library_dir(project.id).resolve()
    if not path.is_dir():
        raise ValueError("本地 Skill 导入路径不是目录")
    if path == root or root.is_relative_to(path):
        raise ValueError("不能从项目 Skill 投放目录本身或其父目录导入")
    return _install_discovered(store, project, path, overwrite=overwrite, actor=actor)


def _extract_zip(payload: bytes, destination: Path) -> None:
    if not payload:
        raise ValueError("ZIP 文件为空")
    if len(payload) > MAX_SKILL_ARCHIVE_BYTES:
        raise ValueError("ZIP 文件超过 50 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("上传内容不是有效 ZIP") from exc
    total = 0
    file_count = 0
    seen_paths: set[str] = set()
    with archive:
        for info in archive.infolist():
            raw = info.filename.replace("\\", "/")
            relative = PurePosixPath(raw)
            if (not raw or relative.is_absolute()
                    or any(part in ("", ".", "..") for part in relative.parts)
                    or "\x00" in raw):
                raise ValueError(f"ZIP 包含不安全路径：{raw!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP 不允许符号链接：{raw}")
            if info.is_dir():
                continue
            normalized = relative.as_posix()
            if normalized in seen_paths:
                raise ValueError(f"ZIP 包含重复路径：{raw}")
            seen_paths.add(normalized)
            if info.flag_bits & 0x1:
                raise ValueError(f"ZIP 不支持加密文件：{raw}")
            file_count += 1
            total += info.file_size
            if file_count > MAX_SKILL_FILES:
                raise ValueError(f"ZIP 文件数量超过限制 {MAX_SKILL_FILES}")
            if info.file_size > MAX_SKILL_FILE_BYTES:
                raise ValueError(f"ZIP 单个文件超过 25 MiB：{raw}")
            if total > MAX_SKILL_TOTAL_BYTES:
                raise ValueError("ZIP 解压后总大小超过 200 MiB")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            permissions = mode & 0o777
            if permissions:
                target.chmod(permissions)


def import_skill_zip(store: Store, project: Project, payload: bytes,
                     *, overwrite: bool = False, actor: str = "human") -> dict:
    project_root = projects_dir() / project.id
    project_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            dir=project_root, prefix=".skill-zip-") as temporary_name:
        source = Path(temporary_name)
        _extract_zip(payload, source)
        return _install_discovered(
            store, project, source, overwrite=overwrite, actor=actor)
