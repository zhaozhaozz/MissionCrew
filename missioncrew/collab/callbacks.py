"""角色登记的一次性回调:到点,或所盯的频道/Task 有更新时,回到登记的频道重新启动该角色。

回调由角色经 Agent Tool `callback.create` 登记,归属登记那一轮的 频道 × 角色;人可以在
聊天页查看和取消。触发时平台在登记的频道发一条 kind=callback 的平台消息并照常启动该
角色:这是一轮正常运行,有自己的 Agent Tool 令牌,和 Runtime 自带的定时器不同,平台能
看见、能停止。

检测靠轮询(默认 5s),覆盖所有写入来源——人、Agent、自动化脚本、数据源同步——不必在
每条写路径埋钩子。频道"有更新"指登记之后出现人类、Agent 或自动化脚本的消息(平台回执
与通知不算,登记者自己的消息也不算);Task "有更新"指内容指纹变化(标题、摘要、正文、
标签、频道、链接、附注、归档、外部更新时间或新简报),数据源同步原样重写不算。登记者
自己经 Agent Tool 改这个 Task 时顺延基线,不会被自己的改动唤醒。

一次登记只回调一次:要继续等就在被回调的那一轮里再登记,两个角色互相盯也不会无限循环。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Generator, Optional

from ..core.store import Store

LOGGER = logging.getLogger(__name__)

CALLBACK_TICK_SECONDS = 5.0
MIN_DELAY_SECONDS = 30
MAX_DELAY_SECONDS = 30 * 86400
# 只盯更新、没给时间的回调最多等这么久,到期仍无更新就过期(不触发)
WATCH_TTL_SECONDS = 7 * 86400
# 同一 频道 × 角色 同时等待中的回调上限,防止循环登记失控
MAX_PENDING_PER_OWNER = 10
NOTE_MAX_CHARS = 2000
WATCH_KINDS = ("channel", "task")


def _utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def resolve_fire_at(delay_seconds, at, now: float) -> float:
    """把 delay_seconds(秒)或 at(带时区的 ISO 8601)换算成触发时间;都没给返回 0。"""
    if delay_seconds is not None and at not in (None, ""):
        raise ValueError("delay_seconds 与 at 只能给一个")
    if delay_seconds is not None:
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)):
            raise ValueError("delay_seconds 必须是秒数")
        fire_at = now + float(delay_seconds)
    elif at not in (None, ""):
        try:
            moment = datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"at 不是 ISO 8601 时间: {at}") from exc
        if moment.tzinfo is None:
            raise ValueError("at 必须带时区,例如 2026-11-09T07:22:00Z")
        fire_at = moment.timestamp()
    else:
        return 0.0
    if fire_at - now < MIN_DELAY_SECONDS:
        raise ValueError(f"回调时间至少在 {MIN_DELAY_SECONDS} 秒之后")
    if fire_at - now > MAX_DELAY_SECONDS:
        raise ValueError(f"回调时间最多在 {MAX_DELAY_SECONDS // 86400} 天之内")
    return fire_at


def task_fingerprint(store: Store, task_id: str) -> str:
    """Task 有意义内容的指纹;数据源同步原样重写只刷新 updated_at,指纹不变。"""
    task = store.get_task(task_id)
    if task is None:
        return "deleted"
    briefs = store.list_task_briefs(task_id, limit=1)
    payload = [task.title, task.summary, task.body, task.labels, task.channel_ids,
               task.url, task.meta, task.archived, task.external_updated_at,
               int(briefs[0]["id"]) if briefs else 0]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _short_channel(channel_id: str) -> str:
    return channel_id.split(":", 1)[-1]


def describe_conditions(row: dict) -> str:
    """人类可读的触发条件,用于回执、列表与触发消息。"""
    parts = []
    if row["watch_kind"] == "channel":
        parts.append(f"频道 #{_short_channel(row['watch_target'])} 有新消息")
    elif row["watch_kind"] == "task":
        parts.append(f"Task {row['watch_target']} 有更新")
    if row["fire_at"]:
        parts.append(f"{_utc_text(row['fire_at'])} 到点")
    text = " 或 ".join(parts)
    if row["watch_kind"] and not row["fire_at"] and row["expires_at"]:
        text += f"（最晚等到 {_utc_text(row['expires_at'])}）"
    return text


def public_callback(row: dict) -> dict:
    """给 Agent 与页面的回调视图。"""
    return {
        "id": int(row["id"]),
        "channel": _short_channel(row["channel"]),
        "role_id": row["role_id"],
        "note": row["note"],
        "conditions": describe_conditions(row),
        "fire_at": row["fire_at"] or None,
        "expires_at": row["expires_at"] or None,
        "watch": ({"kind": row["watch_kind"],
                   "target": (_short_channel(row["watch_target"])
                              if row["watch_kind"] == "channel" else row["watch_target"])}
                  if row["watch_kind"] else None),
        "status": row["status"],
        "reason": row.get("reason") or "",
        "message_id": int(row.get("message_id") or 0) or None,
        "created_at": row["created_at"],
    }


class CallbackService:
    """回调的登记、取消与轮询触发。

    ``fire(row, content, context)`` 由聊天引擎提供:在登记的频道发触发消息并启动角色,
    返回消息 id;频道正在停止时返回 None,回调留待下一轮再试。``notice(channel, content,
    context)`` 发一条不触发任何角色的平台消息(过期提示)。``_lock`` 串行化轮询检查与
    "登记者自己改 Task"的基线顺延,避免轮询在两者之间看到自己的改动。
    """

    def __init__(self, store: Store,
                 fire: Callable[[dict, str, dict], Optional[int]],
                 notice: Callable[[str, str, dict], None]):
        self.store = store
        self._fire = fire
        self._notice = notice
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 登记 / 取消 ----
    def create(self, *, project_id: str, channel_id: str, role_id: str, note: str,
               fire_at: float = 0.0, watch_kind: str = "", watch_target: str = "",
               origin_run_id: int = 0, origin_trigger: int = 0,
               now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        note = str(note or "").strip()
        if not note:
            raise ValueError("note 必填:写清回调时要做什么,触发消息会原样带上")
        if len(note) > NOTE_MAX_CHARS:
            raise ValueError(f"note 最多 {NOTE_MAX_CHARS} 个字符")
        if watch_kind and watch_kind not in WATCH_KINDS:
            raise ValueError(f"不支持的监听对象: {watch_kind}")
        if not fire_at and not watch_kind:
            raise ValueError("至少给出 delay_seconds/at,或要盯的 channel/task 之一")
        with self._lock:
            pending = self.store.list_chat_callbacks(
                channel=channel_id, role_id=role_id, status="pending")
            if len(pending) >= MAX_PENDING_PER_OWNER:
                raise ValueError(
                    f"本频道你已有 {len(pending)} 个等待中的回调,"
                    f"上限 {MAX_PENDING_PER_OWNER};先用 callback.cancel 取消不需要的")
            baseline = ""
            if watch_kind == "channel":
                baseline = str(self.store.last_message_id(watch_target))
            elif watch_kind == "task":
                baseline = task_fingerprint(self.store, watch_target)
            expires_at = fire_at or (now + WATCH_TTL_SECONDS)
            callback_id = self.store.add_chat_callback(
                project_id=project_id, channel=channel_id, role_id=role_id,
                note=note, fire_at=fire_at, expires_at=expires_at,
                watch_kind=watch_kind, watch_target=watch_target, baseline=baseline,
                origin_run_id=origin_run_id, origin_trigger=origin_trigger)
        row = self.store.get_chat_callback(callback_id)
        assert row is not None
        self.store.audit(f"role:{role_id}", "chat_callback_created",
                         detail=f"channel={channel_id} callback={callback_id} "
                                f"conditions={describe_conditions(row)}")
        return row

    def cancel(self, callback_id: int, actor: str, reason: str = "") -> bool:
        with self._lock:
            done = self.store.finish_chat_callback(
                callback_id, "cancelled", reason or f"{actor} 取消")
        if done:
            self.store.audit(actor, "chat_callback_cancelled",
                             detail=f"callback={callback_id}")
        return done

    @contextmanager
    def own_task_change(self, channel_id: str, role_id: str,
                        task_id: str) -> Generator[None, None, None]:
        """登记者自己改所盯的 Task:改动前后指纹相同于基线时顺延基线,不回调自己。

        改动前的指纹已偏离基线说明别人先改过,保留原基线照常触发。"""
        watching = [row for row in self.store.list_chat_callbacks(
                        channel=channel_id, role_id=role_id, status="pending")
                    if row["watch_kind"] == "task" and row["watch_target"] == task_id]
        if not watching:
            yield
            return
        with self._lock:
            before = task_fingerprint(self.store, task_id)
            yield
            after = task_fingerprint(self.store, task_id)
            if after != before:
                for row in watching:
                    if row["baseline"] == before:
                        self.store.rebase_chat_callback(int(row["id"]), after)

    # ---- 轮询 ----
    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        for row in self.store.list_chat_callbacks(status="pending", limit=1000):
            try:
                with self._lock:
                    self._check(row, now)
            except Exception:
                LOGGER.exception("Chat callback check failed: %s", row.get("id"))

    def _check(self, row: dict, now: float) -> None:
        current = self.store.get_chat_callback(int(row["id"]))
        if current is None or current["status"] != "pending":
            return   # 刚被取消
        row = current
        cause, detail, extra = "", "", {}
        if row["watch_kind"] == "channel":
            message = self.store.first_update_message(
                row["watch_target"], int(row["baseline"] or 0), exclude_role=row["role_id"])
            if message is not None:
                cause = "channel"
                author = (f"@{message['author']}" if message["author_type"] == "agent"
                          else message["author"])
                snippet = " ".join(str(message["content"]).split())
                snippet = snippet if len(snippet) <= 120 else snippet[:120] + "…"
                detail = (f"频道 #{_short_channel(row['watch_target'])} 有新消息"
                          f"（{author}：{snippet}）")
                extra = {"message_id": int(message["id"])}
            elif self.store.get_channel(row["watch_target"]) is None:
                cause, detail = "channel", f"所盯的频道 #{_short_channel(row['watch_target'])} 已删除"
        elif row["watch_kind"] == "task":
            fingerprint = task_fingerprint(self.store, row["watch_target"])
            if fingerprint != row["baseline"]:
                cause = "task"
                task = self.store.get_task(row["watch_target"])
                if task is None:
                    detail = f"所盯的 Task {row['watch_target']} 已删除"
                else:
                    status = task.status or "无状态"
                    detail = f"Task「{task.title}」（{task.id}）有更新，当前状态：{status}"
                    if task.archived:
                        detail += "，已归档"
                extra = {"task_id": row["watch_target"]}
        if not cause and row["fire_at"] and now >= row["fire_at"]:
            cause = "time"
            detail = f"{_utc_text(row['fire_at'])} 到点"
            if row["watch_kind"]:
                detail += f"，{describe_conditions({**row, 'fire_at': 0, 'expires_at': 0})}仍未发生"
        if not cause:
            if not row["fire_at"] and row["expires_at"] and now >= row["expires_at"]:
                self._expire(row)
            return
        content = (f"@{row['role_id']} 你登记的回调 #{row['id']} 已触发：{detail}。\n"
                   f"登记时的备注：{row['note']}")
        context: dict[str, object] = {"callback": {"id": int(row["id"]), "cause": cause,
                                "detail": detail, "note": row["note"],
                                "registered_at": row["created_at"],
                                "origin_run_id": int(row["origin_run_id"] or 0), **extra}}
        if int(row["origin_trigger"] or 0) > 0:
            # 结果交回规则沿用登记那一轮(人类直接点名则留在频道,否则交回主控)
            context["self_dispatch_origin"] = int(row["origin_trigger"])
        message_id = self._fire(row, content, context)
        if message_id:
            self.store.audit("platform", "chat_callback_fired",
                             detail=f"channel={row['channel']} role={row['role_id']} "
                                    f"callback={row['id']} cause={cause} message={message_id}")

    def _expire(self, row: dict) -> None:
        reason = f"{WATCH_TTL_SECONDS // 86400} 天内{describe_conditions({**row, 'expires_at': 0})}没有发生"
        if not self.store.finish_chat_callback(int(row["id"]), "expired", reason):
            return
        if self.store.get_channel(row["channel"]) is not None:
            self._notice(
                row["channel"],
                f"@{row['role_id']} 登记的回调 #{row['id']} 已过期：{reason}，未触发。"
                f"备注：{row['note']}",
                {"callback": {"id": int(row["id"]), "cause": "expired"}})
        self.store.audit("platform", "chat_callback_expired",
                         detail=f"channel={row['channel']} callback={row['id']}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="chat-callbacks", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(CALLBACK_TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Chat callback tick failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
