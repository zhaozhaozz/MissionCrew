"""SQLite 持久化层。

对象类文档(项目/后端/资源/任务)按 JSON 文档存储,记录类(证据/审批/运行/审计/
统计/授权)用固定列,便于查询与审计。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .models import Backend, Channel, Project, Resource, Role, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backends  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks     (id TEXT PRIMARY KEY, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, stage TEXT NOT NULL, type TEXT NOT NULL,
  path TEXT NOT NULL, summary TEXT DEFAULT '', source TEXT DEFAULT 'agent',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, stage TEXT NOT NULL, approver TEXT NOT NULL,
  decision TEXT NOT NULL, note TEXT DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, stage TEXT NOT NULL, backend_id TEXT NOT NULL,
  tier TEXT NOT NULL, success INTEGER NOT NULL, cost REAL NOT NULL,
  summary TEXT DEFAULT '', trace TEXT DEFAULT '', workdir TEXT DEFAULT '',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, stage TEXT NOT NULL, resource_id TEXT NOT NULL,
  expires_at REAL NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  task_id TEXT DEFAULT '', detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stats (
  backend_id TEXT NOT NULL, project_id TEXT NOT NULL, task_type TEXT NOT NULL,
  success INTEGER DEFAULT 0, failure INTEGER DEFAULT 0,
  PRIMARY KEY (backend_id, project_id, task_type)
);
CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles    (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL, author TEXT NOT NULL, author_type TEXT NOT NULL,
  content TEXT NOT NULL, mentions TEXT DEFAULT '[]',
  reply_to INTEGER, root_id INTEGER, depth INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL, role_id TEXT NOT NULL, backend_id TEXT DEFAULT '',
  trigger_message_id INTEGER NOT NULL, root_id INTEGER NOT NULL,
  depth INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
  error TEXT DEFAULT '', created_at REAL NOT NULL, finished_at REAL
);
"""


