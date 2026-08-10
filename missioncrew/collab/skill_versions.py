"""完整 Skill 目录的项目级 Git 版本历史。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath

from ..core.config import projects_dir


_ID_RE = re.compile(r"[\w-]+")
_REVISION_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_COMPARE_TEXT_LIMIT = 1024 * 1024


def _project_lock(project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


def _clean_identity(value: str, fallback: str) -> str:
    return " ".join(str(value).split())[:80] or fallback


def _safe_relative(value: str) -> str:
    raw = str(value).replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if (not raw or raw.endswith("/") or path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)
            or "\x00" in raw):
        raise ValueError("Skill 文件路径必须是 Skill 目录内的相对路径")
    return path.as_posix()


class SkillVersionLibrary:
    """以临时工作树提交有效 Skill 包，历史仓库不暴露给 Runtime。"""

    def __init__(self, project_id: str):
        if not _ID_RE.fullmatch(project_id):
            raise ValueError("项目 id 只能包含字母、数字、下划线、连字符")
        self.project_id = project_id
        self.project_root = projects_dir() / project_id
        self.repo = self.project_root / "skill-history.git"
        self.marker = self.project_root / ".skill-history-version"
        self._lock = _project_lock(project_id)
        with self._lock:
            self.project_root.mkdir(parents=True, exist_ok=True)
            if not self.repo.exists():
                subprocess.run(
                    ["git", "init", "--bare", "--quiet", str(self.repo)],
                    check=True, capture_output=True, text=True,
                )
                with tempfile.TemporaryDirectory(
                        dir=self.project_root, prefix=".skill-history-init-") as worktree:
                    self._commit(
                        Path(worktree), "platform",
                        "Initialize project Skill history", allow_empty=True)

    def _git(self, *args: str, worktree: Path | None = None,
             check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", f"--git-dir={self.repo}"]
        if worktree is not None:
            cmd.append(f"--work-tree={worktree}")
        cmd.extend(args)
        return subprocess.run(
            cmd, check=check, capture_output=True, text=text)

    def _commit(self, worktree: Path, actor: str, message: str,
                *, allow_empty: bool = False) -> str:
        self._git("add", "--all", worktree=worktree)
        if (not allow_empty and self._git(
                "diff", "--cached", "--quiet", worktree=worktree,
                check=False).returncode == 0):
            return self.head()
        clean_actor = _clean_identity(actor, "platform")
        clean_message = _clean_identity(message, "Update project Skills")[:200]
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": clean_actor,
            "GIT_AUTHOR_EMAIL": "missioncrew@local",
            "GIT_COMMITTER_NAME": clean_actor,
            "GIT_COMMITTER_EMAIL": "missioncrew@local",
        }
        subprocess.run(
            ["git", f"--git-dir={self.repo}", f"--work-tree={worktree}",
             "commit", "--quiet", "--allow-empty", "-m", clean_message],
            check=True, capture_output=True, text=True, env=env,
        )
        return self.head()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def _snapshot_digest(directories: dict[str, Path]) -> str:
        digest = hashlib.sha256()
        for skill_id, directory in sorted(directories.items()):
            digest.update(skill_id.encode("utf-8"))
            digest.update(b"\0")
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(directory).as_posix()
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
        return digest.hexdigest()

    def _write_marker(self, revision: str, digest: str) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.project_root,
                    prefix=".skill-history-version.", delete=False) as handle:
                handle.write(f"{revision} {digest}\n")
                temporary = Path(handle.name)
            temporary.replace(self.marker)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def record(self, directories: dict[str, Path], *, actor: str,
               message: str) -> str:
        """提交当前全部有效 Skill；内容未变化时返回现有 HEAD。"""
        for skill_id, directory in directories.items():
            if not _ID_RE.fullmatch(skill_id) or directory.is_symlink() \
                    or not directory.is_dir():
                raise ValueError(f"无效 Skill 目录: {skill_id}")
        with self._lock:
            digest = self._snapshot_digest(directories)
            head = self.head()
            try:
                marker_revision, marker_digest = self.marker.read_text(
                    encoding="utf-8").strip().split(" ", 1)
            except (FileNotFoundError, OSError, UnicodeError, ValueError):
                marker_revision = marker_digest = ""
            if marker_revision == head and marker_digest == digest:
                return head
            with tempfile.TemporaryDirectory(
                    dir=self.project_root, prefix=".skill-history-worktree-") as name:
                worktree = Path(name)
                for skill_id, directory in sorted(directories.items()):
                    shutil.copytree(directory, worktree / skill_id)
                revision = self._commit(worktree, actor, message)
            self._write_marker(revision, digest)
            return revision

    def _revision(self, revision: str) -> str:
        if not _REVISION_RE.fullmatch(str(revision)):
            raise ValueError("版本号不合法")
        proc = self._git(
            "rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
        if proc.returncode:
            raise FileNotFoundError(f"Skill 版本不存在: {revision}")
        return proc.stdout.strip()

    @staticmethod
    def _skill_id(skill_id: str) -> str:
        if not _ID_RE.fullmatch(str(skill_id)):
            raise ValueError("Skill id 只能包含字母、数字、下划线、连字符")
        return str(skill_id)

    def history(self, skill_id: str, limit: int = 100) -> list[dict]:
        skill_id = self._skill_id(skill_id)
        proc = self._git(
            "log", f"--max-count={max(1, min(limit, 500))}",
            "--format=%H%x1f%at%x1f%an%x1f%s", "--", skill_id,
            check=False)
        if proc.returncode:
            return []
        rows = []
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) == 4:
                rows.append({
                    "revision": parts[0], "created_at": float(parts[1]),
                    "actor": parts[2], "message": parts[3],
                })
        return rows

    def current_revision(self, skill_id: str) -> str:
        rows = self.history(skill_id, 1)
        return rows[0]["revision"] if rows else ""

    def files(self, skill_id: str, revision: str) -> list[dict]:
        skill_id = self._skill_id(skill_id)
        resolved = self._revision(revision)
        proc = self._git(
            "ls-tree", "-r", "-l", "-z", resolved, "--", skill_id,
            check=False)
        if proc.returncode:
            raise FileNotFoundError(f"版本 {revision} 中不存在 Skill {skill_id}")
        files = []
        prefix = f"{skill_id}/"
        for record in proc.stdout.split("\0"):
            if not record:
                continue
            metadata, path = record.split("\t", 1)
            mode, kind, object_id, size = metadata.split(None, 3)
            if kind != "blob" or not path.startswith(prefix):
                continue
            files.append({
                "path": path[len(prefix):], "size": int(size),
                "hash": object_id, "mode": mode,
            })
        if not files:
            raise FileNotFoundError(f"版本 {revision} 中不存在 Skill {skill_id}")
        return files

    def read_bytes(self, skill_id: str, relative: str, revision: str) -> bytes:
        skill_id = self._skill_id(skill_id)
        relative = _safe_relative(relative)
        resolved = self._revision(revision)
        proc = self._git(
            "show", f"{resolved}:{skill_id}/{relative}",
            check=False, text=False)
        if proc.returncode:
            raise FileNotFoundError(
                f"版本 {revision} 中不存在 Skill 文件 {relative}")
        return proc.stdout

    def read_text(self, skill_id: str, relative: str,
                  revision: str) -> tuple[str, bool]:
        data = self.read_bytes(skill_id, relative, revision)
        truncated = len(data) > 512 * 1024
        preview = data[:512 * 1024] if truncated else data
        if b"\x00" in preview:
            raise ValueError("二进制文件不支持在线查看")
        return preview.decode("utf-8", errors="replace"), truncated

    def package_info(self, skill_id: str, revision: str) -> dict:
        resolved = self._revision(revision)
        files = self.files(skill_id, resolved)
        markdown = self.read_bytes(skill_id, "SKILL.md", resolved).decode("utf-8")
        tree = self._git("rev-parse", f"{resolved}:{skill_id}").stdout.strip()
        return {
            "id": skill_id, "revision": resolved, "markdown": markdown,
            "files": [item["path"] for item in files],
            "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files),
            "content_version": tree[:16],
        }

    def comparison_text(self, skill_id: str, revision: str) -> str:
        blocks = []
        for item in self.files(skill_id, revision):
            data = self.read_bytes(skill_id, item["path"], revision)
            marker = None
            if len(data) > _COMPARE_TEXT_LIMIT:
                marker = f"<large file: {len(data)} bytes, sha256={hashlib.sha256(data).hexdigest()}>"
            elif b"\x00" in data:
                marker = f"<binary: {len(data)} bytes, sha256={hashlib.sha256(data).hexdigest()}>"
            else:
                try:
                    marker = data.decode("utf-8")
                except UnicodeDecodeError:
                    marker = (f"<non-UTF-8: {len(data)} bytes, "
                              f"sha256={hashlib.sha256(data).hexdigest()}>")
            blocks.append(f"===== {item['path']} =====\n{marker}\n")
        return "\n".join(blocks)

    def file_changes(self, skill_id: str, before: str, after: str) -> list[dict]:
        old = {item["path"]: item for item in self.files(skill_id, before)}
        new = {item["path"]: item for item in self.files(skill_id, after)}
        changes = []
        for path in sorted(set(old) | set(new)):
            if path not in old:
                status = "added"
            elif path not in new:
                status = "deleted"
            elif (old[path]["hash"], old[path]["mode"]) != \
                    (new[path]["hash"], new[path]["mode"]):
                status = "modified"
            else:
                continue
            changes.append({"path": path, "status": status})
        return changes

    def restore_files(self, skill_id: str, revision: str, target_root: Path) -> None:
        """原子替换当前 Skill 目录；调用方负责重新扫描并提交恢复版本。"""
        skill_id = self._skill_id(skill_id)
        resolved = self._revision(revision)
        files = self.files(skill_id, resolved)
        target = target_root / skill_id
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError("Skill 恢复目标必须是普通目录")
        with tempfile.TemporaryDirectory(
                dir=target_root.parent, prefix=".skill-restore-") as name:
            temporary = Path(name)
            restored = temporary / skill_id
            restored.mkdir()
            for item in files:
                if item["mode"] not in {"100644", "100755"}:
                    raise ValueError(f"历史 Skill 包含不支持的文件类型: {item['path']}")
                destination = restored / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(
                    self.read_bytes(skill_id, item["path"], resolved))
                destination.chmod(0o755 if item["mode"] == "100755" else 0o644)
            backup = temporary / "backup"
            try:
                if target.exists():
                    target.rename(backup)
                restored.rename(target)
            except Exception:
                if target.exists():
                    shutil.rmtree(target)
                if backup.exists():
                    backup.rename(target)
                raise


def skill_version_library(project_id: str) -> SkillVersionLibrary:
    return SkillVersionLibrary(project_id)
