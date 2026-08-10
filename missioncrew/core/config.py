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
  projects/<id>/skill-history.git                完整 Skill 包 Git 版本历史
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


# ---- pi Runtime 自有目录:全部落在 MC_HOME 下,不使用 ~/.pi 全局配置 ----

def pi_home() -> Path:
    return mc_home() / "pi"


def pi_agent_dir() -> Path:
    """pi 的配置目录(经 PI_CODING_AGENT_DIR 注入),models.json 在此。"""
    d = pi_home() / "agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pi_models_path() -> Path:
    return pi_agent_dir() / "models.json"


def pi_sessions_dir() -> Path:
    d = pi_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pi_vendor_prefix() -> Path:
    """vendored pi 的 npm --prefix 安装目录;平台不依赖系统级 pi。"""
    return pi_home() / "vendor"


def pi_vendor_bin() -> Path:
    return pi_vendor_prefix() / "node_modules" / ".bin" / "pi"


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


DEFAULT_CONTEXT_REINJECT_BYTES = 200_000
DEFAULT_CONTEXT_REINJECT_TURNS = 5


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def context_reinject_bytes() -> int:
    """增量回合累计输入/输出体积超过该字节数后强制重注入完整公共上下文。

    统计是粗略的(utf-8 字节,不折算 token);<=0 关闭按体积触发。
    环境变量 MISSIONCREW_CONTEXT_REINJECT_BYTES 覆盖。
    """
    return _int_env("MISSIONCREW_CONTEXT_REINJECT_BYTES",
                    DEFAULT_CONTEXT_REINJECT_BYTES)


def context_reinject_turns() -> int:
    """连续增量回合达到该轮数后强制重注入完整公共上下文;<=0 关闭按轮数触发。

    环境变量 MISSIONCREW_CONTEXT_REINJECT_TURNS 覆盖。
    """
    return _int_env("MISSIONCREW_CONTEXT_REINJECT_TURNS",
                    DEFAULT_CONTEXT_REINJECT_TURNS)