class Store:
    """线程安全:聊天引擎在后台线程池中并发读写,所有访问都过同一把锁。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> int:
        """写操作,返回 lastrowid。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or 0

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- 文档对象通用 CRUD ----
    def _put(self, table: str, id: str, data: dict) -> None:
        self._execute(
            f"INSERT INTO {table}(id, data) VALUES(?, ?) "
            f"ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (id, json.dumps(data, ensure_ascii=False)),
        )

    def _get(self, table: str, id: str) -> Optional[dict]:
        rows = self._query(f"SELECT data FROM {table} WHERE id=?", (id,))
        return json.loads(rows[0]["data"]) if rows else None

    def _list(self, table: str) -> list[dict]:
        return [json.loads(r["data"]) for r in self._query(f"SELECT data FROM {table}")]

    def _delete(self, table: str, id: str) -> None:
        self._execute(f"DELETE FROM {table} WHERE id=?", (id,))

    def delete_role(self, project_id: str, id: str) -> None:
        self._delete("roles", f"{project_id}:{id}")
    def delete_backend(self, id: str) -> None: self._delete("backends", id)
    def delete_channel(self, id: str) -> None: self._delete("channels", id)

    def delete_project(self, id: str) -> None:
        """删除项目并级联其角色与频道(消息记录保留,便于审计追溯)。"""
        for r in self.list_roles(id):
            self.delete_role(id, r.id)
        for c in self.list_channels(id):
            self.delete_channel(c.id)
        self._delete("projects", id)

    # ---- Projects / Backends / Resources / Tasks ----
    def put_project(self, p: Project) -> None: self._put("projects", p.id, p.to_dict())
    def get_project(self, id: str) -> Optional[Project]:
        d = self._get("projects", id)
        return Project.from_dict(d) if d else None
    def list_projects(self) -> list[Project]:
        return [Project.from_dict(d) for d in self._list("projects")]

    def put_backend(self, b: Backend) -> None: self._put("backends", b.id, b.to_dict())
    def get_backend(self, id: str) -> Optional[Backend]:
        d = self._get("backends", id)
        return Backend.from_dict(d) if d else None
    def list_backends(self) -> list[Backend]:
        return [Backend.from_dict(d) for d in self._list("backends")]

    def put_resource(self, r: Resource) -> None: self._put("resources", r.id, r.to_dict())
    def get_resource(self, id: str) -> Optional[Resource]:
        d = self._get("resources", id)
        return Resource.from_dict(d) if d else None

    def put_task(self, t: Task) -> None:
        t.updated_at = time.time()
        self._put("tasks", t.id, t.to_dict())
    def get_task(self, id: str) -> Optional[Task]:
        d = self._get("tasks", id)
        return Task.from_dict(d) if d else None
    def list_tasks(self) -> list[Task]:
        ts = [Task.from_dict(d) for d in self._list("tasks")]
        return sorted(ts, key=lambda t: t.created_at, reverse=True)

    # ---- 证据 ----
    def add_evidence(self, task_id: str, stage: str, type: str, path: str,
                     summary: str = "", source: str = "agent") -> None:
        self._execute(
            "INSERT INTO evidence(task_id, stage, type, path, summary, source, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, stage, type, path, summary, source, time.time()),
        )

    def list_evidence(self, task_id: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM evidence WHERE task_id=? ORDER BY id", (task_id,))]

    def evidence_types(self, task_id: str) -> set[str]:
        rows = self._query("SELECT DISTINCT type FROM evidence WHERE task_id=?", (task_id,))
        return {r["type"] for r in rows}

    # ---- 审批 ----
    def add_approval(self, task_id: str, stage: str, approver: str,
                     decision: str, note: str = "") -> None:
        self._execute(
            "INSERT INTO approvals(task_id, stage, approver, decision, note, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, stage, approver, decision, note, time.time()),
        )

    def get_approval(self, task_id: str, stage: str) -> Optional[dict]:
        rows = self._query(
            "SELECT * FROM approvals WHERE task_id=? AND stage=? ORDER BY id DESC LIMIT 1",
            (task_id, stage))
        return dict(rows[0]) if rows else None

    def list_approvals(self, task_id: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM approvals WHERE task_id=? ORDER BY id", (task_id,))]

    # ---- 运行记录 / 授权 / 审计 ----
    def add_run(self, task_id: str, stage: str, backend_id: str, tier: str,
                success: bool, cost: float, summary: str, trace: str, workdir: str) -> None:
        self._execute(
            "INSERT INTO runs(task_id, stage, backend_id, tier, success, cost, summary, "
            "trace, workdir, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, stage, backend_id, tier, int(success), cost, summary,
             trace, workdir, time.time()),
        )

    def list_runs(self, task_id: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,))]

    def add_grant(self, task_id: str, stage: str, resource_id: str, expires_at: float) -> None:
        self._execute(
            "INSERT INTO grants(task_id, stage, resource_id, expires_at, created_at) "
            "VALUES(?,?,?,?,?)",
            (task_id, stage, resource_id, expires_at, time.time()),
        )

    def audit(self, actor: str, action: str, task_id: str = "", detail: str = "") -> None:
        self._execute(
            "INSERT INTO audit(ts, actor, action, task_id, detail) VALUES(?,?,?,?,?)",
            (time.time(), actor, action, task_id, detail),
        )

    def list_audit(self, task_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        if task_id:
            rows = self._query(
                "SELECT * FROM audit WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit))
        else:
            rows = self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- 成功率统计(路由器的"成功概率"依据) ----
    def stats_record(self, backend_id: str, project_id: str, task_type: str, success: bool) -> None:
        col = "success" if success else "failure"
        self._execute(
            f"INSERT INTO stats(backend_id, project_id, task_type, {col}) VALUES(?,?,?,1) "
            f"ON CONFLICT(backend_id, project_id, task_type) DO UPDATE SET {col}={col}+1",
            (backend_id, project_id, task_type),
        )

    def stats_prob(self, backend_id: str, project_id: str, task_type: str) -> float:
        """拉普拉斯平滑的历史成功率,无记录时为 0.5。"""
        rows = self._query(
            "SELECT success, failure FROM stats WHERE backend_id=? AND project_id=? AND task_type=?",
            (backend_id, project_id, task_type))
        s, f = (rows[0]["success"], rows[0]["failure"]) if rows else (0, 0)
        return (s + 1) / (s + f + 2)

    # ---- 聊天:频道 / 角色(均按项目隔离,项目是第一层级) ----
    def put_channel(self, c: Channel) -> None: self._put("channels", c.id, c.to_dict())
    def get_channel(self, id: str) -> Optional[Channel]:
        d = self._get("channels", id)
        return Channel.from_dict(d) if d else None
    def list_channels(self, project_id: Optional[str] = None) -> list[Channel]:
        cs = [Channel.from_dict(d) for d in self._list("channels")]
        if project_id is not None:
            cs = [c for c in cs if c.project_id == project_id]
        return sorted(cs, key=lambda c: c.created_at)

    def put_role(self, r: Role) -> None:
        self._put("roles", f"{r.project_id}:{r.id}", r.to_dict())
    def get_role(self, project_id: str, id: str) -> Optional[Role]:
        d = self._get("roles", f"{project_id}:{id}")
        return Role.from_dict(d) if d else None
    def list_roles(self, project_id: Optional[str] = None) -> list[Role]:
        rs = [Role.from_dict(d) for d in self._list("roles")]
        if project_id is not None:
            rs = [r for r in rs if r.project_id == project_id]
        return sorted(rs, key=lambda r: (r.project_id, r.id))

    # ---- 聊天:消息 ----
    def add_message(self, channel: str, author: str, author_type: str, content: str,
                    mentions: list[str], reply_to: Optional[int] = None,
                    root_id: Optional[int] = None, depth: int = 0) -> int:
        msg_id = self._execute(
            "INSERT INTO messages(channel, author, author_type, content, mentions, "
            "reply_to, root_id, depth, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (channel, author, author_type, content,
             json.dumps(mentions, ensure_ascii=False), reply_to, root_id, depth, time.time()),
        )
        if root_id is None:  # 人类发起的消息,自身就是协作链的根
            self._execute("UPDATE messages SET root_id=? WHERE id=?", (msg_id, msg_id))
        return msg_id

    def get_message(self, msg_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM messages WHERE id=?", (msg_id,))
        return dict(rows[0]) if rows else None

    def list_messages(self, channel: str, after_id: int = 0, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM messages WHERE channel=? AND id>? ORDER BY id LIMIT ?",
            (channel, after_id, limit))]

    def recent_messages(self, channel: str, limit: int = 20) -> list[dict]:
        rows = self._query(
            "SELECT * FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?",
            (channel, limit))
        return [dict(r) for r in reversed(rows)]

    # ---- 聊天:执行记录 ----
    def add_chat_run(self, channel: str, role_id: str, trigger_message_id: int,
                     root_id: int, depth: int) -> int:
        return self._execute(
            "INSERT INTO chat_runs(channel, role_id, trigger_message_id, root_id, depth, "
            "status, created_at) VALUES(?,?,?,?,?,'queued',?)",
            (channel, role_id, trigger_message_id, root_id, depth, time.time()),
        )

    def update_chat_run(self, run_id: int, status: str, backend_id: str = "",
                        error: str = "") -> None:
        finished = time.time() if status in ("done", "failed") else None
        self._execute(
            "UPDATE chat_runs SET status=?, backend_id=?, error=?, finished_at=? WHERE id=?",
            (status, backend_id, error, finished, run_id),
        )

    def active_chat_runs(self, channel: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM chat_runs WHERE channel=? AND status IN ('queued','running') "
            "ORDER BY id", (channel,))]

    def count_chain_runs(self, root_id: int) -> int:
        """一条协作链(同一 root 消息)累计触发的执行数,用于防爆炸。"""
        rows = self._query("SELECT COUNT(*) AS n FROM chat_runs WHERE root_id=?", (root_id,))
        return rows[0]["n"]
