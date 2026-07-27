"""平台运行目录与全局配置。

MC_HOME 布局:
  db.sqlite3        平台数据库
  workspaces/<task> 旧阶段式 Task 的历史工作区（仅迁移兼容）
  agent-workspaces/<project>/channels/<channel>/<role>/.missioncrew
                    聊天角色隔离的 Agent harness 工作区
  projects/<id>/documents              项目文档库普通目录
  projects/<id>/document-history.git   文档库 Git 版本历史(与工作目录分离)
  projects/<id>/guidelines             项目准则 Markdown 普通目录
  projects/<id>/guideline-history.git  准则 Git 版本历史(与工作目录分离)
  projects/<id>/runtime-context/guidelines/*.md  Runtime 按需读取的准则全文
  projects/<id>/skills/<skill-id>/SKILL.md       项目完整 Skill 投放目录
  projects/<id>/runtime-context/skills/<skill-id> 已启用 Skill 的共享链接视图
  projects/<id>/recycle-bin/<item-id>             项目资源统一回收站
  secrets.yaml      受控资源的密钥(secret_ref -> value),不进入任何 Prompt
"""
from __future__ import annotations

import os
from pathlib import Path


def mc_home() -> Path:
    """平台主目录:环境变量 MISSIONCREW_HOME 优先,默认当前目录下 .missioncrew/。"""
    home = Path(os.environ.get("MISSIONCREW_HOME", Path.cwd() / ".missioncrew"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def db_path() -> Path:
    return mc_home() / "db.sqlite3"


def workspaces_dir() -> Path:
    d = mc_home() / "workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def projects_dir() -> Path:
    d = mc_home() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def secrets_path() -> Path:
    return mc_home() / "secrets.yaml"


DEFAULT_CHAT_MAX_WORKERS = 16


def chat_max_workers() -> int:
    """聊天执行线程池大小:全部项目/频道/角色共享的并发执行上限。

    环境变量 MISSIONCREW_CHAT_MAX_WORKERS 覆盖,非法值回落默认;
    同频道同角色仍按会话锁串行,该值只决定不同会话间的并行度。
    """
    raw = os.environ.get("MISSIONCREW_CHAT_MAX_WORKERS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CHAT_MAX_WORKERS
    return max(1, value)
