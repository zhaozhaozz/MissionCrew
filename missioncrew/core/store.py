"""SQLite 持久化层。

对象类文档(项目/后端/资源/任务)按 JSON 文档存储,记录类(证据/审批/运行/审计/
统计/授权)用固定列,便于查询与审计。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .models import Backend, Board, Channel, Project, Resource, Role, Task

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
CREATE TABLE IF NOT EXISTS role_templates (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS boards   (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL, author TEXT NOT NULL, author_type TEXT NOT NULL,
  content TEXT NOT NULL, mentions TEXT DEFAULT '[]',
  mention_spans TEXT DEFAULT '[]',
  kind TEXT DEFAULT 'message',
  runtime_id TEXT DEFAULT '', model TEXT DEFAULT '', effort TEXT DEFAULT '',
  reply_to INTEGER, root_id INTEGER, depth INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_created
  ON messages(channel, created_at DESC);
CREATE TABLE IF NOT EXISTS chat_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL, role_id TEXT NOT NULL, backend_id TEXT DEFAULT '',
  trigger_message_id INTEGER NOT NULL, root_id INTEGER NOT NULL,
  depth INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
  error TEXT DEFAULT '', created_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
  session_key TEXT PRIMARY KEY,
  channel TEXT NOT NULL, role_id TEXT NOT NULL,
  backend_id TEXT NOT NULL, adapter TEXT NOT NULL,
  workdir TEXT NOT NULL, runtime_session_id TEXT DEFAULT '',
  context_version TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);
CREATE TABLE IF NOT EXISTS runtime_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backend_id TEXT NOT NULL, adapter TEXT NOT NULL,
  mode TEXT NOT NULL, transport TEXT NOT NULL,
  task_id TEXT DEFAULT '', stage_name TEXT DEFAULT '',
  project_id TEXT DEFAULT '', role_id TEXT DEFAULT '',
  session_key TEXT DEFAULT '', model TEXT DEFAULT '', effort TEXT DEFAULT '',
  workdir TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'running',
  success INTEGER, summary TEXT DEFAULT '', owner_pid INTEGER NOT NULL,
  started_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runtime_usage_started
  ON runtime_usage(started_at DESC);
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
        self._migrate_chat_messages()
        self._migrate_runtime_usage()
        self._migrate_harness_paths()
        self._conn.commit()

    def _migrate_harness_paths(self) -> int:
        """旧任务证据从工作区根目录迁入 `.missioncrew/evidence/`。"""
        cursor = self._conn.execute(
            "UPDATE evidence SET path='.missioncrew/' || path "
            "WHERE path LIKE 'evidence/%'")
        return cursor.rowcount

    def _migrate_chat_messages(self) -> int:
        """升级聊天消息字段，并修复旧版末尾 4000 字符回复。

        旧数据库不会因 ``CREATE TABLE IF NOT EXISTS`` 自动增加列；旧适配器又把
        完整回复截成末尾 4000 字符，但完整文本仍保留在 run_events 中。迁移按
        message.reply_to + author 找到对应执行，仅在事件文本明确以旧正文结尾时
        恢复，避免猜测或误改普通的 4000 字符消息。
        """
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(messages)").fetchall()}
        for name in ("runtime_id", "model", "effort", "kind", "mention_spans"):
            if name not in columns:
                default = ("'message'" if name == "kind" else
                           "'[]'" if name == "mention_spans" else "''")
                self._conn.execute(
                    f"ALTER TABLE messages ADD COLUMN {name} TEXT DEFAULT {default}")

        rows = self._conn.execute(
            "SELECT m.*, r.id AS run_id, r.backend_id AS run_backend_id "
            "FROM messages m LEFT JOIN chat_runs r ON r.id=("
            "  SELECT r2.id FROM chat_runs r2 "
            "  WHERE r2.trigger_message_id=m.reply_to AND r2.role_id=m.author "
            "  ORDER BY r2.id DESC LIMIT 1"
            ") WHERE m.author_type='agent' AND ("
            "  m.runtime_id='' OR m.model='' OR m.effort='' OR LENGTH(m.content)=4000"
            ") ORDER BY m.id"
        ).fetchall()
        changed = 0
        for row in rows:
            runtime_id, model, effort = row["runtime_id"], row["model"], row["effort"]
            content = row["content"]
            run_id = row["run_id"]
            events = self._conn.execute(
                "SELECT kind, content FROM run_events WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall() if run_id else []

            if run_id and (not runtime_id or not model or not effort):
                prompt = "".join(e["content"] for e in events if e["kind"] == "input")
                combo = next((line.removeprefix("固定执行组合:")
                              for line in prompt.splitlines()
                              if line.startswith("固定执行组合:")), "")
                if combo:
                    parsed_runtime, sep, model_effort = combo.partition("/")
                    parsed_model, effort_sep, parsed_effort = model_effort.rpartition(
                        "/effort=")
                    runtime_id = runtime_id or parsed_runtime
                    if sep:
                        model = model or (parsed_model if effort_sep else model_effort)
                    effort = effort or (parsed_effort if effort_sep else "")
            runtime_id = runtime_id or row["run_backend_id"] or ""
            if model == "(CLI 默认)":
                model = ""

            if len(content) == 4000 and events:
                candidates = []
                for kind in ("text", "stdout"):
                    last = next((i for i in range(len(events) - 1, -1, -1)
                                 if events[i]["kind"] == kind), None)
                    if last is None:
                        continue
                    first = last
                    while first > 0 and events[first - 1]["kind"] == kind:
                        first -= 1
                    candidate = "".join(
                        events[i]["content"] for i in range(first, last + 1)).strip()
                    if len(candidate) > len(content) and candidate.endswith(content):
                        candidates.append(candidate)
                if candidates:
                    content = max(candidates, key=len)

            before = (row["runtime_id"], row["model"], row["effort"], row["content"])
            after = (runtime_id, model, effort, content)
            if after != before:
                self._conn.execute(
                    "UPDATE messages SET runtime_id=?, model=?, effort=?, content=? "
                    "WHERE id=?", (*after, row["id"]))
                changed += 1
        self._conn.commit()
        return changed

    def _migrate_runtime_usage(self) -> None:
        """为已有使用历史补齐项目与角色归属列。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(runtime_usage)").fetchall()}
        for name in ("project_id", "role_id"):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE runtime_usage ADD COLUMN {name} TEXT DEFAULT ''")

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
        self._execute(
            "DELETE FROM chat_sessions WHERE role_id=? AND channel IN ("
            "SELECT id FROM channels WHERE json_extract(data, '$.project_id')=?"
            ")", (id, project_id))
        self._delete("roles", f"{project_id}:{id}")
    def delete_backend(self, id: str) -> None:
        self._execute("DELETE FROM chat_sessions WHERE backend_id=?", (id,))
        self._delete("backends", id)
    def delete_channel(self, id: str) -> None:
        self._execute("DELETE FROM chat_sessions WHERE channel=?", (id,))
        self._delete("channels", id)

    def delete_project(self, id: str) -> None:
        """删除项目并级联其角色、频道与面板(消息记录保留,便于审计追溯)。"""
        for r in self.list_roles(id):
            self.delete_role(id, r.id)
        for c in self.list_channels(id):
            self.delete_channel(c.id)
        for board in self.list_boards(id):
            self.delete_board(board.id)
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

    # ---- 系统全局 Runtime 使用历史 ----
    def start_runtime_usage(self, *, backend_id: str, adapter: str,
                            mode: str, transport: str, task_id: str = "",
                            stage_name: str = "", project_id: str = "",
                            role_id: str = "", session_key: str = "",
                            model: str = "", effort: str = "",
                            workdir: str = "") -> int:
        """在 provider 启动前落库，使执行中的调用也能出现在历史列表。"""
        return self._execute(
            "INSERT INTO runtime_usage(backend_id,adapter,mode,transport,task_id,"
            "stage_name,project_id,role_id,session_key,model,effort,workdir,status,"
            "owner_pid,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'running',?,?)",
            (backend_id, adapter, mode, transport, task_id, stage_name,
             project_id, role_id, session_key, model, effort, workdir,
             os.getpid(), time.time()),
        )

    def finish_runtime_usage(self, usage_id: int, success: bool,
                             summary: str = "") -> None:
        status = "succeeded" if success else "failed"
        self._execute(
            "UPDATE runtime_usage SET status=?, success=?, summary=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (status, int(success), str(summary or "")[:1000], time.time(), usage_id),
        )

    def reconcile_runtime_usage(self) -> int:
        """把所属进程已经消失的未完成调用标为中断，保留重启前记录。"""
        rows = self._query(
            "SELECT DISTINCT owner_pid FROM runtime_usage WHERE status='running'")
        stale = []
        for row in rows:
            pid = int(row["owner_pid"] or 0)
            try:
                if pid <= 0:
                    raise ProcessLookupError
                os.kill(pid, 0)
            except ProcessLookupError:
                stale.append(pid)
            except PermissionError:
                pass
        if not stale:
            return 0
        changed = 0
        for pid in stale:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE runtime_usage SET status='interrupted', success=0, "
                    "finished_at=? WHERE status='running' AND owner_pid=?",
                    (time.time(), pid),
                )
                self._conn.commit()
                changed += cursor.rowcount
        return changed

    def list_runtime_usage(self, limit: int = 100,
                           backend_id: str = "") -> list[dict]:
        self.reconcile_runtime_usage()
        limit = max(1, min(int(limit), 500))
        fields = (
            "id,backend_id,adapter,mode,transport,task_id,stage_name,session_key,"
            "project_id,role_id,model,effort,workdir,status,success,summary,"
            "started_at,finished_at"
        )
        if backend_id:
            rows = self._query(
                f"SELECT {fields} FROM runtime_usage WHERE backend_id=? "
                "ORDER BY id DESC LIMIT ?", (backend_id, limit))
        else:
            rows = self._query(
                f"SELECT {fields} FROM runtime_usage ORDER BY id DESC LIMIT ?",
                (limit,))
        now = time.time()
        result = []
        for row in rows:
            item = dict(row)
            if item["success"] is not None:
                item["success"] = bool(item["success"])
            ended = item["finished_at"] or now
            item["duration_seconds"] = max(0.0, ended - item["started_at"])
            result.append(item)
        return result

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
    def put_channel(self, c: Channel) -> None:
        # general 是每个项目稳定的入口，不允许因旧数据或内部调用进入归档态。
        if c.is_general:
            c.archived = False
            c.archived_at = 0.0
        self._put("channels", c.id, c.to_dict())

    def get_channel(self, id: str) -> Optional[Channel]:
        d = self._get("channels", id)
        return Channel.from_dict(d) if d else None

    def list_channels(self, project_id: Optional[str] = None,
                      include_archived: bool = True) -> list[Channel]:
        cs = [Channel.from_dict(d) for d in self._list("channels")]
        if project_id is not None:
            cs = [c for c in cs if c.project_id == project_id]
        latest = {
            row["channel"]: float(row["last_message_at"] or 0)
            for row in self._query(
                "SELECT channel, MAX(created_at) AS last_message_at "
                "FROM messages GROUP BY channel")
        }
        for channel in cs:
            channel.last_message_at = latest.get(channel.id, 0.0)
        if not include_archived:
            cs = [channel for channel in cs if not channel.archived]
        # 每个项目的 general 永远第一；其余频道按最近消息排序，空频道用
        # 创建时间作为活动时间，最后以 id 保证结果稳定。
        return sorted(cs, key=lambda channel: (
            channel.project_id or "",
            0 if channel.is_general else 1,
            -(channel.last_message_at or channel.created_at),
            channel.id,
        ))

    def put_role(self, r: Role) -> None:
        self._put("roles", f"{r.project_id}:{r.id}", r.to_dict())
    def get_role(self, project_id: str, id: str) -> Optional[Role]:
        d = self._get("roles", f"{project_id}:{id}")
        return Role.from_dict(d) if d else None
    def list_roles(self, project_id: Optional[str] = None) -> list[Role]:
        rs = [Role.from_dict(d) for d in self._list("roles")]
        if project_id is not None:
            rs = [r for r in rs if r.project_id == project_id]
        # 手工排序优先,同序号(含旧数据的默认 0)按 id 字母序稳定兜底
        return sorted(rs, key=lambda r: (r.project_id, r.sort_order, r.id))

    # ---- 全局角色模板:仅供新项目复制,不与已有项目角色联动 ----
    def put_role_template(self, role: Role) -> None:
        data = role.to_dict()
        data["project_id"] = ""
        self._put("role_templates", role.id, data)

    def get_role_template(self, id: str) -> Optional[Role]:
        d = self._get("role_templates", id)
        return Role.from_dict(d) if d else None

    def list_role_templates(self) -> list[Role]:
        roles = [Role.from_dict(d) for d in self._list("role_templates")]
        return sorted(roles, key=lambda role: (role.sort_order, role.id))

    def delete_role_template(self, id: str) -> None:
        self._delete("role_templates", id)

    # ---- 自定义面板 ----
    def put_board(self, board: Board) -> None:
        board.updated_at = time.time()
        self._put("boards", board.id, board.to_dict())

    def get_board(self, id: str) -> Optional[Board]:
        d = self._get("boards", id)
        return Board.from_dict(d) if d else None

    def list_boards(self, project_id: Optional[str] = None) -> list[Board]:
        boards = [Board.from_dict(d) for d in self._list("boards")]
        if project_id is not None:
            boards = [b for b in boards if b.project_id == project_id]
        return sorted(boards, key=lambda b: (b.project_id, b.created_at))

    def delete_board(self, id: str) -> None:
        self._delete("boards", id)

    # ---- 聊天:消息 ----
    def add_message(self, channel: str, author: str, author_type: str, content: str,
                    mentions: list[str], reply_to: Optional[int] = None,
                    root_id: Optional[int] = None, depth: int = 0,
                    runtime_id: str = "", model: str = "", effort: str = "",
                    kind: str = "message",
                    mention_spans: Optional[list[dict]] = None) -> int:
        msg_id = self._execute(
            "INSERT INTO messages(channel, author, author_type, content, mentions, "
            "mention_spans, runtime_id, model, effort, kind, reply_to, root_id, depth, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (channel, author, author_type, content,
             json.dumps(mentions, ensure_ascii=False),
             json.dumps(mention_spans or [], ensure_ascii=False),
             runtime_id, model, effort, kind, reply_to, root_id, depth, time.time()),
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

    def all_messages(self, channel: str) -> list[dict]:
        """返回频道完整消息历史，不受分页接口的条数上限影响。"""
        return [dict(r) for r in self._query(
            "SELECT * FROM messages WHERE channel=? ORDER BY id", (channel,))]

    def recent_messages(self, channel: str, limit: int = 20,
                        after_id: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT * FROM messages WHERE channel=? AND id>? "
            "ORDER BY id DESC LIMIT ?", (channel, after_id, limit))
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

    def resume_chat_run_after_interaction(self, run_id: int,
                                          backend_id: str = "") -> None:
        """只恢复仍在等待用户的运行，不能覆盖并发写入的终态。"""
        self._execute(
            "UPDATE chat_runs SET status='running', backend_id=? "
            "WHERE id=? AND status='waiting_user'",
            (backend_id, run_id),
        )

    def wait_chat_run_for_interaction(self, run_id: int,
                                      backend_id: str = "") -> bool:
        """把活动运行切到等待态；已经结束的运行不能被重新打开。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE chat_runs SET status='waiting_user', backend_id=? "
                "WHERE id=? AND status NOT IN ('done','failed')",
                (backend_id, run_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def active_chat_runs(self, channel: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM chat_runs WHERE channel=? "
            "AND status IN ('queued','running','waiting_user') "
            "ORDER BY id", (channel,))]

    def count_chain_runs(self, root_id: int) -> int:
        """一条协作链(同一 root 消息)累计触发的执行数,用于防爆炸。"""
        rows = self._query("SELECT COUNT(*) AS n FROM chat_runs WHERE root_id=?", (root_id,))
        return rows[0]["n"]

    # ---- 聊天:Runtime 持久会话 ----
    def get_chat_session(self, session_key: str) -> Optional[dict]:
        rows = self._query(
            "SELECT * FROM chat_sessions WHERE session_key=?", (session_key,))
        return dict(rows[0]) if rows else None

    def put_chat_session(self, session_key: str, channel: str, role_id: str,
                         backend_id: str, adapter: str, workdir: str,
                         runtime_session_id: str, context_version: str) -> None:
        now = time.time()
        self._execute(
            "INSERT INTO chat_sessions(session_key,channel,role_id,backend_id,adapter,"
            "workdir,runtime_session_id,context_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
            "channel=excluded.channel,role_id=excluded.role_id,"
            "backend_id=excluded.backend_id,adapter=excluded.adapter,"
            "workdir=excluded.workdir,runtime_session_id=excluded.runtime_session_id,"
            "context_version=excluded.context_version,updated_at=excluded.updated_at",
            (session_key, channel, role_id, backend_id, adapter, workdir,
             runtime_session_id, context_version, now, now),
        )

    def delete_chat_session(self, session_key: str) -> None:
        self._execute("DELETE FROM chat_sessions WHERE session_key=?", (session_key,))

    def chat_sessions_for_channel(self, channel: str) -> list[dict]:
        return [dict(row) for row in self._query(
            "SELECT * FROM chat_sessions WHERE channel=? ORDER BY session_key",
            (channel,))]

    def clear_chat_sessions(self, channel: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM chat_sessions WHERE channel=?", (channel,))
            self._conn.commit()
            return cursor.rowcount

    # ---- 聊天:执行过程事件(实时运行输出) ----
    # 同类连续事件合并进同一行(追加文本),避免逐 chunk/逐行插入把表撑爆;
    # 换 kind 或单行超过上限时另起新行。前端按"行"整体重渲染,无需增量游标。
    RUN_EVENT_MAX = 8000

    def append_run_event(self, run_id: int, kind: str, text: str) -> Optional[int]:
        if not text:
            return None
        # JSON 事件必须保持一行一个对象；相邻权限/用量事件不能字符串拼接。
        if kind in {"permission_request", "user_input_request", "usage",
                    "backend_agent"}:
            return self._execute(
                "INSERT INTO run_events(run_id, kind, content, created_at) "
                "VALUES(?,?,?,?)", (run_id, kind, text, time.time()))
        event_id: Optional[int] = None
        with self._lock:
            remaining = text
            while remaining:
                rows = self._query(
                    "SELECT id, kind, LENGTH(content) AS n FROM run_events "
                    "WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,))
                last = rows[0] if rows else None
                if last and last["kind"] == kind and last["n"] < self.RUN_EVENT_MAX:
                    capacity = self.RUN_EVENT_MAX - last["n"]
                    chunk, remaining = remaining[:capacity], remaining[capacity:]
                    self._execute(
                        "UPDATE run_events SET content = content || ? WHERE id=?",
                        (chunk, last["id"]))
                    event_id = int(last["id"])
                else:
                    chunk, remaining = (remaining[: self.RUN_EVENT_MAX],
                                        remaining[self.RUN_EVENT_MAX:])
                    event_id = self._execute(
                        "INSERT INTO run_events(run_id, kind, content, created_at) "
                        "VALUES(?,?,?,?)",
                        (run_id, kind, chunk, time.time()))
        return event_id

    def append_interaction_event(self, run_id: int, kind: str,
                                 payload: dict) -> int:
        """交互请求必须独占一行，不能与相邻 JSON 事件拼接。"""
        return self._execute(
            "INSERT INTO run_events(run_id, kind, content, created_at) "
            "VALUES(?,?,?,?)",
            (run_id, kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def update_interaction_event(self, event_id: int, run_id: int,
                                 payload: dict) -> bool:
        """只更新指定 run 的交互行，避免跨运行猜测 request id。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE run_events SET content=? WHERE id=? AND run_id=?",
                (json.dumps(payload, ensure_ascii=False), event_id, run_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def run_events(self, run_id: int, limit: int = 200) -> list[dict]:
        rows = self._query(
            "SELECT id, kind, content, created_at FROM run_events "
            "WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit))
        return [dict(r) for r in reversed(rows)]

    def remove_duplicate_reply_output(self, run_id: int, reply: str) -> int:
        """删除与最终 Agent 回复完全相同的 text/stdout 事件，避免聊天流重复。"""
        expected = reply.strip()
        if not expected:
            return 0
        removed = 0
        with self._lock:
            rows = self._query(
                "SELECT kind, content FROM run_events WHERE run_id=? ORDER BY id",
                (run_id,))
            for kind in ("text", "stdout"):
                output_rows = [row for row in rows if row["kind"] == kind]
                if (output_rows
                        and "".join(row["content"] for row in output_rows).strip()
                        == expected):
                    self._execute(
                        "DELETE FROM run_events WHERE run_id=? AND kind=?",
                        (run_id, kind))
                    removed += len(output_rows)
        return removed

    def chat_runs_for_channel(self, channel: str, limit: int = 30) -> list[dict]:
        """频道最近的执行记录(含已结束)。

        events_size 是事件内容总长度:合并式追加不改行数,前端用它判断
        过程输出是否有增量、要不要重新拉取事件。
        """
        rows = self._query(
            "SELECT r.*, (SELECT COALESCE(SUM(LENGTH(e.content)), 0) "
            "FROM run_events e WHERE e.run_id = r.id) AS events_size "
            "FROM chat_runs r WHERE r.channel=? ORDER BY r.id DESC LIMIT ?",
            (channel, limit))
        return [dict(r) for r in reversed(rows)]
