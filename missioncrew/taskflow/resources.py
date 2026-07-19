"""受控资源系统:任务级、阶段级、限时、可审计的资源授权。

敏感值(Token、凭据)只存在于 MC_HOME/secrets.yaml,由平台在进程启动时注入
环境变量,从不写入 Prompt、Skill 或任务描述;每次授权都记录 grants 与 audit。
"""
from __future__ import annotations

import os
import time
from typing import Optional

import yaml

from ..core.config import secrets_path
from ..core.models import Project, Task, TaskStage
from ..core.store import Store


def _resolve_secret(ref: str) -> Optional[str]:
    """secrets.yaml 优先,其次同名环境变量;都没有则视为未配置。"""
    p = secrets_path()
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
        if ref in data:
            return str(data[ref])
    return os.environ.get(ref)


def grant_resources(store: Store, task: Task, stage: TaskStage,
                    project: Project) -> tuple[dict, list[str]]:
    """为当前阶段授权项目声明的受控资源。

    返回 (注入执行环境的变量表, 给 Agent 看的资源说明——只有描述,没有值)。
    """
    env: dict = {}
    notes: list[str] = []
    for rid in project.resources:
        res = store.get_resource(rid)
        if res is None:
            continue
        # 阶段范围与密级双重校验,不由 Agent 自己判断
        if res.stages and stage.name not in res.stages:
            continue
        if res.security_level > task.security_level:
            notes.append(f"- {res.id}: 密级不足,未授权")
            store.audit("platform", "resource_denied", task.id,
                        f"stage={stage.name} resource={rid} 密级不足")
            continue
        value = _resolve_secret(res.secret_ref) if res.secret_ref else None
        if res.env and value is not None:
            env[res.env] = value
        expires = time.time() + res.ttl_seconds
        store.add_grant(task.id, stage.name, rid, expires)
        store.audit("platform", "resource_granted", task.id,
                    f"stage={stage.name} resource={rid} env={res.env} ttl={res.ttl_seconds}s")
        notes.append(f"- {res.id}({res.kind}): {res.description};已注入环境变量 {res.env}")
    return env, notes
