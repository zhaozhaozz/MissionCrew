"""GitHub 轮询来源:用 GitHub token 定时查询事件,整理成与 webhook 同形的投递。

外部 webhook 需要一个 GitHub 够得着的公网地址,还要有仓库或组织的管理权限去配置;
只有一个个人访问令牌(PAT)时,平台改为自己去问 GitHub"有什么新事件"。一个
kind=github 的 webhook 来源描述要问什么(`WebhookSource.github`):

- 仓库事件流(每个仓库一条):`GET /repos/<owner>/<repo>/events`。事件类型与 webhook
  基本一一对应(issues、pull_request、issue_comment、push、release……),classic 与
  fine-grained PAT 都能用,私有仓库也可以。
- 通知流:`GET /notifications`,即 token 所属账号的通知收件箱(被 @、被指派、被请求
  评审、关注的仓库有动态……)。只有按 scope 授权的令牌(classic PAT)能用。

新事件经 webhooks.fan_out_event 交给订阅该来源的自动化脚本,脚本侧与外部 webhook
完全一样(`MISSIONCREW_TRIGGER=webhook`、事件文件),事件里 `transport` 为 `poll`。

与外部投递不同的三点:
- 不丢事件:进度(各流的已见事件与游标)存库,服务重启后接着取停机期间的事件;
  脚本排队放不下时不标记已见,下一轮再取,而不是丢弃。
- 不回放历史:一条流第一次轮询只记下基线,此前已有的事件不投递。
- 有延迟:最短每 60 秒问一次(GitHub 经 `X-Poll-Interval` 要求的下限),仓库事件
  接口自身还有延迟(通常一分钟内,GitHub 文档写明最坏可达数小时)。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

from ..core.models import WebhookSource
from ..core.store import Store
from .github import (GitHubError, GitHubResponse, Transport, github_request,
                     github_token, token_kind)
from .webhooks import fan_out_event, source_subscribers

REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
EVENT_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
MAX_REPOS = 50
MIN_INTERVAL_SECONDS = 60          # GitHub 的 X-Poll-Interval 下限,再短会被要求退避
MAX_INTERVAL_SECONDS = 3600
EVENTS_PAGE_SIZE = 100
EVENTS_MAX_PAGES = 3               # 事件接口最多保留 300 条
NOTIFICATIONS_PAGE_SIZE = 50
NOTIFICATIONS_MAX_PAGES = 5
SEEN_LIMIT = 1000                  # 每条仓库流记住的事件 id 数,远多于接口保留的 300 条
NOTIFICATIONS_STREAM = "notifications"
NOTIFICATION_EVENT = "notification"
# 仓库事件流能产生的事件名(同 webhook 的事件名),供页面勾选;白名单不限于此表
REPO_EVENT_NAMES = (
    "issues", "issue_comment", "pull_request", "pull_request_review",
    "pull_request_review_comment", "push", "create", "delete", "release",
    "commit_comment", "discussion", "fork", "watch", "member", "public", "gollum",
)
# 这些令牌不按 scope 授权,GitHub 的通知接口不接受
UNSCOPED_TOKEN_KINDS = {"fine_grained": "fine-grained PAT", "app_user": "GitHub App 令牌",
                        "app_installation": "GitHub App 令牌"}


def _string_list(raw, field: str) -> list[str]:
    """接受数组,或用换行、逗号、空白分隔的字符串(页面文本框)。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} 必须是字符串数组")
    return list(dict.fromkeys(item.strip() for item in raw if item.strip()))


