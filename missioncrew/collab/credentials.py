"""凭据:人类保存、Agent 按文件读取的密钥,分全局共享与项目独有两层。

项目可用的凭据 = 全局凭据 + 本项目凭据,同名时项目凭据覆盖全局。值只出现在
两处:数据库 credentials 表的 value 列,以及每个项目的凭据目录
``projects/<id>/runtime-context/credentials/<NAME>``(目录 0700、文件 0600)。
Agent 工作区的 ``credentials/`` 链接到这个目录,自动化脚本经
``MISSIONCREW_CREDENTIALS_DIR`` 拿到它;值从不进入 Prompt、Agent Tool 结果或
任何 JSON 接口。凭据变化时立即重写受影响项目的目录,长驻会话下一条命令就能
读到新值,不用重启。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional

from ..core.config import projects_dir
from ..core.models import Credential, credential_fingerprint
from ..core.store import Store
from .github import (GITHUB_TOKEN_NAME, remove_github_cli_config,
                     sync_github_cli_config)

INDEX_FILE = "index.json"        # 凭据目录里不含值的索引:名字、用途、作用域
MAX_VALUE_BYTES = 64 * 1024
_CONTEXT_LOCK = threading.Lock()


def credential_context_dir(project_id: str) -> Path:
    return (projects_dir() / project_id / "runtime-context" / "credentials").resolve()


def effective_credentials(store: Store, project_id: str) -> list[Credential]:
    """项目可见的凭据(含待填写的),同名时项目覆盖全局,按名字排序。"""
    merged = {item.name: item for item in store.list_credentials("")}
    if project_id:
        merged.update({item.name: item for item in store.list_credentials(project_id)})
    return [merged[name] for name in sorted(merged)]


def _normalize_value(value: str) -> str:
    """去掉粘贴时带上的首尾空白;多行值(如 PEM 私钥)写文件时再补回结尾换行。"""
    if not isinstance(value, str):
        raise ValueError("凭据值必须是字符串")
    value = value.strip()
    if not value:
        raise ValueError("凭据值不能为空")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError("凭据值不能超过 64 KB")
    return value


def _check_scope(store: Store, project_id: str) -> None:
    if project_id and store.get_project(project_id) is None:
        raise LookupError(f"项目不存在: {project_id}")


def save_credential(store: Store, *, project_id: str, name: str,
                    description: Optional[str] = None,
                    value: Optional[str] = None,
                    actor: str = "human") -> tuple[Credential, bool]:
    """新建或更新凭据,返回 (凭据, 是否新建)。value 为 None 表示保留原值;
    新建时必须给值(待填写的凭据只由 Agent 经 request_credential 登记)。"""
    _check_scope(store, project_id)
    existing = store.get_credential(project_id, str(name or "").strip())
    created = existing is None
    credential = existing or Credential(name=str(name or "").strip(),
                                        project_id=project_id)
    if description is not None:
        credential.description = str(description).strip()
    if value is not None:
        value = _normalize_value(value)
        credential.has_value = True
        credential.fingerprint = credential_fingerprint(value)
        credential.requested_by = ""
    elif not credential.has_value:
        raise ValueError("请填写凭据值")
    store.put_credential(credential, value)
    store.audit(actor, "credential_saved",
                detail=(f"scope={credential.scope} project={project_id} "
                        f"name={credential.name} value_changed={value is not None}"))
    refresh_credential_contexts(store, project_id)
    return credential, created


def request_credential(store: Store, *, project_id: str, name: str,
                       description: str, actor: str) -> tuple[Credential, bool]:
    """Agent 登记一份需要人来填写的项目凭据,返回 (凭据, 是否新登记)。

    项目里已有可用的同名凭据(含继承自全局的)时抛 FileExistsError,调用方告诉
    Agent 直接读取;已经登记过、还没填写时只更新用途说明。"""
    _check_scope(store, project_id)
    name = str(name or "").strip()
    current = next((item for item in effective_credentials(store, project_id)
                    if item.name == name), None)
    if current is not None and current.has_value:
        raise FileExistsError(name)
    credential = (current if current is not None and current.project_id == project_id
                  else None)
    created = credential is None
    if credential is None:
        credential = Credential(name=name, project_id=project_id,
                                requested_by=actor)
    if description:
        credential.description = str(description).strip()
    store.put_credential(credential)
    store.audit(actor, "credential_requested",
                detail=f"project={project_id} name={name} new={created}")
    return credential, created


def delete_credential(store: Store, *, project_id: str, name: str,
                      actor: str = "human") -> Credential:
    credential = store.get_credential(project_id, name)
    if credential is None:
        raise LookupError(f"凭据不存在: {name}")
    store.delete_credential(credential.id)
    store.audit(actor, "credential_deleted",
                detail=f"scope={credential.scope} project={project_id} name={name}")
    refresh_credential_contexts(store, project_id)
    return credential


def refresh_credential_contexts(store: Store, project_id: str) -> None:
    """全局凭据变化影响所有项目,项目凭据只影响本项目。"""
    targets = [project_id] if project_id else [item.id for item in store.list_projects()]
    for target in targets:
        write_credential_context(store, target)


def _file_content(value: str) -> str:
    # 单行 token 不带换行,方便 --password-file 一类按原样读取的工具;
    # 多行值(PEM 私钥等)保留结尾换行,ssh 等工具缺了它会拒绝加载
    return value + "\n" if "\n" in value else value


def write_private_file(path: Path, content: str) -> None:
    """原子写入 0600 文件(mkstemp 建的临时文件本就是 0600);内容未变不重写。"""
    try:
        if path.read_text(encoding="utf-8") == content:
            path.chmod(0o600)
            return
    except (FileNotFoundError, UnicodeError):
        pass
    temporary: Optional[Path] = None
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


def write_credential_context(store: Store, project_id: str) -> Path:
    """按项目当前可用的凭据重写凭据目录:每份一个文件,外加不含值的索引。"""
    directory = credential_context_dir(project_id)
    with _CONTEXT_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        wanted: dict[str, str] = {}
        index = []
        for credential in effective_credentials(store, project_id):
            value = (store.credential_value(credential.project_id, credential.name)
                     if credential.has_value else "")
            if not value:
                continue
            wanted[credential.name] = value
            index.append({"name": credential.name,
                          "description": credential.description,
                          "scope": credential.scope})
        for entry in directory.iterdir():
            if entry.name == INDEX_FILE or entry.name in wanted:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for name, value in wanted.items():
            write_private_file(directory / name, _file_content(value))
        write_private_file(
            directory / INDEX_FILE,
            json.dumps({"credentials": index}, ensure_ascii=False, indent=2) + "\n")
        # GITHUB_TOKEN 同时是项目的 GitHub 身份:gh 配置与凭据文件在同一把锁里一起更新
        sync_github_cli_config(project_id, wanted.get(GITHUB_TOKEN_NAME, ""),
                               write_private_file)
    return directory


def remove_credential_context(project_id: str) -> None:
    """项目删除后清掉它的凭据目录(其中也有继承来的全局凭据文件)。"""
    with _CONTEXT_LOCK:
        shutil.rmtree(credential_context_dir(project_id), ignore_errors=True)
        remove_github_cli_config(project_id)
