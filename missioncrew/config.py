"""平台运行目录与全局配置。

MC_HOME 布局:
  db.sqlite3        平台数据库
  workspaces/<task> 每个任务一个隔离工作区(跨阶段共享,证据累积在 evidence/)
  projects/<id>/documents              项目文档库普通目录
  projects/<id>/document-history.git   文档库 Git 版本历史(与工作目录分离)
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