def normalize_github_config(store: Store, raw: dict) -> dict:
    """校验并规整轮询配置;不合法抛 ValueError。

    repos 是要看的仓库(`owner/repo`);events 是仓库事件的白名单(webhook 事件名,
    空 = 全部),不影响通知流;notifications 开启通知流,participating 让它只含
    直接参与或被 @ 的通知;project_id 决定用哪份 GITHUB_TOKEN(空 = 全局凭据,
    否则是该项目生效的那份);interval_seconds 是轮询间隔。
    """
    if not isinstance(raw, dict):
        raise ValueError("github 配置必须是对象")
    unknown = sorted(set(raw) - {"repos", "events", "notifications", "participating",
                                 "project_id", "interval_seconds"})
    if unknown:
        raise ValueError(f"github 配置含未知字段: {', '.join(unknown)}")
    repos = _string_list(raw.get("repos"), "repos")
    for repo in repos:
        if not REPO_RE.fullmatch(repo):
            raise ValueError(f"仓库要写成 owner/repo: {repo}")
    if len(repos) > MAX_REPOS:
        raise ValueError(f"一个来源最多 {MAX_REPOS} 个仓库")
    events = [item.lower() for item in _string_list(raw.get("events"), "events")]
    for name in events:
        if not EVENT_NAME_RE.fullmatch(name):
            raise ValueError(f"事件名只含小写字母、数字、下划线: {name}")
    flags = {}
    for key in ("notifications", "participating"):
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        flags[key] = value
    if not repos and not flags["notifications"]:
        raise ValueError("至少填一个仓库,或开启通知")
    project_id = str(raw.get("project_id") or "").strip()
    if project_id and store.get_project(project_id) is None:
        raise ValueError(f"项目不存在: {project_id}")
    interval = raw.get("interval_seconds", MIN_INTERVAL_SECONDS)
    if (isinstance(interval, bool) or not isinstance(interval, int)
            or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        raise ValueError(
            f"interval_seconds 必须是 {MIN_INTERVAL_SECONDS}..{MAX_INTERVAL_SECONDS} 的整数")
    return {"repos": repos, "events": events, **flags,
            "project_id": project_id, "interval_seconds": interval}


def event_name(event_type: object) -> str:
    """事件接口的类型名转成 webhook 事件名:`PullRequestReviewEvent` → `pull_request_review`。"""
    base = str(event_type or "").removesuffix("Event")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _server_time(response: GitHubResponse, fallback: float) -> str:
    """基线取 GitHub 的时钟(响应头 Date):事件时间戳是 GitHub 打的,用本机时钟
    作基线会在两边时钟有偏差时漏掉或重放那段时间里的事件。"""
    try:
        return _iso(parsedate_to_datetime(response.headers["date"]).timestamp())
    except (KeyError, TypeError, ValueError):
        return _iso(fallback)


def _poll_event(source_id: str, name: str, delivery: str, payload: dict) -> dict:
    """与 webhooks.webhook_event 同形的事件;headers 只有 GitHub webhook 会带的两个
    标识头,让按请求头写的脚本也能用。"""
    return {
        "source": source_id, "provider": "github", "event": name, "delivery": delivery,
        "transport": "poll",
        "received_at": time.time(), "content_type": "application/json",
        "headers": {"x-github-event": name, "x-github-delivery": delivery},
        "payload": payload, "body": None,
    }


def repo_event(source_id: str, raw: dict) -> dict:
    """事件接口的一条事件 → 投递。payload 是 GitHub 给的事件正文,另补 webhook 正文里
    常用来过滤的 `repository`、`sender`、`organization`,原始事件的 id、类型与时间
    放在 `github_event`。"""
    repo = raw.get("repo") or {}
    full_name = str(repo.get("name") or "")
    payload = dict(raw.get("payload") or {})
    payload.setdefault("repository", {
        "id": repo.get("id"), "name": full_name.split("/")[-1], "full_name": full_name,
        "url": repo.get("url"), "html_url": f"https://github.com/{full_name}"})
    if raw.get("actor"):
        payload.setdefault("sender", raw["actor"])
    if raw.get("org"):
        payload.setdefault("organization", raw["org"])
    payload["github_event"] = {"id": str(raw.get("id")), "type": raw.get("type"),
                               "created_at": raw.get("created_at"),
                               "public": raw.get("public")}
    return _poll_event(source_id, event_name(raw.get("type")),
                       f"poll-{raw.get('id')}", payload)


def notification_event(source_id: str, thread: dict) -> dict:
    """一条通知 → 投递。通知只说"哪个 Issue/PR 因为什么原因有了动静":reason 是原因
    (mention、assign、review_requested、author、subscribed……),subject 是对象
    (标题、类型、API 地址,以及最新评论的 API 地址),具体内容由脚本再去取。"""
    payload = {
        "reason": thread.get("reason"), "unread": thread.get("unread"),
        "subject": thread.get("subject"), "repository": thread.get("repository"),
        "notification": {key: thread.get(key) for key in (
            "id", "updated_at", "last_read_at", "url", "subscription_url")},
    }
    return _poll_event(source_id, NOTIFICATION_EVENT,
                       f"poll-notification-{thread.get('id')}-{thread.get('updated_at')}",
                       payload)


class _StreamError(Exception):
    """一条事件流这一轮取不到:message 给人看,retry_at 是 GitHub 要求的最早重试时间。"""

    def __init__(self, message: str, retry_at: float = 0.0):
        super().__init__(message)
        self.message = message
        self.retry_at = retry_at


@dataclass
class _Item:
    """一份待投递的事件;mark 在投递后把它记为已见。"""
    event: dict
    mark: Callable[[], None]


def poll_status(store: Store, source: WebhookSource) -> dict:
    """页面与 Agent 看的轮询状态:不含已见事件等内部进度。"""
    state = store.get_webhook_source_state(source.id)
    streams = state.get("streams") or {}
    return {
        "last_poll_at": state.get("last_poll_at", 0.0),
        "next_poll_at": state.get("next_poll_at", 0.0),
        "last_error": state.get("last_error", ""),
        "last_event_at": state.get("last_event_at", 0.0),
        "delivered_total": state.get("delivered_total", 0),
        "pending": bool(state.get("pending")),
        # ready = 已建立基线,之后的新事件才会投递
        "streams": [{"key": key, "ready": "since" in item, "error": item.get("error", "")}
                    for key, item in streams.items()],
    }


class GitHubEventPoller:
    """轮询全部启用的 GitHub 来源。一个线程顺序处理,同一时刻只轮询一个来源。"""

    TICK_SECONDS = 10.0

    def __init__(self, store: Store, service, transport: Optional[Transport] = None):
        self.store = store
        self.service = service            # AutomationService:触发脚本、查询排队余量
        self.transport = transport
        self._lock = threading.Lock()     # 后台循环与页面的"立即检查"互斥
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="github-event-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def poke(self) -> None:
        """来源配置变化后立即检查一遍,不等下一个 tick。"""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_due(time.time())
            except Exception as exc:   # 轮询线程不能因单次异常退出
                self.store.audit("platform", "github_poll_failed",
                                 detail=f"error={exc}")
            self._wake.wait(self.TICK_SECONDS)
            self._wake.clear()

    def poll_due(self, now: float) -> list[str]:
        """轮询所有到点的来源,返回本次轮询过的来源 id。"""
        polled = []
        for source in self.store.list_webhook_sources():
            if source.kind != "github" or not source.enabled:
                continue
            state = self.store.get_webhook_source_state(source.id)
            if state.get("next_poll_at", 0.0) > now:
                continue
            try:
                self.poll(source.id, now)
            except LookupError:   # 刚被删除
                continue
            polled.append(source.id)
        return polled

    # ---- 一次轮询 ----
    def poll(self, source_id: str, now: Optional[float] = None) -> dict:
        """轮询一个来源的全部事件流并投递新事件,返回 poll_status。"""
        with self._lock:
            source = self.store.get_webhook_source(source_id)
            if source is None or source.kind != "github":
                raise LookupError(f"GitHub 轮询来源不存在: {source_id}")
            now = time.time() if now is None else now
            config = source.github
            previous = self.store.get_webhook_source_state(source.id)
            wanted = [f"repo:{repo}" for repo in config.get("repos", [])]
            if config.get("notifications"):
                wanted.append(NOTIFICATIONS_STREAM)
            # 只保留配置里还在的流:去掉的仓库连同进度一起丢弃,重新加回时从新基线开始
            old_streams = previous.get("streams") or {}
            streams = {key: dict(old_streams.get(key) or {}) for key in wanted}
            errors: list[str] = []
            delivered = 0
            pending = False
            retry_at = 0.0
            interval = float(config.get("interval_seconds", MIN_INTERVAL_SECONDS))

            token, _scope = github_token(self.store, config.get("project_id", ""))
            if not token:
                where = (f"项目 {config['project_id']}" if config.get("project_id")
                         else "全局凭据")
                errors.append(f"{where}里没有 GITHUB_TOKEN")
            else:
                # None = 没有脚本订阅:事件照常记为已见,但无处可投
                room = self._room(source)
                for key in wanted:
                    stream = streams[key]
                    label = "通知" if key == NOTIFICATIONS_STREAM else key.removeprefix("repo:")
                    try:
                        if key == NOTIFICATIONS_STREAM:
                            items, finish, hint = self._notifications(
                                token, source.id, config, stream, now)
                        else:
                            items, finish, hint = self._repo_events(
                                token, source.id, key.removeprefix("repo:"),
                                config, stream, now)
                        interval = max(interval, hint)
                        sent = 0
                        try:
                            for item in items:
                                if room is not None:
                                    if room <= 0:
                                        break
                                    fan_out_event(self.store, self.service, source,
                                                  item.event, auth="poll")
                                    room -= 1
                                    delivered += 1
                                item.mark()
                                sent += 1
                        finally:
                            # 投递中途出错也要把已投的记为已见,否则下一轮会重复投递
                            complete = sent == len(items)
                            pending = pending or not complete
                            finish(complete)
                        stream["error"] = ""
                    except _StreamError as exc:
                        stream["error"] = exc.message
                        errors.append(f"{label}: {exc.message}")
                        retry_at = max(retry_at, exc.retry_at)
                    except GitHubError as exc:
                        stream["error"] = str(exc)
                        errors.append(f"{label}: {exc}")
                    except Exception as exc:   # 响应结构出乎意料等:记下原因,不影响其他流
                        stream["error"] = f"处理失败: {exc}"
                        errors.append(f"{label}: 处理失败: {exc}")

            error = "; ".join(errors)
            state = {
                "streams": streams,
                "last_poll_at": now,
                "next_poll_at": max(now + interval, retry_at),
                "last_error": error,
                "last_event_at": now if delivered else previous.get("last_event_at", 0.0),
                "delivered_total": previous.get("delivered_total", 0) + delivered,
                "pending": pending,
            }
            self.store.put_webhook_source_state(source.id, state)
            # 同一个错误每分钟都会重现,只在错误内容变化时记审计
            if error and error != previous.get("last_error", ""):
                self.store.audit("platform", "github_poll_failed",
                                 detail=f"source={source.id} error={error}")
            return poll_status(self.store, source)

    def _room(self, source: WebhookSource) -> Optional[int]:
        """这一轮最多能投几份:取各订阅脚本排队余量的最小值,保证每份事件对所有
        订阅者都放得下。一个脚本只订阅一个来源,轮询又是单线程,余量不会被别处占掉。"""
        subscribers = source_subscribers(self.store, source.id)
        if not subscribers:
            return None
        return min(self.service.webhook_room(item.id) for item in subscribers)

    # ---- 访问 GitHub ----
    def _get(self, token: str, path: str, params: dict,
             headers: Optional[dict] = None) -> tuple[GitHubResponse, float]:
        """返回 (响应, GitHub 要求的最短轮询间隔);200 与 304 之外一律转成 _StreamError。"""
        response = github_request(token, path, params=params, headers=headers,
                                  transport=self.transport)
        try:
            hint = float(response.headers.get("x-poll-interval", 0) or 0)
        except ValueError:
            hint = 0.0
        if response.status in (200, 304):
            return response, hint
        retry_at = 0.0
        limited = (response.headers.get("x-ratelimit-remaining") == "0"
                   or "retry-after" in response.headers)
        if response.status in (403, 429) and limited:
            try:
                if "retry-after" in response.headers:
                    retry_at = time.time() + float(response.headers["retry-after"])
                else:
                    retry_at = float(response.headers.get("x-ratelimit-reset", 0))
            except ValueError:
                retry_at = 0.0
            raise _StreamError("触发 GitHub 限流,到点后自动重试", retry_at)
        if response.status == 401:
            raise _StreamError("token 无效或已过期(GitHub 返回 401)")
        if response.status == 404:
            raise _StreamError("找不到:仓库不存在,或 token 无权访问"
                               "(无权访问的私有仓库 GitHub 也返回 404)")
        if response.status == 403:
            raise _StreamError(f"token 权限不足: {response.message()}")
        raise _StreamError(f"GitHub 返回 {response.status}: {response.message()}")

    @staticmethod
    def _rows(response: GitHubResponse) -> list[dict]:
        data = response.json()
        if not isinstance(data, list):
            raise _StreamError("GitHub 返回的不是列表")
        return [item for item in data if isinstance(item, dict)]

    def _repo_events(self, token: str, source_id: str, repo: str, config: dict,
                     stream: dict, now: float):
        """取一个仓库的新事件,返回 (待投递事件, 收尾函数, 轮询间隔下限)。

        事件接口不保证按时间排序,新旧事件会穿插,所以不用"最大 id"作游标,而是
        记住见过的事件 id:没见过、且发生在基线之后的才是新事件。响应带 ETag,
        没有变化时 GitHub 回 304,不计入限流额度。
        """
        first = "since" not in stream
        path = f"/repos/{repo}/events"
        headers = ({"If-None-Match": stream["etag"]}
                   if stream.get("etag") and not first else None)
        response, hint = self._get(token, path, {"per_page": EVENTS_PAGE_SIZE}, headers)
        if response.status == 304:
            return [], lambda complete: None, hint
        seen_order = [str(item) for item in stream.get("seen") or []]
        seen = set(seen_order)
        page_rows = self._rows(response)
        rows = list(page_rows)
        page = 1
        # 基线要把接口现有的事件都记为已见;之后只有整页都是新事件时才需要再翻一页
        while (len(page_rows) == EVENTS_PAGE_SIZE and page < EVENTS_MAX_PAGES
               and (first or all(str(row.get("id")) not in seen for row in page_rows))):
            page += 1
            more, _ = self._get(token, path, {"per_page": EVENTS_PAGE_SIZE, "page": page})
            page_rows = self._rows(more)
            rows += page_rows
        etag = response.headers.get("etag", "")
        if first:
            stream.update(since=_server_time(response, now), etag=etag,
                          seen=[str(row.get("id")) for row in rows][-SEEN_LIMIT:])
            return [], lambda complete: None, hint

        allow = set(config.get("events") or [])
        # 接口把新事件排在前面:先倒过来再按时间做稳定排序,同一秒内的事件也保持先后
        fresh = {str(row.get("id")): row for row in reversed(rows)
                 if str(row.get("id")) not in seen}
        items: list[_Item] = []
        for event_id, row in sorted(fresh.items(),
                                    key=lambda pair: str(pair[1].get("created_at") or "")):
            # 基线之前发生、现在才露面的旧事件,以及白名单之外的事件:只记已见,不投递
            if (str(row.get("created_at") or "") < stream["since"]
                    or (allow and event_name(row.get("type")) not in allow)):
                seen_order.append(event_id)
                continue
            items.append(_Item(repo_event(source_id, row),
                               lambda event_id=event_id: seen_order.append(event_id)))

        def finish(complete: bool) -> None:
            stream["seen"] = seen_order[-SEEN_LIMIT:]
            # 还有事件没投完时不留 ETag:否则下一轮得到 304,剩下的事件永远取不回来
            stream["etag"] = etag if complete else ""

        return items, finish, hint

    def _notifications(self, token: str, source_id: str, config: dict,
                       stream: dict, now: float):
        """取账号通知收件箱里的新通知,返回值同 _repo_events。

        一条通知对应一个 Issue/PR 等对象,对象每有新动静,通知的 updated_at 就更新
        一次。游标 since 只取其后更新的通知,`seen` 记住 since 之后已投递的
        (通知 id → updated_at),同一次更新不重复投递。用 all=true 取含已读的:
        人在网页上先读掉通知,事件也不会漏。
        """
        kind = token_kind(token)
        if kind in UNSCOPED_TOKEN_KINDS:
            raise _StreamError(
                f"通知接口只接受 classic PAT(需要 notifications 或 repo 权限),"
                f"当前 token 是 {UNSCOPED_TOKEN_KINDS[kind]}")
        params = {"all": "true", "per_page": NOTIFICATIONS_PAGE_SIZE}
        if config.get("participating"):
            params["participating"] = "true"
        if "since" not in stream:
            # 第一次只确认 token 能访问通知接口,基线从现在开始,不回放收件箱里的旧通知
            response, hint = self._get(token, "/notifications", {**params, "per_page": 1})
            self._rows(response)
            stream.update(since=_server_time(response, now), seen={},
                          last_modified=response.headers.get("last-modified", ""))
            return [], lambda complete: None, hint

        params["since"] = stream["since"]
        headers = ({"If-Modified-Since": stream["last_modified"]}
                   if stream.get("last_modified") else None)
        response, hint = self._get(token, "/notifications", params, headers)
        if response.status == 304:
            return [], lambda complete: None, hint
        page_rows = self._rows(response)
        rows = list(page_rows)
        page = 1
        while len(page_rows) == NOTIFICATIONS_PAGE_SIZE and page < NOTIFICATIONS_MAX_PAGES:
            page += 1
            more, _ = self._get(token, "/notifications", {**params, "page": page})
            page_rows = self._rows(more)
            rows += page_rows

        seen = {str(key): str(value) for key, value in (stream.get("seen") or {}).items()}
        fresh: dict[str, dict] = {}
        for row in reversed(rows):   # 同 _repo_events:倒序后稳定排序,保持同一秒内的先后
            thread_id, updated = str(row.get("id")), str(row.get("updated_at") or "")
            if updated >= stream["since"] and seen.get(thread_id) != updated:
                fresh[thread_id] = row
        ordered = sorted(fresh.items(), key=lambda pair: str(pair[1].get("updated_at") or ""))
        items = [_Item(notification_event(source_id, row),
                       lambda thread_id=thread_id, row=row: seen.__setitem__(
                           thread_id, str(row.get("updated_at") or "")))
                 for thread_id, row in ordered]
        last_modified = response.headers.get("last-modified", "")

        def finish(complete: bool) -> None:
            if complete:
                # 全部投完:游标推进到最新一条的时间,更早的已见记录不再需要
                newest = max([stream["since"], *seen.values()])
                stream["since"] = newest
                stream["seen"] = {key: value for key, value in seen.items()
                                  if value >= newest}
                stream["last_modified"] = last_modified
            else:
                stream["seen"] = seen
                stream["last_modified"] = ""   # 同 ETag:没投完就不能让下一轮得到 304

        return items, finish, hint
