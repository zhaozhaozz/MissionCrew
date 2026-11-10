"""凭据:人类保存、Agent 按文件读取的密钥,分全局共享与项目独有两层。

项目可用的凭据 = 全局凭据 + 本项目凭据,同名时项目凭据覆盖全局。值只出现在
两处:数据库 credentials 表的 value 列,以及每个项目的凭据目录
``projects/<id>/runtime-context/credentials/<NAME>``(目录 0700、文件 0600)。
Agent 工作区的 ``credentials/`` 链接到这个目录,自动化脚本经
``MISSIONCREW_CREDENTIALS_DIR`` 拿到它;值从不进入 Prompt、Agent Tool 结果或
任何 JSON 接口。凭据变化时立即重写受影响项目的目录,长驻会话下一条命令就能
读到新值,不用重启。

凭据有两种(``Credential.kind``):``secret`` 是单个保密值,对应一个文件;
``environment``(环境)是一组需要配套使用的变量(地址 + 用户名 + 密码),每个变量
各自标记是否 secret,对应目录 ``credentials/<NAME>/`` 下每个变量一个文件。secret
变量与单值凭据同样对待(只写不读、落库文本里脱敏);普通变量的值不保密,页面
和 ``index.json`` 里都直接可见,也不参与脱敏。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional

from ..core.config import projects_dir
from ..core.models import Credential, CredentialVariable, credential_fingerprint
from ..core.store import Store
from .github import (GITHUB_TOKEN_NAME, remove_github_cli_config,
                     sync_github_cli_config)

INDEX_FILE = "index.json"        # 凭据目录里的索引:名字、类型、用途、作用域,不含保密值
MAX_VALUE_BYTES = 64 * 1024
MAX_PLAIN_VALUE_BYTES = 4 * 1024     # 普通变量的值进元数据和 index.json,不放大段内容
MAX_VARIABLES = 50
_CONTEXT_LOCK = threading.Lock()


def credential_context_dir(project_id: str) -> Path:
    return (projects_dir() / project_id / "runtime-context" / "credentials").resolve()


def effective_credentials(store: Store, project_id: str) -> list[Credential]:
    """项目可见的凭据(含待填写的),同名时项目覆盖全局,按名字排序。"""
    merged = {item.name: item for item in store.list_credentials("")}
    if project_id:
        merged.update({item.name: item for item in store.list_credentials(project_id)})
    return [merged[name] for name in sorted(merged)]


def _normalize_value(value: str, *, label: str = "凭据值", plain: bool = False) -> str:
    """去掉粘贴时带上的首尾空白;多行值(如 PEM 私钥)写文件时再补回结尾换行。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    value = value.strip()
    if not value:
        raise ValueError(f"{label}不能为空")
    if plain and len(value.encode("utf-8")) > MAX_PLAIN_VALUE_BYTES:
        raise ValueError(f"{label}不能超过 4 KB;更长的内容请设为 secret")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError(f"{label}不能超过 64 KB")
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
    if existing is not None and existing.is_environment:
        raise ValueError(f"{existing.name} 是环境,请按变量填写")
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
    if current is not None and current.is_environment:
        raise ValueError(f"{name} 已经是环境;要给它加变量请带上 variables,否则换一个名字")
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


def _variable_specs(variables, *, requesting: bool = False) -> list[dict]:
    """校验一组变量声明,返回 [{name, secret, value}];value 为 None 表示没给。

    requesting=True 是 Agent 登记:secret 变量不能带值,保密值只由人在页面上填。"""
    if not isinstance(variables, list) or not variables:
        raise ValueError("环境至少要有一个变量")
    if len(variables) > MAX_VARIABLES:
        raise ValueError(f"一个环境最多 {MAX_VARIABLES} 个变量")
    specs: list[dict] = []
    for item in variables:
        if not isinstance(item, dict):
            raise ValueError("variables 的每一项是 {name, secret, value} 对象")
        name = str(item.get("name") or "").strip()
        secret = item.get("secret", True)
        if not isinstance(secret, bool):
            raise ValueError(f"变量 {name} 的 secret 只能是 true 或 false")
        CredentialVariable(name=name, secret=secret)      # 借它校验变量名
        if any(spec["name"] == name for spec in specs):
            raise ValueError(f"变量 {name} 重复")
        value = item.get("value")
        if value is not None and not isinstance(value, str):
            raise ValueError(f"变量 {name} 的值必须是字符串")
        if value is not None and not value.strip():
            value = None
        if requesting and secret and value is not None:
            raise ValueError(f"变量 {name} 是 secret,值只能由人在页面上填写,不要经参数传递")
        specs.append({"name": name, "secret": secret, "value": value})
    return specs


