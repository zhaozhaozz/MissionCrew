"""SQLite 持久化层：领域对象用 JSON，消息、简报和审计用查询友好的记录表。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .models import (GITHUB_ENVIRONMENT, GITHUB_TOKEN_VARIABLE, LEGACY_GITHUB_TOKEN,
                     LEGACY_VALUE_VARIABLE, Automation, Backend, Board, BoardDataSource,
                     Channel, Credential, Project, Role, Task, WebhookSource,
                     credential_id, new_id, normalize_labels, with_status)
from .redaction import redact_text, redaction_pairs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backends  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks     (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, author TEXT NOT NULL, author_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_briefs_task_created
  ON task_briefs(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  task_id TEXT DEFAULT '', detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles    (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS role_templates (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS role_usage_blocks (
  project_id TEXT NOT NULL, role_id TEXT NOT NULL, backend_id TEXT NOT NULL,
  window_keys TEXT NOT NULL DEFAULT '[]', disabled_until REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(project_id, role_id)
);
CREATE TABLE IF NOT EXISTS boards   (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS board_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL, author TEXT NOT NULL, author_type TEXT NOT NULL,
  content TEXT NOT NULL, mentions TEXT DEFAULT '[]',
  mention_spans TEXT DEFAULT '[]',
  context TEXT DEFAULT '{}',
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
  model TEXT DEFAULT '', effort TEXT DEFAULT '',
  trigger_message_id INTEGER NOT NULL, root_id INTEGER NOT NULL,
  depth INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
  error TEXT DEFAULT '', created_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_chat_runs_finished ON chat_runs(finished_at);
CREATE INDEX IF NOT EXISTS idx_chat_runs_chain ON chat_runs(root_id, channel);
-- 角色登记的一次性回调:到点(fire_at)或所盯的频道/Task 有更新时,在登记的
-- 频道(channel)以平台消息重新启动该角色。watch_kind 为空只看时间;只盯更新、
-- 没给时间的回调到 expires_at 仍无更新就过期。baseline 是登记时的对照基线:
-- 频道是最后一条消息 id,Task 是内容指纹。
CREATE TABLE IF NOT EXISTS chat_callbacks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL, channel TEXT NOT NULL, role_id TEXT NOT NULL,
  note TEXT NOT NULL, fire_at REAL NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL DEFAULT 0,
  watch_kind TEXT NOT NULL DEFAULT '', watch_target TEXT NOT NULL DEFAULT '',
  baseline TEXT NOT NULL DEFAULT '',
  -- 盯 webhook 来源事件(watch_kind=event)的回调:spec 是匹配条件 JSON(事件名、动作、
  -- 仓库、过滤脚本),progress 是检查进度 JSON(看过几条、脚本出错几次及最近的原因)
  spec TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
  origin_run_id INTEGER NOT NULL DEFAULT 0, origin_trigger INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
  message_id INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_chat_callbacks_status ON chat_callbacks(status, channel);
CREATE TABLE IF NOT EXISTS chat_sessions (
  session_key TEXT PRIMARY KEY,
  channel TEXT NOT NULL, role_id TEXT NOT NULL,
  backend_id TEXT NOT NULL, adapter TEXT NOT NULL,
  workdir TEXT NOT NULL, runtime_session_id TEXT DEFAULT '',
  context_version TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
  needs_reinject INTEGER DEFAULT 0,
  lean_turns INTEGER DEFAULT 0, lean_bytes INTEGER DEFAULT 0,
  resource_state TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_tokens (
  token_hash TEXT PRIMARY KEY,
  token_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  role_id TEXT NOT NULL,
  run_id INTEGER NOT NULL DEFAULT 0,
  scopes TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_used_at REAL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_identity
  ON agent_tokens(project_id, channel, role_id, created_at DESC);
CREATE TABLE IF NOT EXISTS automations (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  automation_id TEXT NOT NULL, project_id TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'manual', trigger_detail TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  exit_code INTEGER, stdout TEXT DEFAULT '', stderr TEXT DEFAULT '',
  error TEXT DEFAULT '', started_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_automation
  ON automation_runs(automation_id, id DESC);
CREATE TABLE IF NOT EXISTS webhook_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
-- 轮询来源的进度(各事件流的游标、已见事件 id、最近一次轮询结果);与来源定义分开存,
-- 每次轮询都写,不改动来源本身的 updated_at
CREATE TABLE IF NOT EXISTS webhook_source_state (id TEXT PRIMARY KEY, data TEXT NOT NULL);
-- 来源收到的每一次投递(外部 webhook 与轮询生成的都记):摘要列供列表与过滤,
-- data 是事件全文 JSON(过大时不存正文),outcomes 是交给了哪些脚本及结果
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL, received_at REAL NOT NULL,
  provider TEXT DEFAULT '', event TEXT DEFAULT '', action TEXT DEFAULT '',
  repository TEXT DEFAULT '', delivery TEXT DEFAULT '',
  transport TEXT DEFAULT 'webhook', auth TEXT DEFAULT '',
  outcomes TEXT NOT NULL DEFAULT '[]', size INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_source
  ON webhook_deliveries(source_id, id DESC);
-- 凭据(环境):data 是不含保密值的元数据 JSON,保密值单独一列("变量名 -> 值"的
-- JSON,只含 secret 变量),通用的 _get/_list 只读 data 列
CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, value TEXT NOT NULL DEFAULT ''
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


def _environment_secrets(raw) -> dict[str, str]:
    """credentials.value 列里环境的保密值:JSON 对象,损坏或为空时当作没有。"""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return ({str(key): str(value) for key, value in data.items() if value}
            if isinstance(data, dict) else {})


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
        # 旧阶段式受控资源表从未接入执行链,已由 credentials 取代
        self._conn.execute("DROP TABLE IF EXISTS resources")
        # 脱敏用的 (原文, 占位符) 缓存;凭据经本实例写入时作废重建
        self._redaction: Optional[list[tuple[str, str]]] = None
        self._migrate_backend_commands()
        self._migrate_chat_runs()
        self._migrate_automation_runs()
        self._migrate_automation_webhooks()
        self._migrate_chat_messages()
        self._migrate_runtime_usage()
        self._migrate_chat_sessions()
        self._migrate_agent_tokens()
        self._migrate_chat_callbacks()
        self._migrate_source_cards_to_tasks()
        self._migrate_tasks_to_issues()
        self._migrate_board_status_filters()
        self._migrate_credentials_to_environments()
        self._conn.commit()

    def _migrate_chat_runs(self) -> None:
        """旧数据库补齐 chat_runs 的执行组合列(运行卡片展示模型与推理力度)。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(chat_runs)").fetchall()}
        for name in ("model", "effort"):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE chat_runs ADD COLUMN {name} TEXT DEFAULT ''")

    def _migrate_automation_runs(self) -> None:
        """旧数据库补齐 automation_runs.trigger_detail(webhook 事件名与投递 id)。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(automation_runs)").fetchall()}
        if "trigger_detail" not in columns:
            self._conn.execute(
                "ALTER TABLE automation_runs ADD COLUMN trigger_detail TEXT DEFAULT ''")

    def _migrate_automation_webhooks(self) -> int:
        """旧版每个脚本自带 webhook 开关与密钥,迁成全局 webhook 来源并让脚本订阅它。

        迁出的来源沿用旧投递路径 `<项目>/<脚本短 id>` 与原密钥,外部系统的配置不用改;
        已关闭 webhook 的脚本只丢弃保留的密钥。返回改写的脚本数。
        """
        rows = self._conn.execute("SELECT id, data FROM automations").fetchall()
        taken = {row["id"] for row in self._conn.execute(
            "SELECT id FROM webhook_sources").fetchall()}
        changed = 0
        for row in rows:
            data = json.loads(row["data"])
            if "webhook" not in data and "webhook_secret" not in data:
                continue
            enabled = bool(data.pop("webhook", False))
            secret = str(data.pop("webhook_secret", "") or "")
            project_id = str(data.get("project_id", ""))
            short = str(data.get("id", "")).removeprefix(f"{project_id}:")
            if enabled and secret and not data.get("webhook_source"):
                source_id, n = short, 1
                while source_id in taken:   # 不同项目的同名脚本:加项目前缀与序号区分
                    n += 1
                    source_id = f"{project_id}-{short}" + (f"-{n}" if n > 2 else "")
                taken.add(source_id)
                source = WebhookSource(
                    id=source_id, name=str(data.get("name") or short),
                    description=f"由自动化脚本 {data.get('id')} 的 webhook 迁移",
                    path=f"{project_id}/{short}", secret=secret)
                self._conn.execute(
                    "INSERT INTO webhook_sources(id, data) VALUES(?, ?)",
                    (source_id, json.dumps(source.to_dict(), ensure_ascii=False)))
                data["webhook_source"] = source_id
            self._conn.execute("UPDATE automations SET data=? WHERE id=?",
                               (json.dumps(data, ensure_ascii=False), row["id"]))
            changed += 1
        return changed

    def _migrate_credentials_to_environments(self) -> int:
        """旧版一份凭据是单个保密值,现在一律是环境(一组变量):把旧行改成只有一个
        secret 变量的环境。返回改写的行数,已经迁过的库返回 0。

        - 普通凭据 `NAME` 变成环境 `NAME`,值放进变量 `VALUE`,名字、作用域、用途、
          待填写状态都不变,所以"项目同名覆盖全局"的结果和迁移前一样。
        - `GITHUB_TOKEN` 是 GitHub 身份,变成环境 `GITHUB` 的变量 `TOKEN`。同一作用域
          已经有人建了 `GITHUB` 环境时把 TOKEN 并进去;那个环境自己已有 TOKEN 时保留
          它,旧凭据按普通规则迁成 `GITHUB_TOKEN` / `VALUE`,不丢值。
        - 上一版已经是环境的行只去掉不再使用的字段。
        """
        rows = self._conn.execute("SELECT id, data, value FROM credentials").fetchall()
        environments: dict[str, tuple[dict, dict]] = {}     # id -> (元数据, secret 值)
        legacy: list[tuple[str, dict, str]] = []
        dirty: set[str] = set()
        for row in rows:
            data, value = json.loads(row["data"]), str(row["value"] or "")
            if "kind" not in data and "variables" in data:
                environments[row["id"]] = (data, _environment_secrets(value))
            elif data.get("kind") == "environment":
                data.pop("kind", None)
                data.pop("fingerprint", None)
                environments[row["id"]] = (data, _environment_secrets(value))
                dirty.add(row["id"])
            else:
                legacy.append((row["id"], data, value))
        if not legacy and not dirty:
            return 0
        removed: set[str] = set()
        for old_id, data, value in legacy:
            project_id, name = str(data.get("project_id") or ""), str(data["name"])
            environment, key = ((GITHUB_ENVIRONMENT, GITHUB_TOKEN_VARIABLE)
                                if name == LEGACY_GITHUB_TOKEN else (name, LEGACY_VALUE_VARIABLE))
            target = environments.get(credential_id(project_id, environment))
            if target is not None and any(item["name"] == key
                                          for item in target[0]["variables"]):
                environment, key, target = name, LEGACY_VALUE_VARIABLE, None
            variable = {"name": key, "secret": True, "has_value": bool(value),
                        "fingerprint": str(data.get("fingerprint") or "") if value else "",
                        "value": ""}
            if target is None:
                target = ({"name": environment, "project_id": project_id,
                           "description": str(data.get("description") or ""),
                           "has_value": True, "requested_by": "",
                           "created_at": data.get("created_at") or time.time(),
                           "updated_at": data.get("updated_at") or time.time(),
                           "variables": []}, {})
                environments[credential_id(project_id, environment)] = target
            meta, secrets = target
            meta["variables"].append(variable)
            meta["has_value"] = all(item["has_value"] for item in meta["variables"])
            if not value:
                meta["requested_by"] = (meta.get("requested_by")
                                        or str(data.get("requested_by") or ""))
            else:
                secrets[key] = value
            removed.add(old_id)
            dirty.add(credential_id(project_id, environment))
        for old_id in removed - dirty:
            self._conn.execute("DELETE FROM credentials WHERE id=?", (old_id,))
        for id in dirty:
            meta, secrets = environments[id]
            self._conn.execute(
                "INSERT INTO credentials(id, data, value) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, value=excluded.value",
                (id, json.dumps(meta, ensure_ascii=False),
                 json.dumps(secrets, ensure_ascii=False) if secrets else ""))
        return len(dirty)

    def _migrate_backend_commands(self) -> int:
        """删除旧 Backend 记录中的命令覆盖，统一回到内置 Runtime 启动方式。"""
        changed = 0
        for row in self._conn.execute("SELECT id, data FROM backends").fetchall():
            try:
                raw = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or "command" not in raw:
                continue
            raw.pop("command")
            self._conn.execute(
                "UPDATE backends SET data=? WHERE id=?",
                (json.dumps(raw, ensure_ascii=False), row["id"]),
            )
            changed += 1
        return changed

    def _migrate_tasks_to_issues(self) -> int:
        """任务行原地升级(旧枚举 status 转状态标签),并清理失效频道绑定。"""
        channel_rows = self._conn.execute("SELECT data FROM channels").fetchall()
        channels_by_project: dict[str, list[Channel]] = {}
        for row in channel_rows:
            try:
                channel = Channel.from_dict(json.loads(row["data"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not channel.archived:
                channels_by_project.setdefault(channel.project_id, []).append(channel)

        changed = 0
        for row in self._conn.execute("SELECT id, data FROM tasks").fetchall():
            try:
                raw = json.loads(row["data"])
                task = Task.from_dict(raw)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            # 频道绑定已可选:只剔除失效引用,不再强制补默认频道
            valid_ids = {channel.id for channel in
                         channels_by_project.get(task.project_id, [])}
            task.channel_ids = [cid for cid in task.channel_ids if cid in valid_ids]
            normalized = task.to_dict()
            if normalized != raw:
                self._conn.execute(
                    "UPDATE tasks SET data=? WHERE id=?",
                    (json.dumps(normalized, ensure_ascii=False), row["id"]),
                )
                changed += 1
        return changed

    def _migrate_source_cards_to_tasks(self) -> int:
        """把旧版数据源内嵌 cards 一次性迁入统一 tasks 表。

        卡片 id 作为 external_id,状态列 key 映射成状态标签文本;已存在同
        (project, source, external_id) 的任务则跳过,保证迁移可重入。
        """
        existing: set[tuple[str, str, str]] = set()
        for row in self._conn.execute("SELECT data FROM tasks").fetchall():
            try:
                raw = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("external_id"):
                existing.add((str(raw.get("project_id", "")),
                              str(raw.get("source_id", "")),
                              str(raw.get("external_id", ""))))
        changed = 0
        for row in self._conn.execute(
                "SELECT id, data FROM board_sources").fetchall():
            try:
                raw = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or "cards" not in raw:
                continue
            project_id = str(raw.get("project_id", ""))
            short_id = str(raw.get("id", "")).removeprefix(f"{project_id}:")
            key_text = {
                str(col.get("key")): str(col.get("title") or col.get("key") or "")
                for col in raw.get("columns") or [] if isinstance(col, dict)}
            for card in raw.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                external_id = str(card.get("id", "")).strip()
                if (not external_id
                        or (project_id, short_id, external_id) in existing):
                    continue
                status_raw = str(card.get("status") or "")
                labels = with_status(
                    normalize_labels(card.get("labels") or []),
                    key_text.get(status_raw, status_raw))
                stamp = float(card.get("updated_at") or time.time())
                task_id = new_id("t")
                while self._conn.execute(
                        "SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                    task_id = new_id("t")
                task = Task(
                    id=task_id, project_id=project_id,
                    title=str(card.get("title") or external_id),
                    source_id=short_id, external_id=external_id,
                    summary=str(card.get("summary") or ""), labels=labels,
                    url=str(card.get("url") or ""),
                    meta=[str(x) for x in card.get("meta") or []],
                    created_at=stamp, updated_at=stamp,
                )
                self._conn.execute(
                    "INSERT INTO tasks(id, data) VALUES(?, ?)",
                    (task.id, json.dumps(task.to_dict(), ensure_ascii=False)))
                changed += 1
            normalized = BoardDataSource.from_dict(raw).to_dict()
            if normalized != raw:
                self._conn.execute(
                    "UPDATE board_sources SET data=? WHERE id=?",
                    (json.dumps(normalized, ensure_ascii=False), row["id"]))
        return changed

    def _migrate_board_status_filters(self) -> int:
        """把看板筛选列里的裸状态词一次性改写成状态标签表达式。

        旧版卡片状态兼作可筛选标签(如列表达式直接写 `待处理`);状态改为
        `status: 文本` 标签后,与数据源状态取值同名的表达式项按新语义改写,
        其余标签项(如 bug)保持不变。
        """
        from . import label_query
        from .models import BUILTIN_SOURCE_ID, BUILTIN_STATUS_VALUES

        # (project_id, source_id) -> 状态取值集合(小写)
        source_values: dict[tuple[str, str], set[str]] = {}
        for row in self._conn.execute("SELECT data FROM board_sources").fetchall():
            try:
                record = BoardDataSource.from_dict(json.loads(row["data"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            short_id = record.id.removeprefix(f"{record.project_id}:")
            source_values[(record.project_id, short_id)] = {
                str(c.get("value") or "").lower() for c in record.status_values}
        builtin_values = {str(c["value"]).lower() for c in BUILTIN_STATUS_VALUES}

        def rewrite(query: str, values: set[str]) -> str:
            try:
                tokens = label_query._tokenize(query)
            except Exception:
                return query
            parts = []
            for token in tokens:
                if isinstance(token, tuple) and token[0] == "label":
                    text = token[1]
                    if (text.lower() in values
                            and not label_query.split_label(text)[0]):
                        text = f"status: {text}"
                    parts.append(text)
                else:
                    parts.append(str(token))
            return " ".join(parts).replace("( ", "(").replace(" )", ")")

        changed = 0
        for row in self._conn.execute("SELECT id, data FROM boards").fetchall():
            try:
                raw = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or raw.get("kind") != "taskboard":
                continue
            source_id = str(raw.get("source") or "")
            if source_id in ("", "tasks", BUILTIN_SOURCE_ID):
                values = builtin_values
            else:
                values = source_values.get(
                    (str(raw.get("project_id", "")), source_id), set())
            filters = raw.get("filters") or []
            rewritten = [
                {**item, "query": rewrite(str(item.get("query") or ""), values)}
                for item in filters if isinstance(item, dict)]
            if rewritten != filters:
                raw["filters"] = rewritten
                self._conn.execute(
                    "UPDATE boards SET data=? WHERE id=?",
                    (json.dumps(raw, ensure_ascii=False), row["id"]))
                changed += 1
        return changed

    def _migrate_chat_messages(self) -> int:
        """升级聊天消息字段，并修复旧版末尾 4000 字符回复。

        旧数据库不会因 ``CREATE TABLE IF NOT EXISTS`` 自动增加列；旧适配器又把
        完整回复截成末尾 4000 字符，但完整文本仍保留在 run_events 中。迁移按
        message.reply_to + author 找到对应执行，仅在事件文本明确以旧正文结尾时
        恢复，避免猜测或误改普通的 4000 字符消息。
        """
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(messages)").fetchall()}
        for name in ("runtime_id", "model", "effort", "kind", "mention_spans", "context"):
            if name not in columns:
                default = ("'message'" if name == "kind" else
                           "'[]'" if name == "mention_spans" else
                           "'{}'" if name == "context" else "''")
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

    def _migrate_agent_tokens(self) -> int:
        """为旧令牌补充逐 Run 绑定字段与身份类型字段。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(agent_tokens)").fetchall()}
        changed = 0
        if "run_id" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_tokens ADD COLUMN run_id INTEGER NOT NULL DEFAULT 0")
            changed += 1
        if "kind" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'run'")
            changed += 1
        return changed

    def _migrate_runtime_usage(self) -> None:
        """为已有使用历史补齐项目与角色归属列。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(runtime_usage)").fetchall()}
        for name in ("project_id", "role_id"):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE runtime_usage ADD COLUMN {name} TEXT DEFAULT ''")

    def _migrate_chat_sessions(self) -> None:
        """为已有会话记录补齐公共上下文重注入的标记与计数列。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(chat_sessions)").fetchall()}
        for name in ("needs_reinject", "lean_turns", "lean_bytes"):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE chat_sessions ADD COLUMN {name} INTEGER DEFAULT 0")
        if "resource_state" not in columns:   # 会话已看到的准则/Skill 正文版本表
            self._conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN resource_state TEXT DEFAULT ''")

    def _migrate_chat_callbacks(self) -> None:
        """事件回调上线前建的库没有 spec / progress 两列。"""
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(chat_callbacks)").fetchall()}
        for column in ("spec", "progress"):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE chat_callbacks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

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
        self.revoke_agent_tokens(project_id=project_id, role_id=id)
        self.cancel_chat_callbacks("角色已删除", project_id=project_id, role_id=id)
        self.delete_role_usage_block(project_id, id)
        self._delete("roles", f"{project_id}:{id}")
    def delete_backend(self, id: str) -> None:
        self._execute("DELETE FROM chat_sessions WHERE backend_id=?", (id,))
        self._delete("backends", id)

    def _unlink_channel_from_tasks(self, channel: Channel) -> None:
        """移除 Task 对频道的引用。频道绑定可选:解绑后为空就保持为空,不改绑到
        其他频道(派发时才回退到 general)。"""
        for task in self.list_tasks():
            if task.project_id != channel.project_id \
                    or channel.id not in task.channel_ids:
                continue
            task.channel_ids = [
                item for item in task.channel_ids if item != channel.id]
            self.put_task(task)

    def delete_channel(self, id: str) -> None:
        channel = self.get_channel(id)
        if channel is not None:
            self._unlink_channel_from_tasks(channel)
        self._execute("DELETE FROM chat_sessions WHERE channel=?", (id,))
        self.revoke_agent_tokens(channel=id)
        self.cancel_chat_callbacks("频道已删除", channel=id)
        self._delete("channels", id)

    def purge_channel_conversation(self, id: str) -> dict[str, int]:
        """永久删除频道及其会话数据，供内容页“重新开始”语义使用。

        普通频道删除仍通过回收站保留消息；只有显式要求永久清空的内容频道
        才调用本方法。所有会话表在同一事务中删除，避免留下半套记录。
        """
        channel = self.get_channel(id)
        if channel is None:
            return {
                "messages": 0, "runs": 0, "events": 0, "sessions": 0,
                "tokens": 0, "runtime_usage": 0, "channels": 0,
            }
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                active = self._conn.execute(
                    "SELECT 1 FROM chat_runs WHERE channel=? "
                    "AND status IN ('queued','running','waiting_user') LIMIT 1",
                    (id,),
                ).fetchone()
                if active:
                    raise ValueError(
                        "频道仍有 Agent 正在运行，请先停止或等待本轮结束")

                counts: dict[str, int] = {}
                cursor = self._conn.execute(
                    "DELETE FROM run_events WHERE run_id IN "
                    "(SELECT id FROM chat_runs WHERE channel=?)", (id,))
                counts["events"] = cursor.rowcount
                cursor = self._conn.execute(
                    "DELETE FROM chat_runs WHERE channel=?", (id,))
                counts["runs"] = cursor.rowcount
                cursor = self._conn.execute(
                    "DELETE FROM messages WHERE channel=?", (id,))
                counts["messages"] = cursor.rowcount
                cursor = self._conn.execute(
                    "DELETE FROM chat_sessions WHERE channel=?", (id,))
                counts["sessions"] = cursor.rowcount
                cursor = self._conn.execute(
                    "DELETE FROM agent_tokens WHERE channel=?", (id,))
                counts["tokens"] = cursor.rowcount
                # session_key 以 "<channel>::<role>" 开头；使用 substr 避免
                # LIKE 把旧数据中的通配字符解释成模式。
                prefix = f"{id}::"
                cursor = self._conn.execute(
                    "DELETE FROM runtime_usage "
                    "WHERE substr(session_key, 1, ?)=?",
                    (len(prefix), prefix),
                )
                counts["runtime_usage"] = cursor.rowcount
                cursor = self._conn.execute(
                    "DELETE FROM channels WHERE id=?", (id,))
                counts["channels"] = cursor.rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        self._unlink_channel_from_tasks(channel)
        return counts

    def delete_project(self, id: str) -> None:
        """删除项目并级联其角色、频道与面板(消息记录保留,便于审计追溯)。"""
        for r in self.list_roles(id):
            self.delete_role(id, r.id)
        for c in self.list_channels(id):
            self.delete_channel(c.id)
        for board in self.list_boards(id):
            self.delete_board(board.id)
        for source in self.list_board_datasources(id):
            self.delete_board_datasource(source.id)
        for task in self.list_tasks():
            if task.project_id == id:
                self.delete_task(task.id)
        for automation in self.list_automations(id):
            self.delete_automation(automation.id)
        for credential in self.list_credentials(id):
            self.delete_credential(credential.id)
        self.revoke_agent_tokens(project_id=id)
        self.cancel_chat_callbacks("项目已删除", project_id=id)
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


    def put_task(self, t: Task) -> None:
        t.updated_at = time.time()
        self._put("tasks", t.id, t.to_dict())

    def _task_activity(self, task: Task) -> Task:
        """兼容旧数据：状态未变化的简报也应算作 Task 最近活动。"""
        rows = self._query(
            "SELECT MAX(created_at) AS latest FROM task_briefs WHERE task_id=?",
            (task.id,),
        )
        latest = float(rows[0]["latest"] or 0)
        task.updated_at = max(task.updated_at, latest)
        return task

    def get_task(self, id: str) -> Optional[Task]:
        d = self._get("tasks", id)
        return self._task_activity(Task.from_dict(d)) if d else None

    def list_tasks(self, project_id: Optional[str] = None,
                   include_archived: bool = True,
                   source_id: Optional[str] = None) -> list[Task]:
        ts = [Task.from_dict(d) for d in self._list("tasks")]
        latest_briefs = {
            row["task_id"]: float(row["latest"] or 0)
            for row in self._query(
                "SELECT task_id, MAX(created_at) AS latest "
                "FROM task_briefs GROUP BY task_id")
        }
        for task in ts:
            task.updated_at = max(
                task.updated_at, latest_briefs.get(task.id, 0))
        if project_id is not None:
            ts = [task for task in ts if task.project_id == project_id]
        if source_id is not None:
            ts = [task for task in ts if task.source_id == source_id]
        if not include_archived:
            ts = [task for task in ts if not task.archived]
        return sorted(
            ts, key=lambda task: (
                -task.updated_at, -task.created_at, task.id))

    def delete_task(self, id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM task_briefs WHERE task_id=?", (id,))
            self._conn.execute("DELETE FROM tasks WHERE id=?", (id,))

    def restore_task(self, task: Task, briefs: list[dict]) -> None:
        """原子恢复 Task 与状态简报；简报保留原作者、状态、正文和时间。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks(id,data) VALUES(?,?)",
                (task.id, json.dumps(task.to_dict(), ensure_ascii=False)),
            )
            for brief in reversed(briefs):
                self._conn.execute(
                    "INSERT INTO task_briefs"
                    "(task_id,author,author_type,status,content,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        task.id, str(brief.get("author", "")),
                        str(brief.get("author_type", "")),
                        str(brief.get("status", "")),
                        str(brief.get("content", "")),
                        float(brief.get("created_at", time.time())),
                    ),
                )

    def add_task_brief(self, task_id: str, author: str, author_type: str,
                       content: str, status: str = "") -> dict:
        created_at = time.time()
        brief_id = self._execute(
            "INSERT INTO task_briefs(task_id,author,author_type,status,content,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, author, author_type, status, content, created_at),
        )
        return {
            "id": brief_id, "task_id": task_id, "author": author,
            "author_type": author_type, "status": status,
            "content": content, "created_at": created_at,
        }

    def list_task_briefs(self, task_id: str,
                         limit: Optional[int] = 200) -> list[dict]:
        if limit is None:
            rows = self._query(
                "SELECT * FROM task_briefs WHERE task_id=? ORDER BY id DESC",
                (task_id,),
            )
        else:
            rows = self._query(
                "SELECT * FROM task_briefs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, max(1, min(int(limit), 1000))),
            )
        return [dict(row) for row in rows]

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
                             summary: str = "", interrupted: bool = False) -> None:
        status = "interrupted" if interrupted else (
            "succeeded" if success else "failed")
        self._execute(
            "UPDATE runtime_usage SET status=?, success=?, summary=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (status, int(success and not interrupted),
             str(summary or "")[:1000], time.time(), usage_id),
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

    # ---- Agent Tool 身份令牌 ----
    def put_agent_token(self, *, token_hash: str, token_id: str,
                        project_id: str, channel: str, role_id: str,
                        scopes: list[str], expires_at: float,
                        run_id: int = 0, kind: str = "run") -> None:
        self._execute(
            "INSERT INTO agent_tokens(token_hash,token_id,project_id,channel,role_id,"
            "run_id,kind,scopes,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (token_hash, token_id, project_id, channel, role_id, run_id, kind,
             json.dumps(scopes, ensure_ascii=False), time.time(), expires_at),
        )

    def get_agent_token(self, token_hash: str) -> Optional[dict]:
        rows = self._query(
            "SELECT * FROM agent_tokens WHERE token_hash=?", (token_hash,))
        if not rows:
            return None
        item = dict(rows[0])
        try:
            item["scopes"] = json.loads(item.get("scopes") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["scopes"] = []
        return item

    def touch_agent_token(self, token_hash: str) -> None:
        self._execute(
            "UPDATE agent_tokens SET last_used_at=? WHERE token_hash=?",
            (time.time(), token_hash),
        )

    def revoke_agent_tokens(self, *, project_id: str = "", channel: str = "",
                            role_id: str = "", kind: str = "",
                            run_id: Optional[int] = None) -> int:
        clauses = ["revoked_at IS NULL"]
        params: list[object] = []
        for column, value in (("project_id", project_id), ("channel", channel),
                              ("role_id", role_id), ("kind", kind)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if len(clauses) == 1:
            return 0
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE agent_tokens SET revoked_at=? WHERE {' AND '.join(clauses)}",
                (time.time(), *params),
            )
            self._conn.commit()
            return cursor.rowcount

    # ---- 聊天:频道 / 角色(均按项目隔离,项目是第一层级) ----
    def put_channel(self, c: Channel) -> None:
        # general 是每个项目稳定的入口，不允许因旧数据或内部调用进入归档态。
        if c.is_general:
            c.archived = False
            c.archived_at = 0.0
        # 只存频道本身:Task 的频道绑定只由显式操作写入(创建/编辑/派发),
        # 保存任何频道都不会把未绑定的 Task(如数据源同步来的)挂到它上面
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

    # ---- 账户用量与角色启停联动 ----
    def list_role_usage_blocks(self) -> list[dict]:
        rows = self._query(
            "SELECT project_id,role_id,backend_id,window_keys,disabled_until,"
            "created_at,updated_at FROM role_usage_blocks "
            "ORDER BY project_id,role_id")
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["window_keys"] = json.loads(item["window_keys"] or "[]")
            except (TypeError, json.JSONDecodeError):
                item["window_keys"] = []
            result.append(item)
        return result

    def delete_role_usage_block(self, project_id: str, role_id: str) -> None:
        self._execute(
            "DELETE FROM role_usage_blocks WHERE project_id=? AND role_id=?",
            (project_id, role_id),
        )

    def auto_disable_role_for_usage(
            self, project_id: str, role_id: str, backend_id: str,
            window_keys: list[str], disabled_until: float) -> bool:
        """原子停用角色并记录归因；人工已停用的角色不会被自动接管。"""
        storage_id = f"{project_id}:{role_id}"
        timestamp = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT data FROM roles WHERE id=?", (storage_id,)).fetchone()
            if row is None:
                return False
            role = Role.from_dict(json.loads(row["data"]))
            existing = self._conn.execute(
                "SELECT 1 FROM role_usage_blocks WHERE project_id=? AND role_id=?",
                (project_id, role_id),
            ).fetchone()
            if not role.enabled and existing is None:
                return False
            changed = role.enabled
            role.enabled = False
            self._conn.execute(
                "UPDATE roles SET data=? WHERE id=?",
                (json.dumps(role.to_dict(), ensure_ascii=False), storage_id),
            )
            self._conn.execute(
                "INSERT INTO role_usage_blocks(project_id,role_id,backend_id,"
                "window_keys,disabled_until,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,role_id) DO UPDATE SET "
                "backend_id=excluded.backend_id,window_keys=excluded.window_keys,"
                "disabled_until=excluded.disabled_until,updated_at=excluded.updated_at",
                (project_id, role_id, backend_id,
                 json.dumps(window_keys, ensure_ascii=False), disabled_until,
                 timestamp, timestamp),
            )
            return changed

    def auto_enable_role_after_usage(self, project_id: str, role_id: str) -> bool:
        """只恢复仍带自动停用记录的角色，并原子清除该记录。"""
        storage_id = f"{project_id}:{role_id}"
        with self._lock, self._conn:
            block = self._conn.execute(
                "SELECT 1 FROM role_usage_blocks WHERE project_id=? AND role_id=?",
                (project_id, role_id),
            ).fetchone()
            if block is None:
                return False
            row = self._conn.execute(
                "SELECT data FROM roles WHERE id=?", (storage_id,)).fetchone()
            changed = False
            if row is not None:
                role = Role.from_dict(json.loads(row["data"]))
                changed = not role.enabled
                role.enabled = True
                self._conn.execute(
                    "UPDATE roles SET data=? WHERE id=?",
                    (json.dumps(role.to_dict(), ensure_ascii=False), storage_id),
                )
            self._conn.execute(
                "DELETE FROM role_usage_blocks WHERE project_id=? AND role_id=?",
                (project_id, role_id),
            )
            return changed

    def set_role_enabled_manually(
            self, project_id: str, role_id: str, enabled: bool) -> Optional[Role]:
        """人工切换优先于旧的自动归因，防止计时器随后误恢复人工停用。"""
        storage_id = f"{project_id}:{role_id}"
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT data FROM roles WHERE id=?", (storage_id,)).fetchone()
            if row is None:
                return None
            role = Role.from_dict(json.loads(row["data"]))
            role.enabled = enabled
            self._conn.execute(
                "UPDATE roles SET data=? WHERE id=?",
                (json.dumps(role.to_dict(), ensure_ascii=False), storage_id),
            )
            self._conn.execute(
                "DELETE FROM role_usage_blocks WHERE project_id=? AND role_id=?",
                (project_id, role_id),
            )
            return role

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

    # ---- 自定义看板数据源 ----
    def put_board_datasource(self, source: BoardDataSource) -> None:
        source.updated_at = time.time()
        self._put("board_sources", source.id, source.to_dict())

    def get_board_datasource(self, id: str) -> Optional[BoardDataSource]:
        d = self._get("board_sources", id)
        return BoardDataSource.from_dict(d) if d else None

    def list_board_datasources(
            self, project_id: Optional[str] = None) -> list[BoardDataSource]:
        sources = [BoardDataSource.from_dict(d)
                   for d in self._list("board_sources")]
        if project_id is not None:
            sources = [s for s in sources if s.project_id == project_id]
        return sorted(sources, key=lambda s: (s.project_id, s.created_at))

    def delete_board_datasource(self, id: str) -> None:
        self._delete("board_sources", id)

    # ---- 自动化脚本 ----
    def put_automation(self, automation: Automation) -> None:
        automation.updated_at = time.time()
        self._put("automations", automation.id, automation.to_dict())

    def get_automation(self, id: str) -> Optional[Automation]:
        d = self._get("automations", id)
        return Automation.from_dict(d) if d else None

    def list_automations(self, project_id: Optional[str] = None) -> list[Automation]:
        items = [Automation.from_dict(d) for d in self._list("automations")]
        if project_id is not None:
            items = [item for item in items if item.project_id == project_id]
        return sorted(items, key=lambda item: (item.project_id, item.created_at))

    def delete_automation(self, id: str) -> None:
        self._execute("DELETE FROM automation_runs WHERE automation_id=?", (id,))
        self._delete("automations", id)

    # ---- 全局 webhook 来源 ----
    def put_webhook_source(self, source: WebhookSource) -> None:
        source.updated_at = time.time()
        self._put("webhook_sources", source.id, source.to_dict())

    def get_webhook_source(self, id: str) -> Optional[WebhookSource]:
        d = self._get("webhook_sources", id)
        return WebhookSource.from_dict(d) if d else None

    def list_webhook_sources(self) -> list[WebhookSource]:
        items = [WebhookSource.from_dict(d) for d in self._list("webhook_sources")]
        return sorted(items, key=lambda item: (item.created_at, item.id))

    def find_webhook_source_by_path(self, path: str) -> Optional[WebhookSource]:
        rows = self._query(
            "SELECT data FROM webhook_sources WHERE json_extract(data, '$.path')=?",
            (path,))
        return WebhookSource.from_dict(json.loads(rows[0]["data"])) if rows else None

    def delete_webhook_source(self, id: str) -> None:
        self._delete("webhook_sources", id)
        self._delete("webhook_source_state", id)
        self._execute("DELETE FROM webhook_deliveries WHERE source_id=?", (id,))

    # ---- webhook 投递记录 ----
    _DELIVERY_COLUMNS = ("id, source_id, received_at, provider, event, action, repository, "
                         "delivery, transport, auth, outcomes, size")

    def add_webhook_delivery(self, source_id: str, *, received_at: float, provider: str,
                             event: str, action: str, repository: str, delivery: str,
                             transport: str, auth: str, outcomes: list,
                             data: str, size: int, keep: int) -> int:
        """记一次投递并返回记录 id。data 是事件全文 JSON,入库前同样脱敏(外部系统
        送来的正文里也可能带着已保存的凭据);keep 是每个来源保留的条数上限,每
        100 条顺手清一次更早的。"""
        new_id = self._execute(
            "INSERT INTO webhook_deliveries(source_id, received_at, provider, event, action, "
            "repository, delivery, transport, auth, outcomes, size, data) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, received_at, provider, event, action, repository, delivery,
             transport, auth, json.dumps(outcomes, ensure_ascii=False), size,
             self.redact_credentials(data)))
        if new_id % 100 == 0:
            self._execute(
                "DELETE FROM webhook_deliveries WHERE source_id=? AND id NOT IN "
                "(SELECT id FROM webhook_deliveries WHERE source_id=? "
                "ORDER BY id DESC LIMIT ?)", (source_id, source_id, keep))
        return new_id

    @staticmethod
    def _delivery_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["outcomes"] = json.loads(item.get("outcomes") or "[]")
        return item

    def list_webhook_deliveries(self, source_id: str, limit: int = 50, before_id: int = 0,
                                event: str = "", action: str = "",
                                repository: str = "", after_id: int = -1) -> list[dict]:
        """按 id 倒序列出投递摘要(不含事件正文);event/action 精确匹配,repository
        不区分大小写,before_id>0 时只取更早的,用于向前翻页。给了 after_id(>=0)
        则改为只取更新的并按 id 正序,供事件回调从游标往后逐条检查。"""
        conditions, params = ["source_id=?"], [source_id]
        if before_id > 0:
            conditions.append("id<?")
            params.append(before_id)
        if after_id >= 0:
            conditions.append("id>?")
            params.append(after_id)
        for column, value in (("event", event), ("action", action)):
            if value:
                conditions.append(f"{column}=?")
                params.append(value)
        if repository:
            conditions.append("LOWER(repository)=?")
            params.append(repository.lower())
        params.append(max(1, min(int(limit), 200)))
        rows = self._query(
            f"SELECT {self._DELIVERY_COLUMNS} FROM webhook_deliveries "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY id {'ASC' if after_id >= 0 else 'DESC'} LIMIT ?", tuple(params))
        return [self._delivery_row(row) for row in rows]

    def last_webhook_delivery_id(self, source_id: str) -> int:
        rows = self._query(
            "SELECT MAX(id) AS last FROM webhook_deliveries WHERE source_id=?", (source_id,))
        return int(rows[0]["last"] or 0)

    def get_webhook_delivery(self, delivery_id: int) -> Optional[dict]:
        """单条记录,`event_data` 是事件全文(正文过大没存时其中 payload 为 null)。"""
        rows = self._query(
            f"SELECT {self._DELIVERY_COLUMNS}, data FROM webhook_deliveries WHERE id=?",
            (delivery_id,))
        if not rows:
            return None
        item = self._delivery_row(rows[0])
        item["event_data"] = json.loads(item.pop("data") or "null")
        return item

    def count_webhook_deliveries(self, source_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM webhook_deliveries WHERE source_id=?", (source_id,))
        return int(rows[0]["n"]) if rows else 0

    def prune_webhook_deliveries(self, cutoff: float) -> None:
        self._execute("DELETE FROM webhook_deliveries WHERE received_at<?", (cutoff,))

    def get_webhook_source_state(self, id: str) -> dict:
        return self._get("webhook_source_state", id) or {}

    def put_webhook_source_state(self, id: str, state: dict) -> None:
        self._put("webhook_source_state", id, state)

    def delete_webhook_source_state(self, id: str) -> None:
        self._delete("webhook_source_state", id)

    # ---- 凭据(环境;全局 + 项目,保密值与元数据分列存放) ----
    def put_credential(self, credential: Credential,
                       secrets: Optional[dict[str, str]] = None) -> None:
        """保存元数据;secrets 不为 None 时一并整份替换保密值("secret 变量名 -> 值",
        空字典 = 清空)。"""
        credential.updated_at = time.time()
        value = None if secrets is None else (
            json.dumps(secrets, ensure_ascii=False) if secrets else "")
        with self._lock:
            if value is None:
                self._execute(
                    "INSERT INTO credentials(id, data) VALUES(?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (credential.id, json.dumps(credential.to_dict(), ensure_ascii=False)))
            else:
                self._execute(
                    "INSERT INTO credentials(id, data, value) VALUES(?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data, value=excluded.value",
                    (credential.id, json.dumps(credential.to_dict(), ensure_ascii=False),
                     value))
            self._redaction = None

    def get_credential(self, project_id: str, name: str) -> Optional[Credential]:
        d = self._get("credentials", credential_id(project_id, name))
        return Credential.from_dict(d) if d else None

    def list_credentials(self, project_id: Optional[str] = None) -> list[Credential]:
        """project_id 为 None 列出全部;空串只列全局;否则只列该项目自己的。"""
        items = [Credential.from_dict(d) for d in self._list("credentials")]
        if project_id is not None:
            items = [item for item in items if item.project_id == project_id]
        return sorted(items, key=lambda item: (item.project_id, item.name))

    def credential_secrets(self, project_id: str, name: str) -> dict[str, str]:
        """环境里 secret 变量的值(变量名 -> 值);环境不存在时返回空字典。"""
        rows = self._query("SELECT value FROM credentials WHERE id=?",
                           (credential_id(project_id, name),))
        return _environment_secrets(rows[0]["value"]) if rows else {}

    def delete_credential(self, id: str) -> None:
        with self._lock:
            self._delete("credentials", id)
            self._redaction = None

    def _redaction_pairs(self) -> list[tuple[str, str]]:
        with self._lock:
            if self._redaction is None:
                rows = self._query(
                    "SELECT data, value FROM credentials WHERE value != ''")
                # 只有 secret 变量的值在这一列,占位符写成 <环境>.<变量>
                self._redaction = redaction_pairs(
                    (f"{json.loads(row['data'])['name']}.{variable}", value)
                    for row in rows
                    for variable, value in _environment_secrets(row["value"]).items())
            return self._redaction

    def redact_credentials(self, text: str) -> str:
        """把文本里出现的 secret 变量原文换成 [credential:<环境>.<变量>],供各写入口
        落库前调用。"""
        pairs = self._redaction_pairs()
        return redact_text(text, pairs) if pairs and text else text

    def _redact_message(self, content: str,
                        spans: list[dict]) -> tuple[str, list[dict]]:
        """消息正文脱敏。提及范围是正文里的码点偏移,替换会改变长度,所以只替换
        提及之间的片段,再按新长度平移各提及范围。"""
        if not self._redaction_pairs() or not content:
            return content, spans
        ordered = sorted(spans, key=lambda item: item.get("start", 0))
        if not all(type(item.get("start")) is int and type(item.get("end")) is int
                   for item in ordered):
            return self.redact_credentials(content), spans
        out, shifted, cursor = "", [], 0
        for span in ordered:
            out += self.redact_credentials(content[cursor:span["start"]])
            start = len(out)
            out += content[span["start"]:span["end"]]
            shifted.append({**span, "start": start, "end": len(out)})
            cursor = span["end"]
        out += self.redact_credentials(content[cursor:])
        return out, shifted

    def touch_automation_run_state(self, id: str, status: str) -> None:
        """只更新最近运行状态字段，不覆盖脚本内容的并发编辑。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT data FROM automations WHERE id=?", (id,)).fetchone()
            if row is None:
                return
            data = json.loads(row["data"])
            data["last_run_at"] = time.time()
            data["last_status"] = status
            self._conn.execute(
                "UPDATE automations SET data=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), id))

    def start_automation_run(self, automation_id: str, project_id: str,
                             trigger: str, trigger_detail: str = "") -> int:
        return self._execute(
            "INSERT INTO automation_runs(automation_id,project_id,trigger,"
            "trigger_detail,status,started_at) VALUES(?,?,?,?,'running',?)",
            (automation_id, project_id, trigger, trigger_detail, time.time()),
        )

    def prune_automation_runs(self, cutoff: float) -> None:
        """删除 cutoff 之前开始的运行记录;每个脚本最近一条始终保留,
        低频脚本过了保留期也能看到上一次的结果。"""
        self._execute(
            "DELETE FROM automation_runs WHERE started_at<? AND id NOT IN "
            "(SELECT MAX(id) FROM automation_runs GROUP BY automation_id)",
            (cutoff,),
        )

    def finish_automation_run(self, run_id: int, status: str,
                              exit_code: Optional[int] = None,
                              stdout: str = "", stderr: str = "",
                              error: str = "") -> None:
        redact = self.redact_credentials
        self._execute(
            "UPDATE automation_runs SET status=?, exit_code=?, stdout=?, "
            "stderr=?, error=?, finished_at=? WHERE id=?",
            (status, exit_code, redact(stdout), redact(stderr), redact(error),
             time.time(), run_id),
        )

    def get_automation_run(self, run_id: int) -> Optional[dict]:
        rows = self._query(
            "SELECT * FROM automation_runs WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def list_automation_runs(self, automation_id: str, limit: int = 20,
                             before_id: int = 0) -> list[dict]:
        """按 id 倒序取最近 limit 条;before_id>0 时只取更早的记录,用于向前翻页。"""
        condition = "AND id<? " if before_id > 0 else ""
        limit = max(1, min(int(limit), 200))
        params = ((automation_id, before_id, limit) if before_id > 0
                  else (automation_id, limit))
        rows = self._query(
            f"SELECT * FROM automation_runs WHERE automation_id=? {condition}"
            "ORDER BY id DESC LIMIT ?", params)
        return [dict(row) for row in rows]

    def count_automation_runs(self, automation_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM automation_runs WHERE automation_id=?",
            (automation_id,))
        return int(rows[0]["n"]) if rows else 0

    # ---- 聊天:消息 ----
    def add_message(self, channel: str, author: str, author_type: str, content: str,
                    mentions: list[str], reply_to: Optional[int] = None,
                    root_id: Optional[int] = None, depth: int = 0,
                    runtime_id: str = "", model: str = "", effort: str = "",
                    kind: str = "message",
                    mention_spans: Optional[list[dict]] = None,
                    context: Optional[dict] = None) -> int:
        content, mention_spans = self._redact_message(content, mention_spans or [])
        msg_id = self._execute(
            "INSERT INTO messages(channel, author, author_type, content, mentions, "
            "mention_spans, context, runtime_id, model, effort, kind, reply_to, root_id, "
            "depth, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (channel, author, author_type, content,
             json.dumps(mentions, ensure_ascii=False),
             json.dumps(mention_spans or [], ensure_ascii=False),
             json.dumps(context or {}, ensure_ascii=False),
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

    def count_messages(self, channel: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM messages WHERE channel=?", (channel,))
        return int(rows[0]["n"]) if rows else 0

    def recent_messages(self, channel: str, limit: int = 20,
                        after_id: int = 0, before_id: int = 0) -> list[dict]:
        """取窗口内最新的 limit 条并按 id 升序返回;before_id>0 时只取更早
        的消息(id<before_id),用于聊天流向上翻页。"""
        condition = "AND id<? " if before_id > 0 else ""
        params = ((channel, after_id, before_id, limit) if before_id > 0
                  else (channel, after_id, limit))
        rows = self._query(
            "SELECT * FROM messages WHERE channel=? AND id>? "
            f"{condition}ORDER BY id DESC LIMIT ?", params)
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
                        error: str = "", model: str = "",
                        effort: str = "") -> bool:
        """更新未被用户停止的运行，避免迟到结果覆盖 ``stopped`` 终态。

        model/effort 只在传入非空时覆盖:转入 running 时盖章执行组合,
        终态更新不回传就保留原值。
        """
        finished = time.time() if status in ("done", "failed", "stopped") else None
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE chat_runs SET status=?, backend_id=?, error=?, finished_at=?, "
                "model=CASE WHEN ?='' THEN model ELSE ? END, "
                "effort=CASE WHEN ?='' THEN effort ELSE ? END "
                "WHERE id=? AND status!='stopped'",
                (status, backend_id, error, finished,
                 model, model, effort, effort, run_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

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
                "WHERE id=? AND status IN ('queued','running','waiting_user')",
                (backend_id, run_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def chat_run_is_active(self, run_id: int) -> bool:
        rows = self._query(
            "SELECT 1 FROM chat_runs WHERE id=? "
            "AND status IN ('queued','running','waiting_user')",
            (run_id,),
        )
        return bool(rows)

    def get_chat_run(self, run_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM chat_runs WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def stop_active_chat_runs(self, channel: str) -> list[dict]:
        """原子地把频道内全部活动运行改为终态，并返回停止前的快照。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_runs WHERE channel=? "
                "AND status IN ('queued','running','waiting_user') ORDER BY id",
                (channel,),
            ).fetchall()
            if not rows:
                return []
            now = time.time()
            self._conn.execute(
                "UPDATE chat_runs SET status='stopped', error='', finished_at=? "
                "WHERE channel=? AND status IN ('queued','running','waiting_user')",
                (now, channel),
            )
            self._conn.commit()
            return [dict(row) for row in rows]

    def stop_chat_run(self, run_id: int) -> Optional[dict]:
        """原子地停止单个活动运行，返回停止前的快照;已结束返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chat_runs WHERE id=? "
                "AND status IN ('queued','running','waiting_user')",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE chat_runs SET status='stopped', error='', finished_at=? "
                "WHERE id=?", (time.time(), run_id),
            )
            self._conn.commit()
            return dict(row)

    def active_chat_runs(self, channel: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM chat_runs WHERE channel=? "
            "AND status IN ('queued','running','waiting_user') "
            "ORDER BY id", (channel,))]

    def active_chat_run_counts(self) -> dict[str, int]:
        """按频道汇总未结束的运行数;侧栏一次查询即可标记哪些频道有 Agent 在跑。"""
        return {row["channel"]: int(row["n"]) for row in self._query(
            "SELECT channel, COUNT(*) AS n FROM chat_runs "
            "WHERE status IN ('queued','running','waiting_user') GROUP BY channel")}

    def count_chain_runs(self, root_id: int) -> int:
        """一条协作链(同一 root 消息)累计触发的执行数,用于防爆炸。"""
        rows = self._query("SELECT COUNT(*) AS n FROM chat_runs WHERE root_id=?", (root_id,))
        return rows[0]["n"]

    def chat_runs_for_trigger(self, message_id: int) -> list[dict]:
        """由某条消息直接触发的运行,用于回报派发实际启动情况。"""
        rows = self._query(
            "SELECT * FROM chat_runs WHERE trigger_message_id=?", (message_id,))
        return [dict(row) for row in rows]

    def finished_chat_chains(self, since: float, until: float) -> list[dict]:
        """``(since, until]`` 内整条结束的协作链,供浏览器"任务结束"通知增量拉取。

        协作链 = 同一频道里同一 root 消息引发的全部运行(主控派发、结果交回都沿用
        root)。引擎先发布回复(可能顺带派发下一轮)再把本轮标成终态,所以链上
        只要还有排队/运行/等待用户的运行就没结束;最后一次结束时刻落在窗口内的
        链只会被报告一次。status/error 取链上最后结束的那次运行。
        """
        rows = self._query(
            "SELECT r.channel, r.root_id, r.role_id, r.status, r.error, r.finished_at "
            "FROM chat_runs r WHERE (r.root_id, r.channel) IN ("
            "  SELECT root_id, channel FROM chat_runs "
            "  WHERE finished_at > ? AND finished_at <= ?) "
            "AND NOT EXISTS (SELECT 1 FROM chat_runs a "
            "  WHERE a.root_id=r.root_id AND a.channel=r.channel "
            "  AND a.status IN ('queued','running','waiting_user')) "
            "ORDER BY r.finished_at, r.id",
            (since, until))
        chains: dict[tuple[str, int], dict] = {}
        for row in rows:
            chain = chains.setdefault((row["channel"], row["root_id"]), {
                "channel": row["channel"], "root_id": row["root_id"],
                "roles": [], "runs": 0, "failed": 0})
            chain["runs"] += 1
            chain["failed"] += row["status"] == "failed"
            if row["role_id"] not in chain["roles"]:
                chain["roles"].append(row["role_id"])
            chain.update(status=row["status"], error=row["error"] or "",
                         finished_at=row["finished_at"])
        return [chain for chain in chains.values() if chain["finished_at"] <= until]

    def last_chain_reply(self, channel: str, root_id: int) -> Optional[dict]:
        """协作链里最后一条 Agent 回复,作为"任务结束"通知的正文。"""
        rows = self._query(
            "SELECT author, content FROM messages WHERE channel=? AND root_id=? "
            "AND author_type='agent' ORDER BY id DESC LIMIT 1", (channel, root_id))
        return dict(rows[0]) if rows else None

    # ---- 聊天:角色登记的一次性回调 ----
    def add_chat_callback(self, *, project_id: str, channel: str, role_id: str,
                          note: str, fire_at: float = 0.0, expires_at: float = 0.0,
                          watch_kind: str = "", watch_target: str = "",
                          baseline: str = "", spec: str = "", origin_run_id: int = 0,
                          origin_trigger: int = 0) -> int:
        note = self.redact_credentials(note)
        return self._execute(
            "INSERT INTO chat_callbacks(project_id,channel,role_id,note,fire_at,"
            "expires_at,watch_kind,watch_target,baseline,spec,origin_run_id,"
            "origin_trigger,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
            (project_id, channel, role_id, note, fire_at, expires_at, watch_kind,
             watch_target, baseline, self.redact_credentials(spec), origin_run_id,
             origin_trigger, time.time()))

    def get_chat_callback(self, callback_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM chat_callbacks WHERE id=?", (callback_id,))
        return dict(rows[0]) if rows else None

    def list_chat_callbacks(self, *, project_id: str = "", channel: str = "",
                            role_id: str = "", status: str = "pending",
                            limit: int = 200) -> list[dict]:
        clauses, params = [], []
        for column, value in (("project_id", project_id), ("channel", channel),
                              ("role_id", role_id), ("status", status)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return [dict(row) for row in self._query(
            f"SELECT * FROM chat_callbacks {where}ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 1000))))]

    def pending_chat_callback_counts(self) -> dict[str, int]:
        """按频道汇总等待中的回调数;侧栏一次查询即可标记哪些频道在等回调。"""
        return {row["channel"]: int(row["n"]) for row in self._query(
            "SELECT channel, COUNT(*) AS n FROM chat_callbacks "
            "WHERE status='pending' GROUP BY channel")}

    def finish_chat_callback(self, callback_id: int, status: str, reason: str = "",
                             message_id: int = 0) -> bool:
        """把仍在等待的回调记为 fired/cancelled/expired;已结束的不改,返回是否命中。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE chat_callbacks SET status=?, reason=?, message_id=?, "
                "finished_at=? WHERE id=? AND status='pending'",
                (status, reason, message_id, time.time(), callback_id))
            self._conn.commit()
            return cursor.rowcount > 0

    def cancel_chat_callbacks(self, reason: str, *, project_id: str = "",
                              channel: str = "", role_id: str = "") -> list[int]:
        """按归属批量取消等待中的回调(停止频道 Agent、删除频道/角色/项目)。"""
        clauses, params = ["status='pending'"], []
        for column, value in (("project_id", project_id), ("channel", channel),
                              ("role_id", role_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if len(clauses) == 1:
            raise ValueError("取消回调必须限定项目、频道或角色")
        where = " AND ".join(clauses)
        with self._lock:
            ids = [int(row["id"]) for row in self._conn.execute(
                f"SELECT id FROM chat_callbacks WHERE {where}", params).fetchall()]
            if ids:
                self._conn.execute(
                    f"UPDATE chat_callbacks SET status='cancelled', reason=?, "
                    f"finished_at=? WHERE {where}", (reason, time.time(), *params))
                self._conn.commit()
        return ids

    def set_chat_callback_message(self, callback_id: int, message_id: int) -> None:
        self._execute("UPDATE chat_callbacks SET message_id=? WHERE id=?",
                      (message_id, callback_id))

    def rebase_chat_callback(self, callback_id: int, baseline: str) -> None:
        self._execute(
            "UPDATE chat_callbacks SET baseline=? WHERE id=? AND status='pending'",
            (baseline, callback_id))

    def advance_chat_callback(self, callback_id: int, baseline: str, progress: str) -> None:
        """事件回调看完一批投递后推进游标并记下检查进度。"""
        self._execute(
            "UPDATE chat_callbacks SET baseline=?, progress=? WHERE id=? AND status='pending'",
            (baseline, self.redact_credentials(progress), callback_id))

    def last_message_id(self, channel: str) -> int:
        rows = self._query("SELECT MAX(id) AS last FROM messages WHERE channel=?", (channel,))
        return int(rows[0]["last"] or 0)

    def first_update_message(self, channel: str, after_id: int,
                             exclude_role: str = "") -> Optional[dict]:
        """频道里 after_id 之后第一条"有内容的更新":人类、Agent、自动化脚本的
        消息;平台回执与通知不算,登记者自己的消息也不算。"""
        rows = self._query(
            "SELECT * FROM messages WHERE channel=? AND id>? "
            "AND author_type IN ('human','agent','automation') "
            "AND NOT (author_type='agent' AND author=?) ORDER BY id LIMIT 1",
            (channel, after_id, exclude_role))
        return dict(rows[0]) if rows else None

    # ---- 聊天:Runtime 持久会话 ----
    def get_chat_session(self, session_key: str) -> Optional[dict]:
        rows = self._query(
            "SELECT * FROM chat_sessions WHERE session_key=?", (session_key,))
        return dict(rows[0]) if rows else None

    def put_chat_session(self, session_key: str, channel: str, role_id: str,
                         backend_id: str, adapter: str, workdir: str,
                         runtime_session_id: str, context_version: str, *,
                         turn_mode: str = "", turn_bytes: int = 0,
                         clear_reinject: bool = False,
                         resource_state: str = "") -> None:
        """保存会话 id、公共上下文重注入状态与已看到的资源正文版本表。

        turn_mode="lean" 累加增量回合计数;完整注入轮传 clear_reinject=True
        清零计数与压缩标记(本轮又检测到压缩时调用方不传 clear);turn_mode
        为空表示轮内的 id/版本刷新,不动计数。
        """
        now = time.time()
        if turn_mode == "lean":
            counters = (",lean_turns=chat_sessions.lean_turns+1"
                        ",lean_bytes=chat_sessions.lean_bytes+excluded.lean_bytes")
            initial = (0, 1, max(0, turn_bytes))
        elif turn_mode:
            counters = ",lean_turns=0,lean_bytes=0" + (
                ",needs_reinject=0" if clear_reinject else "")
            initial = (0, 0, 0)
        else:
            counters = ""
            initial = (0, 0, 0)
        self._execute(
            "INSERT INTO chat_sessions(session_key,channel,role_id,backend_id,adapter,"
            "workdir,runtime_session_id,context_version,created_at,updated_at,"
            "needs_reinject,lean_turns,lean_bytes,resource_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
            "channel=excluded.channel,role_id=excluded.role_id,"
            "backend_id=excluded.backend_id,adapter=excluded.adapter,"
            "workdir=excluded.workdir,runtime_session_id=excluded.runtime_session_id,"
            "context_version=excluded.context_version,updated_at=excluded.updated_at,"
            "resource_state=excluded.resource_state"
            + counters,
            (session_key, channel, role_id, backend_id, adapter, workdir,
             runtime_session_id, context_version, now, now, *initial,
             resource_state),
        )

    def mark_chat_session_reinject(self, session_key: str) -> None:
        """Runtime 报告上下文压缩:下一轮强制重注入完整公共上下文。"""
        self._execute(
            "UPDATE chat_sessions SET needs_reinject=1, updated_at=? "
            "WHERE session_key=?", (time.time(), session_key))

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
    # 换 kind 或单行超过上限时另起物理行,读取时再恢复为逻辑事件。
    RUN_EVENT_MAX = 8000
    RUN_EVENT_STRUCTURED_KINDS = frozenset({
        "permission_request", "user_input_request", "usage", "backend_agent",
    })

    def append_run_event(self, run_id: int, kind: str, text: str) -> Optional[int]:
        if not text:
            return None
        text = self.redact_credentials(text)
        # JSON 事件必须保持一行一个对象；相邻权限/用量事件不能字符串拼接。
        if kind in self.RUN_EVENT_STRUCTURED_KINDS:
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
                    if not self._append_redacted_across_boundary(int(last["id"]), chunk):
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

    def _append_redacted_across_boundary(self, event_id: int, chunk: str) -> bool:
        """流式文本按增量追加,一个凭据值可能被拆在相邻几次追加里,单看新片段认不出。
        把行尾最多 (最长原文 - 1) 个字符与新片段拼起来再查;命中时连同行尾一起
        改写并返回 True,否则返回 False 交给调用方普通追加。"""
        pairs = self._redaction_pairs()
        if not pairs:
            return False
        overlap = max(len(secret) for secret, _ in pairs) - 1
        tail = str(self._query(
            "SELECT substr(content, -?) AS tail FROM run_events WHERE id=?",
            (overlap, event_id))[0]["tail"] or "")
        merged = tail + chunk
        redacted = redact_text(merged, pairs)
        if redacted == merged:
            return False
        self._execute(
            "UPDATE run_events SET content = substr(content, 1, length(content) - ?) || ? "
            "WHERE id=?", (len(tail), redacted, event_id))
        return True

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
        events: list[dict] = []
        for row in reversed(rows):
            event = dict(row)
            previous = events[-1] if events else None
            if (previous and previous["kind"] == event["kind"]
                    and event["kind"] not in self.RUN_EVENT_STRUCTURED_KINDS):
                # RUN_EVENT_MAX 只是物理存储边界，不应在任意字符处制造新的
                # 用户可见日志。保留最新行 id，让前端仍能识别实时更新。
                previous["content"] += event["content"]
                previous["id"] = event["id"]
            else:
                events.append(event)
        return events

    def run_live_output(self, run_id: int) -> str:
        """运行迄今的全部 text 输出按序拼接。

        过程事件读取有最近 N 行的窗口限制,思考/工具事件交替会把最早的
        输出段挤出窗口;实时输出框必须拿到完整正文,所以单独全量拼接。
        """
        rows = self._query(
            "SELECT content FROM run_events WHERE run_id=? AND kind='text' "
            "ORDER BY id", (run_id,))
        return "".join(str(row["content"]) for row in rows)

    def rewrite_run_events(self, run_id: int, transform: Callable[[str], str],
                           kinds: set[str]) -> int:
        """在最终发布前改写已合并的文本事件，覆盖跨 chunk 的敏感路径。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, content FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchall()
            changed = [
                (updated, row["id"])
                for row in rows if row["kind"] in kinds
                if (updated := transform(str(row["content"]))) != row["content"]
            ]
            if changed:
                self._conn.executemany(
                    "UPDATE run_events SET content=? WHERE id=?", changed)
                self._conn.commit()
            return len(changed)

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
