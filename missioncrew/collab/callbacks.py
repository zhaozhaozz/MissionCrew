"""角色登记的一次性回调:到点,或所盯的频道/Task 有更新、所盯的 webhook 来源收到匹配的
事件时,回到登记的频道重新启动该角色。

回调由角色经 Agent Tool `callback.create` 登记,归属登记那一轮的 频道 × 角色;人可以在
聊天页查看和取消。触发时平台在登记的频道发一条 kind=callback 的平台消息并照常启动该
角色:这是一轮正常运行,有自己的 Agent Tool 令牌,和 Runtime 自带的定时器不同,平台能
看见、能停止。

检测靠轮询(默认 5s),覆盖所有写入来源——人、Agent、自动化脚本、数据源同步——不必在
每条写路径埋钩子。频道"有更新"指登记之后出现人类、Agent 或自动化脚本的消息(平台回执
与通知不算,登记者自己的消息也不算);Task "有更新"指内容指纹变化(标题、摘要、正文、
标签、频道、链接、附注、归档、外部更新时间或新简报),数据源同步原样重写不算。登记者
自己经 Agent Tool 改这个 Task 时顺延基线,不会被自己的改动唤醒。

盯事件(watch_kind=event)是对一个 webhook 来源的一次性订阅:来源可以是接收外部投递的
webhook,也可以是 GitHub 轮询来源。每轮从来源的投递记录里取登记之后的新事件,先按事件名、
动作、仓库筛,再交给角色自带的过滤脚本(event_filters.py,只能判断、不能调用平台动作),
第一个命中的事件触发回调。这类回调必须给超时时间:到时还没等到,同样启动角色并说明
检查过多少事件、脚本出过什么错,不会悄悄过期。盯 GitHub 轮询来源并指定了仓库时,
轮询器把这个仓库也纳入查询(github_events.py 读 event_callback_repos)。

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
from .event_filters import run_event_filter

LOGGER = logging.getLogger(__name__)

CALLBACK_TICK_SECONDS = 5.0
MIN_DELAY_SECONDS = 30
MAX_DELAY_SECONDS = 30 * 86400
# 只盯更新、没给时间的回调最多等这么久,到期仍无更新就过期(不触发)
WATCH_TTL_SECONDS = 7 * 86400
# 同一 频道 × 角色 同时等待中的回调上限,防止循环登记失控
MAX_PENDING_PER_OWNER = 10
NOTE_MAX_CHARS = 2000
WATCH_KINDS = ("channel", "task", "event")
# 盯事件的回调每轮最多检查这么多条候选事件:每条可能要起一次过滤脚本
EVENT_SCAN_LIMIT = 20


def _utc_text(timestamp: float) -> str:
    """回调文本里的时间。Agent 也读这些文本,所以写 UTC;页面按这个格式识别并换成
    浏览器时区(web/js/sidebar.js 的 localizeUtcTimes),改格式要两边一起改。"""
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


def event_spec(row: dict) -> dict:
    """盯事件的回调的匹配条件:{event, action, repository, filter},缺的为空串。"""
    try:
        spec = json.loads(row.get("spec") or "{}")
    except json.JSONDecodeError:
        spec = {}
    return {key: str(spec.get(key) or "") for key in ("event", "action", "repository", "filter")}


def event_progress(row: dict) -> dict:
    """盯事件的回调已经检查过多少候选事件、过滤脚本出错几次及最近一次的原因。"""
    try:
        progress = json.loads(row.get("progress") or "{}")
    except json.JSONDecodeError:
        progress = {}
    return {"checked": int(progress.get("checked") or 0),
            "errors": int(progress.get("errors") or 0),
            "last_error": str(progress.get("last_error") or "")}


def event_callback_repos(store: Store, source_id: str) -> set[str]:
    """等待中的事件回调在这个来源上指定的仓库(小写)。GitHub 轮询来源据此把它们
    纳入查询,即使没有自动化脚本订阅这些仓库。"""
    repos = set()
    for row in store.list_chat_callbacks(status="pending", limit=1000):
        if row["watch_kind"] == "event" and row["watch_target"] == source_id:
            repository = event_spec(row)["repository"]
            if repository:
                repos.add(repository.lower())
    return repos


def _describe_event(row: dict) -> str:
    spec = event_spec(row)
    what = " ".join(part for part in (spec["event"], spec["action"]) if part) or "任意"
    extras = [f"仓库 {spec['repository']}"] if spec["repository"] else []
    if spec["filter"]:
        extras.append("且通过过滤脚本")
    return (f"来源 {row['watch_target']} 收到 {what} 事件"
            + (f"（{'，'.join(extras)}）" if extras else ""))


def describe_conditions(row: dict) -> str:
    """人类可读的触发条件,用于回执、列表与触发消息。"""
    if row["watch_kind"] == "event":
        text = _describe_event(row)
        if row["fire_at"]:
            text += f"，最晚等到 {_utc_text(row['fire_at'])}（到时没等到也会回调）"
        return text
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
                              if row["watch_kind"] == "channel" else row["watch_target"]),
                   # 盯事件:匹配条件(过滤脚本原文不回传,只说有没有)与检查进度
                   **({**{key: value for key, value in event_spec(row).items()
                          if key != "filter"},
                       "has_filter": bool(event_spec(row)["filter"]),
                       "progress": event_progress(row)}
                      if row["watch_kind"] == "event" else {})}
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
               spec: Optional[dict] = None,
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
            raise ValueError(
                "至少给出 delay_seconds/at,或要盯的 channel/task/webhook_source 之一")
        if watch_kind == "event" and not fire_at:
            raise ValueError("盯事件必须用 delay_seconds 或 at 给出超时时间:"
                             "到时还没等到匹配的事件也会回调你")
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
            elif watch_kind == "event":
                # 游标:只看登记之后进入投递记录的事件
                baseline = str(self.store.last_webhook_delivery_id(watch_target))
            expires_at = fire_at or (now + WATCH_TTL_SECONDS)
            callback_id = self.store.add_chat_callback(
                project_id=project_id, channel=channel_id, role_id=role_id,
                note=note, fire_at=fire_at, expires_at=expires_at,
                watch_kind=watch_kind, watch_target=watch_target, baseline=baseline,
                spec=json.dumps(spec, ensure_ascii=False) if spec else "",
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
                # 过滤脚本可能要跑几秒,放在锁外:锁还管着"登记者自己改 Task"的顺延
                hit = self._scan_events(row) if row["watch_kind"] == "event" else None
                with self._lock:
                    self._check(row, now, hit)
            except Exception:
                LOGGER.exception("Chat callback check failed: %s", row.get("id"))

    def _scan_events(self, row: dict) -> Optional[dict]:
        """从游标往后检查来源的新投递,返回第一个命中的投递摘要;没有命中返回 None。

        先用投递记录的摘要列筛事件名、动作、仓库,剩下的再逐个交给过滤脚本。看过的
        推进游标并累计进度;命中时游标停在它前面一条,万一这一轮没能触发(频道正在
        停止),下一轮还能再找到它。
        """
        spec = event_spec(row)
        cursor = int(row["baseline"] or 0)
        candidates = self.store.list_webhook_deliveries(
            row["watch_target"], limit=EVENT_SCAN_LIMIT, after_id=cursor,
            event=spec["event"], action=spec["action"], repository=spec["repository"])
        if not candidates:
            return None
        progress = event_progress(row)
        hit = None
        for delivery in candidates:
            if spec["filter"]:
                full = self.store.get_webhook_delivery(int(delivery["id"]))
                result = run_event_filter(spec["filter"], (full or {}).get("event_data") or {})
                if result.error:
                    progress["errors"] += 1
                    progress["last_error"] = result.error
                if not result.matched:
                    cursor = int(delivery["id"])
                    progress["checked"] += 1
                    continue
            hit = delivery
            cursor = int(delivery["id"]) - 1
            break
        self.store.advance_chat_callback(
            int(row["id"]), str(cursor), json.dumps(progress, ensure_ascii=False))
        return hit

    def _check(self, row: dict, now: float, event_hit: Optional[dict] = None) -> None:
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
        elif row["watch_kind"] == "event":
            if event_hit is not None:
                cause = "event"
                what = " ".join(part for part in (
                    event_hit["event"], event_hit["action"]) if part) or "事件"
                where = "，".join(part for part in (
                    event_hit["repository"],
                    f"投递 {event_hit['delivery']}" if event_hit["delivery"] else "") if part)
                detail = (f"来源 {row['watch_target']} 收到 {what}"
                          + (f"（{where}）" if where else "")
                          + f"；事件全文用 webhook.history 读取（source="
                            f"{row['watch_target']}，id={event_hit['id']}）")
                extra = {"source": row["watch_target"], "delivery_id": int(event_hit["id"]),
                         "event": event_hit["event"], "action": event_hit["action"],
                         "repository": event_hit["repository"]}
            elif self.store.get_webhook_source(row["watch_target"]) is None:
                cause, detail = "event", f"所盯的 webhook 来源 {row['watch_target']} 已删除"
        if not cause and row["fire_at"] and now >= row["fire_at"]:
            cause = "time"
            detail = f"{_utc_text(row['fire_at'])} 到点"
            if row["watch_kind"] == "event":
                # 超时同样回调:带上检查进度,角色据此判断是事件没来还是过滤条件写错了
                cause = "timeout"
                progress = event_progress(row)
                detail = (f"等到 {_utc_text(row['fire_at'])} 超时，"
                          f"{_describe_event(row)}仍未发生；期间检查了 "
                          f"{progress['checked']} 个候选事件")
                if progress["errors"]:
                    detail += (f"，过滤脚本出错 {progress['errors']} 次"
                               f"（最近一次：{progress['last_error']}）")
            elif row["watch_kind"]:
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
