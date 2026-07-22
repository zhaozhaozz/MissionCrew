"""项目版本文件库：普通目录作为工作树，独立 Git 仓库保存历史。"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from ..core.config import projects_dir
from .resource_urls import missioncrew_resource_url

_PROJECT_ID_RE = re.compile(r"[\w-]+")
_REVISION_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _project_lock(project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


def safe_relative_path(value: str) -> str:
    """校验 API/Prompt 中的文档引用，禁止绝对路径和目录逃逸。"""
    raw = value.replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if (not raw or raw.endswith("/") or path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ValueError("文档路径必须是文档库内的相对路径")
    if path.parts[0] == ".git" or "\x00" in raw:
        raise ValueError("文档路径不合法")
    return path.as_posix()


def document_resource_url(project_id: str, relative: str | None = None) -> str:
    """返回不暴露平台数据目录的、可由 Web 打开的项目文档 URL。"""
    base = missioncrew_resource_url(project_id, "documents")
    if relative is None:
        return base
    rel = safe_relative_path(relative)
    return missioncrew_resource_url(project_id, "documents", *rel.split("/"))


def normalize_document_resource_urls(text: str, project_id: str,
                                     local_roots: list[str | Path]) -> str:
    """把 Agent 回复里的文档库真实目录替换成 MissionCrew 资源 URL。

    Runtime 仍需真实路径读写，但频道消息、动作消息和失败摘要不能把平台
    数据目录当成 Web 链接发布。workspace 符号链接使用单独模式兼容。
    """
    normalized = str(text)
    resource_root = document_resource_url(project_id)
    roots: set[str] = set()
    for value in local_roots:
        if not str(value):
            continue
        path = Path(value).expanduser()
        roots.add(str(path).rstrip("/"))
        try:
            roots.add(str(path.resolve()).rstrip("/"))
        except OSError:
            pass
    for root in sorted(roots, key=len, reverse=True):
        if root:
            normalized = re.sub(
                re.escape(root) + r"(?=$|[/\s`\"'()<>\[\]])",
                resource_root, normalized)

    # Agent 常回显 harness 中的 documents 符号链接，而不是它解析后的真实
    # 文档库目录。只替换链接根；后续相对路径原样保留为资源 URL 路径。
    safe_project = re.escape(project_id)
    workspace_root = re.compile(
        rf"/?[^\s`\"'()<>\[\]]*?\.missioncrew/agent-workspaces/"
        rf"{safe_project}/[^\s`\"'()<>\[\]]*?/\.missioncrew/documents"
    )
    normalized = workspace_root.sub(resource_root, normalized)
    task_root = re.compile(
        r"/?[^\s`\"'()<>\[\]]*?\.missioncrew/workspaces/"
        r"[^\s`\"'()<>\[\]]*?/\.missioncrew/documents"
    )
    return task_root.sub(resource_root, normalized)


class VersionedFileLibrary:
    """普通文件工作树 + 独立 bare Git 的共享版本存储。

    `root` 不包含 `.git`，因此交给任意 Runtime 时就是普通目录；Git 元数据
    保存在同级独立目录，Runtime 无法误改历史。
    """

    def __init__(self, project_id: str, directory_name: str,
                 history_name: str, label: str):
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("项目 id 只能包含字母、数字、下划线、连字符")
        project_root = projects_dir() / project_id
        self.root = project_root / directory_name
        self.repo = project_root / history_name
        self.label = label
        self._lock = _project_lock(project_id)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.repo.exists():
                project_root.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "init", "--bare", "--quiet", str(self.repo)],
                    check=True, capture_output=True, text=True,
                )
                self.commit_changes(
                    "platform", f"Initialize project {label} library", allow_empty=True)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["git", f"--git-dir={self.repo}", f"--work-tree={self.root}", *args]
        with self._lock:
            return subprocess.run(cmd, check=check, capture_output=True, text=True)

    def _path(self, relative: str) -> tuple[str, Path]:
        rel = safe_relative_path(relative)
        candidate = self.root / rel
        root = self.root.resolve()
        resolved_parent = candidate.parent.resolve()
        if not resolved_parent.is_relative_to(root):
            raise ValueError("文档路径不能离开文档库")
        if candidate.is_symlink() or (candidate.exists() and not candidate.resolve().is_relative_to(root)):
            raise ValueError("文档路径不能通过符号链接离开文档库")
        return rel, candidate

    def list_files(self) -> list[dict]:
        files = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
            files.append({
                "path": path.relative_to(self.root).as_posix(),
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            })
        return files

    def read(self, relative: str, revision: str | None = None) -> str:
        rel, path = self._path(relative)
        if revision:
            if not _REVISION_RE.fullmatch(revision):
                raise ValueError("版本号不合法")
            proc = self._git("show", f"{revision}:{rel}", check=False)
            if proc.returncode:
                raise FileNotFoundError(f"版本 {revision} 中不存在{self.label} {rel}")
            return proc.stdout
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"{self.label}不存在: {rel}")
        return path.read_text(encoding="utf-8")

    def read_bytes(self, relative: str, revision: str | None = None) -> bytes:
        """读取任意文档字节，供二进制下载和历史版本下载使用。"""
        with self._lock:
            rel, path = self._path(relative)
            if revision:
                if not _REVISION_RE.fullmatch(revision):
                    raise ValueError("版本号不合法")
                proc = subprocess.run(
                    ["git", f"--git-dir={self.repo}", f"--work-tree={self.root}",
                     "show", f"{revision}:{rel}"],
                    check=False, capture_output=True,
                )
                if proc.returncode:
                    raise FileNotFoundError(
                        f"版本 {revision} 中不存在{self.label} {rel}")
                return proc.stdout
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(f"{self.label}不存在: {rel}")
            return path.read_bytes()

    def write(self, relative: str, content: str, actor: str = "human",
              message: str = "") -> str:
        with self._lock:
            rel, path = self._path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return self.commit_changes(actor, message or f"Update {rel}")

    def write_bytes(self, relative: str, content: bytes, actor: str = "human",
                    message: str = "", overwrite: bool = False) -> str:
        """保存上传的原始字节并记录文档库版本。"""
        with self._lock:
            rel, path = self._path(relative)
            if path.exists() and not path.is_file():
                raise ValueError(f"{self.label}路径不是普通文件: {rel}")
            if path.is_file() and not overwrite:
                raise FileExistsError(f"{self.label}已存在: {rel}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return self.commit_changes(actor, message or f"Upload {rel}")

    def delete(self, relative: str, actor: str = "human") -> str:
        with self._lock:
            rel, path = self._path(relative)
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(f"{self.label}不存在: {rel}")
            path.unlink()
            return self.commit_changes(actor, f"Delete {rel}")

    def rename(self, source: str, target: str, actor: str = "human") -> str:
        """以独立 commit 重命名文件，让 ``git log --follow`` 稳定追踪旧路径。"""
        with self._lock:
            source_rel, source_path = self._path(source)
            target_rel, target_path = self._path(target)
            if not source_path.is_file() or source_path.is_symlink():
                raise FileNotFoundError(f"{self.label}不存在: {source_rel}")
            if target_path.exists():
                raise FileExistsError(f"{self.label}已存在: {target_rel}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.rename(target_path)
            return self.commit_changes(
                actor, f"Rename {source_rel} to {target_rel}")

    def restore(self, relative: str, revision: str, actor: str = "human") -> str:
        """把某文件恢复到历史版本(作为新版本写入,历史保持完整可追溯)。"""
        with self._lock:
            content = self.read_history_bytes(relative, revision)  # 兼容文本和二进制
            rel, path = self._path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return self.commit_changes(actor, f"Restore {rel} to {revision[:10]}")

    def commit_changes(self, actor: str, message: str, allow_empty: bool = False) -> str:
        """把普通目录中的直接写入快照成一个版本；无变化时返回空字符串。"""
        with self._lock:
            self._git("add", "--all")
            if not allow_empty and self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return ""
            clean_actor = " ".join(actor.split())[:80] or "platform"
            clean_message = " ".join(message.split())[:200] or f"Update project {self.label}"
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": clean_actor,
                "GIT_AUTHOR_EMAIL": "missioncrew@local",
                "GIT_COMMITTER_NAME": clean_actor,
                "GIT_COMMITTER_EMAIL": "missioncrew@local",
            }
            cmd = ["git", f"--git-dir={self.repo}", f"--work-tree={self.root}",
                   "commit", "--quiet", "--allow-empty", "-m", clean_message]
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
            return self._git("rev-parse", "HEAD").stdout.strip()

    def history(self, relative: str | None = None, limit: int = 100) -> list[dict]:
        args = ["log", f"--max-count={max(1, min(limit, 500))}",
                "--format=%H%x1f%at%x1f%an%x1f%s"]
        if relative:
            rel, _ = self._path(relative)
            # 记录每个 commit 当时的真实文件名，重命名后的历史版本仍可读取。
            args = ["-c", "core.quotepath=false", "log", "--follow",
                    f"--max-count={max(1, min(limit, 500))}",
                    "--format=%x1e%H%x1f%at%x1f%an%x1f%s", "--name-only", "--", rel]
        proc = self._git(*args, check=False)
        if proc.returncode:
            return []
        result = []
        if relative:
            for record in proc.stdout.split("\x1e"):
                lines = [line for line in record.splitlines() if line.strip()]
                if not lines:
                    continue
                parts = lines[0].split("\x1f", 3)
                if len(parts) != 4:
                    continue
                revision_path = next(
                    (path for path in reversed(lines[1:])
                     if self._git("cat-file", "-e", f"{parts[0]}:{path}",
                                  check=False).returncode == 0),
                    rel,
                )
                result.append({
                    "revision": parts[0], "created_at": float(parts[1]),
                    "actor": parts[2], "message": parts[3],
                    "path": revision_path,
                })
            return result
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) == 4:
                result.append({
                    "revision": parts[0], "created_at": float(parts[1]),
                    "actor": parts[2], "message": parts[3],
                })
        return result

    def read_history_bytes(self, relative: str, revision: str) -> bytes:
        """按历史记录里的当时路径读取版本，兼容文件重命名。"""
        if not _REVISION_RE.fullmatch(revision):
            raise ValueError("版本号不合法")
        row = next((item for item in self.history(relative, 500)
                    if item["revision"] == revision), None)
        if row is None:
            raise FileNotFoundError(f"{self.label}版本不存在: {revision}")
        return self.read_bytes(row.get("path") or relative, revision)

    def read_history(self, relative: str, revision: str) -> str:
        return self.read_history_bytes(relative, revision).decode("utf-8")


class DocumentLibrary(VersionedFileLibrary):
    def __init__(self, project_id: str):
        super().__init__(project_id, "documents", "document-history.git", "文档")


class GuidelineLibrary(VersionedFileLibrary):
    def __init__(self, project_id: str):
        super().__init__(project_id, "guidelines", "guideline-history.git", "准则")


def library_for(project_id: str) -> DocumentLibrary:
    return DocumentLibrary(project_id)


def guideline_library_for(project_id: str) -> GuidelineLibrary:
    return GuidelineLibrary(project_id)


def archive_library(project_id: str) -> str:
    """删除项目时归档文档、准则及历史，避免重建同名项目读到旧资料。"""
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("项目 id 只能包含字母、数字、下划线、连字符")
    with _project_lock(project_id):
        source = projects_dir() / project_id
        if not source.exists():
            return ""
        archive_root = projects_dir() / ".archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / f"{project_id}-{int(time.time() * 1000)}"
        source.rename(target)
        return str(target)