def _environment_name(name) -> str:
    name = str(name or "").strip()
    if name == GITHUB_TOKEN_NAME:
        raise ValueError(f"{GITHUB_TOKEN_NAME} 是 GitHub 身份,只能保存为单个凭据")
    return name


def save_environment(store: Store, *, project_id: str, name: str,
                     description: Optional[str] = None, variables: list,
                     actor: str = "human") -> tuple[Credential, bool]:
    """新建或更新环境,返回 (环境, 是否新建)。variables 是保存后的完整变量列表,
    不在其中的旧变量被删除;某个变量不带 value 表示保留原值。

    人保存的环境每个变量都要有值。secret 改成普通变量时必须重新填值:保留原值
    等于把保密值显示到页面上。"""
    _check_scope(store, project_id)
    name = _environment_name(name)
    existing = store.get_credential(project_id, name)
    if existing is not None and not existing.is_environment:
        raise ValueError(f"{name} 已经是单个凭据,不能改成环境,请换一个名字")
    created = existing is None
    credential = existing or Credential(name=name, project_id=project_id,
                                        kind="environment")
    if description is not None:
        credential.description = str(description).strip()
    old_secrets = store.credential_secrets(project_id, name) if existing else {}
    saved: list[CredentialVariable] = []
    secrets: dict[str, str] = {}
    for spec in _variable_specs(variables):
        key, secret, value = spec["name"], spec["secret"], spec["value"]
        old = credential.variable(key)
        if value is not None:
            value = _normalize_value(value, label=f"变量 {key} 的值", plain=not secret)
        else:
            kept = "" if old is None or not old.has_value else (
                old_secrets.get(key, "") if old.secret else old.value)
            if not kept:
                raise ValueError(f"请填写变量 {key} 的值")
            if old.secret and not secret:
                raise ValueError(f"变量 {key} 从 secret 改为普通变量时要重新填写值")
            value = _normalize_value(kept, label=f"变量 {key} 的值", plain=not secret)
        if secret:
            secrets[key] = value
            saved.append(CredentialVariable(name=key, secret=True, has_value=True,
                                            fingerprint=credential_fingerprint(value)))
        else:
            saved.append(CredentialVariable(name=key, secret=False, has_value=True,
                                            value=value))
    credential.variables = saved
    credential.has_value = True
    credential.requested_by = ""
    store.put_credential(credential, secrets)
    store.audit(actor, "credential_saved",
                detail=(f"scope={credential.scope} project={project_id} name={name} "
                        f"kind=environment variables={len(saved)}"))
    refresh_credential_contexts(store, project_id)
    return credential, created


def request_environment(store: Store, *, project_id: str, name: str,
                        description: str, variables: list,
                        actor: str) -> tuple[Credential, bool]:
    """Agent 登记一个环境,或给本项目已有的环境补登记变量,返回 (环境, 是否新登记)。

    Agent 只能新增变量:普通变量可以直接带上它已知的值(如用户名),secret 变量留给
    人填;已有变量的值和是否 secret 都不动。要的变量都已有值时抛 FileExistsError。
    继承来的全局环境不能由项目补变量(项目同名环境会整份盖住全局那份),抛
    ValueError 让 Agent 换名字或请人处理。"""
    _check_scope(store, project_id)
    name = _environment_name(name)
    specs = _variable_specs(variables, requesting=True)
    current = next((item for item in effective_credentials(store, project_id)
                    if item.name == name), None)
    if current is not None and not current.is_environment:
        raise ValueError(f"{name} 已经是单个凭据,不能改成环境,请换一个名字")
    missing = [spec["name"] for spec in specs
               if current is None or not getattr(current.variable(spec["name"]),
                                                 "has_value", False)]
    if current is not None and not missing:
        raise FileExistsError(name)
    if current is not None and current.project_id != project_id:
        raise ValueError(
            f"{name} 是全局环境,其中没有变量 {'、'.join(missing)};"
            "请换一个环境名,或请人在全局设置里补上")
    created = current is None
    credential = current or Credential(name=name, project_id=project_id,
                                       kind="environment")
    # 已有环境的用途说明可能是人写的,补登记变量时不覆盖
    if description and not credential.description:
        credential.description = str(description).strip()
    for spec in specs:
        if credential.variable(spec["name"]) is not None:
            continue
        value = spec["value"]
        if value is not None:
            value = _normalize_value(value, label=f"变量 {spec['name']} 的值", plain=True)
        credential.variables.append(CredentialVariable(
            name=spec["name"], secret=spec["secret"], has_value=value is not None,
            value=value or ""))
    if len(credential.variables) > MAX_VARIABLES:
        raise ValueError(f"一个环境最多 {MAX_VARIABLES} 个变量")
    credential.has_value = all(item.has_value for item in credential.variables)
    if not credential.has_value:
        credential.requested_by = actor
    store.put_credential(credential)
    store.audit(actor, "credential_requested",
                detail=(f"project={project_id} name={name} kind=environment new={created} "
                        f"missing={','.join(missing)}"))
    # Agent 带来的普通变量值已经可用,连同"哪些变量还缺"一起写进凭据目录
    refresh_credential_contexts(store, project_id)
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
    """按项目当前可用的凭据重写凭据目录:单值凭据一个文件,环境一个目录(每个有值的
    变量一个文件),外加索引。索引不含保密值,普通变量的值直接写在里面。"""
    directory = credential_context_dir(project_id)
    with _CONTEXT_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        files: dict[str, str] = {}                    # 单值凭据:文件名 -> 值
        groups: dict[str, dict[str, str]] = {}        # 环境:目录名 -> {变量 -> 值}
        index = []
        for credential in effective_credentials(store, project_id):
            entry = {"name": credential.name, "type": credential.kind,
                     "description": credential.description, "scope": credential.scope}
            if credential.is_environment:
                secrets = store.credential_secrets(credential.project_id, credential.name)
                values: dict[str, str] = {}
                listed = []
                for variable in credential.variables:
                    value = (secrets.get(variable.name, "") if variable.secret
                             else variable.value) if variable.has_value else ""
                    item = {"name": variable.name, "secret": variable.secret,
                            "available": bool(value)}
                    if value:
                        values[variable.name] = value
                        if not variable.secret:
                            item["value"] = value
                    listed.append(item)
                if not values:
                    continue
                groups[credential.name] = values
                index.append({**entry, "variables": listed})
                continue
            value = (store.credential_value(credential.project_id, credential.name)
                     if credential.has_value else "")
            if not value:
                continue
            files[credential.name] = value
            index.append(entry)
        for entry in directory.iterdir():
            is_directory = entry.is_dir() and not entry.is_symlink()
            # 同名条目换了类型(项目环境盖住全局单值凭据等)时,旧形态先清掉
            if entry.name == INDEX_FILE or (entry.name in files and not is_directory) \
                    or (entry.name in groups and is_directory):
                continue
            if is_directory:
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for name, value in files.items():
            write_private_file(directory / name, _file_content(value))
        for name, values in groups.items():
            group = directory / name
            group.mkdir(exist_ok=True)
            group.chmod(0o700)
            for entry in group.iterdir():
                if entry.name in values and entry.is_file() and not entry.is_symlink():
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            for variable, value in values.items():
                write_private_file(group / variable, _file_content(value))
        write_private_file(
            directory / INDEX_FILE,
            json.dumps({"credentials": index}, ensure_ascii=False, indent=2) + "\n")
        # GITHUB_TOKEN 同时是项目的 GitHub 身份:gh 配置与凭据文件在同一把锁里一起更新
        sync_github_cli_config(project_id, files.get(GITHUB_TOKEN_NAME, ""),
                               write_private_file)
    return directory


def remove_credential_context(project_id: str) -> None:
    """项目删除后清掉它的凭据目录(其中也有继承来的全局凭据文件)。"""
    with _CONTEXT_LOCK:
        shutil.rmtree(credential_context_dir(project_id), ignore_errors=True)
        remove_github_cli_config(project_id)
