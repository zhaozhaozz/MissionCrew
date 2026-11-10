"""GitHub 轮询来源:用 token 查询仓库事件与通知,整理成 webhook 同形的投递触发订阅脚本。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from missioncrew.api import create_app
from missioncrew.api import webhooks as webhook_api
from missioncrew.collab import automations as automation_mod
from missioncrew.collab import github as github_mod
from missioncrew.collab.automations import AutomationService, save_automation
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.credentials import save_credential
from missioncrew.collab.github import GitHubResponse
from missioncrew.collab.github_events import (GitHubEventPoller, event_name,
                                              normalize_github_config)
from missioncrew.collab.webhooks import save_webhook_source

CLASSIC = "ghp_" + "a1B2c3D4e5" * 4
PROJECT_TOKEN = "ghp_" + "Z9y8X7w6V5" * 4
FINE = "github_pat_" + "Q1w2E3r4T5" * 6
SERVER_DATE = "Tue, 10 Nov 2026 03:30:00 GMT"       # 假 GitHub 的时钟,基线取它
BEFORE, AFTER = "2026-11-10T03:00:00Z", "2026-11-10T03:31:00Z"


class FakeGitHub:
    """只实现轮询用到的两个接口:仓库事件(带 ETag、分页)与通知(since、Last-Modified)。"""

    def __init__(self):
        self.events: dict[str, list[dict]] = {}     # 仓库 → 事件,新的在前
        self.notifications: list[dict] = []
        self.calls: list[SimpleNamespace] = []
        self.failures: dict[str, GitHubResponse] = {}   # 路径 → 固定返回的错误
        self._next_id = 1000

    def add_event(self, repo, kind, payload, created_at=AFTER, actor="alice"):
        self._next_id += 1
        self.events.setdefault(repo, []).insert(0, {
            "id": str(self._next_id), "type": kind, "created_at": created_at,
            "public": True, "actor": {"login": actor}, "repo": {"id": 7, "name": repo},
            "payload": payload})
        return str(self._next_id)

    def notify(self, thread_id, reason, updated_at, title="Fix cart", kind="Issue"):
        self.notifications = [item for item in self.notifications if item["id"] != thread_id]
        self.notifications.insert(0, {
            "id": thread_id, "reason": reason, "unread": True, "updated_at": updated_at,
            "last_read_at": None, "url": f"https://api.github.com/notifications/threads/{thread_id}",
            "subscription_url": "", "subject": {
                "title": title, "type": kind,
                "url": "https://api.github.com/repos/acme/webshop/issues/42",
                "latest_comment_url": "https://api.github.com/repos/acme/webshop/issues/comments/9"},
            "repository": {"full_name": "acme/webshop"}})

    def __call__(self, method, url, headers, timeout):
        parsed = urlparse(url)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        self.calls.append(SimpleNamespace(path=parsed.path, params=params, headers=headers,
                                          token=headers["Authorization"].removeprefix("Bearer ")))
        if parsed.path in self.failures:
            return self.failures[parsed.path]
        base = {"date": SERVER_DATE, "x-poll-interval": "60"}
        if parsed.path == "/notifications":
            rows = [item for item in self.notifications
                    if item["updated_at"] >= params.get("since", "")]
            modified = max([item["updated_at"] for item in self.notifications], default="none")
            if headers.get("If-Modified-Since") == modified:
                return GitHubResponse(304, base)
            base["last-modified"] = modified
        else:
            repo = parsed.path.removeprefix("/repos/").removesuffix("/events")
            rows = self.events.get(repo, [])
            etag = hashlib.sha1(json.dumps([row["id"] for row in rows]).encode()).hexdigest()
            if headers.get("If-None-Match") == etag:
                return GitHubResponse(304, base)
            base["etag"] = etag
        size, page = int(params.get("per_page", 30)), int(params.get("page", 1))
        return GitHubResponse(200, base, json.dumps(rows[(page - 1) * size:page * size]).encode())


def _wait_runs(store, automation_id, count, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = store.list_automation_runs(automation_id, 50)
        if len(runs) >= count and all(run["status"] != "running" for run in runs):
            return list(reversed(runs))
        time.sleep(0.05)
    raise AssertionError("automation runs did not finish in time")


def _wait_idle(service, timeout=10.0):
    deadline = time.time() + timeout
    while service.running_ids() and time.time() < deadline:
        time.sleep(0.02)
    assert service.running_ids() == set()


SCRIPT = ('echo "trigger=$MISSIONCREW_TRIGGER provider=$MISSIONCREW_WEBHOOK_PROVIDER '
          'event=$MISSIONCREW_WEBHOOK_EVENT delivery=$MISSIONCREW_WEBHOOK_DELIVERY"\n'
          'cat "$MISSIONCREW_WEBHOOK_EVENT_FILE"\n')


@pytest.fixture()
def watch(seeded):
    """全局 token + 看 acme/webshop 的轮询来源 + 订阅它的脚本 + 假 GitHub。"""
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=CLASSIC)
    github = FakeGitHub()
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    source, _ = save_webhook_source(seeded, id="watch", kind="github",
                                    github={"repos": ["acme/webshop"]})
    automation, _ = save_automation(seeded, "webshop", id="on-event",
                                    webhook_source="watch", script=SCRIPT)
    poller = GitHubEventPoller(seeded, service, transport=github)
    return SimpleNamespace(store=seeded, github=github, service=service, poller=poller,
                           source=source, automation=automation)


def _event_of(run) -> dict:
    """脚本第二行起打印的事件文件内容。"""
    return json.loads(run["stdout"].split("\n", 1)[1])


# ---- 配置校验 ----

def test_config_is_normalized_and_validated(seeded):
    config = normalize_github_config(seeded, {
        "repos": "acme/webshop, acme/api\nacme/webshop", "events": ["Issues", "pull_request"],
        "notifications": True, "project_id": "webshop", "interval_seconds": 120})
    assert config == {"repos": ["acme/webshop", "acme/api"], "events": ["issues", "pull_request"],
                      "notifications": True, "participating": False,
                      "project_id": "webshop", "interval_seconds": 120}
    for bad in ({}, {"repos": ["not-a-repo"]}, {"repos": ["acme/webshop"], "interval_seconds": 5},
                {"repos": ["acme/webshop"], "project_id": "missing"},
                {"repos": ["acme/webshop"], "notifications": "yes"},
                {"repos": ["acme/webshop"], "events": ["bad name"]},
                {"repos": ["acme/webshop"], "token": "x"}):
        with pytest.raises(ValueError):
            normalize_github_config(seeded, bad)
    assert event_name("PullRequestReviewCommentEvent") == "pull_request_review_comment"
    assert event_name("IssuesEvent") == "issues" and event_name("PushEvent") == "push"

    with pytest.raises(ValueError):       # 类型创建后不能改
        save_webhook_source(seeded, id="plain")
        save_webhook_source(seeded, id="plain", kind="github", github={"repos": ["a/b"]})
    with pytest.raises(ValueError):       # 普通 webhook 来源不带轮询配置
        save_webhook_source(seeded, id="other", github={"repos": ["a/b"]})


# ---- 仓库事件流 ----

def test_first_poll_only_sets_baseline_then_new_events_run_the_script(watch):
    watch.github.add_event("acme/webshop", "IssuesEvent",
                           {"action": "opened", "issue": {"number": 1}}, created_at=BEFORE)
    status = watch.poller.poll("watch", now=1000.0)
    assert status["streams"] == [{"key": "repo:acme/webshop", "ready": True, "error": ""}]
    assert status["delivered_total"] == 0 and status["next_poll_at"] == 1060.0
    assert watch.store.list_automation_runs(watch.automation.id, 5) == []   # 历史事件不回放

    event_id = watch.github.add_event(
        "acme/webshop", "IssuesEvent",
        {"action": "opened", "issue": {"number": 42, "title": "Cart is empty"}})
    status = watch.poller.poll("watch", now=1060.0)
    assert status["delivered_total"] == 1 and status["last_event_at"] == 1060.0
    [run] = _wait_runs(watch.store, watch.automation.id, 1)
    assert run["trigger"] == "webhook"
    assert run["trigger_detail"] == f"watch: github issues poll-{event_id}"
    assert run["stdout"].startswith(
        f"trigger=webhook provider=github event=issues delivery=poll-{event_id}\n")
    event = _event_of(run)
    assert event["transport"] == "poll" and event["source"] == "watch"
    assert event["headers"] == {"x-github-event": "issues",
                                "x-github-delivery": f"poll-{event_id}"}
    payload = event["payload"]
    assert payload["action"] == "opened" and payload["issue"]["number"] == 42
    # 补上 webhook 正文里常用来过滤的字段
    assert payload["repository"]["full_name"] == "acme/webshop"
    assert payload["repository"]["html_url"] == "https://github.com/acme/webshop"
    assert payload["sender"] == {"login": "alice"}
    assert payload["github_event"]["type"] == "IssuesEvent"
    assert CLASSIC not in json.dumps(event)


def test_unchanged_feed_is_a_conditional_request_and_events_are_not_repeated(watch):
    watch.poller.poll("watch", now=1000.0)
    watch.github.add_event("acme/webshop", "PullRequestEvent",
                           {"action": "merged", "number": 7, "pull_request": {"number": 7}})
    watch.poller.poll("watch", now=1060.0)
    _wait_runs(watch.store, watch.automation.id, 1)
    _wait_idle(watch.service)

    watch.github.calls.clear()
    status = watch.poller.poll("watch", now=1120.0)
    [call] = watch.github.calls
    assert call.headers.get("If-None-Match")           # 带着上次的 ETag,GitHub 回 304
    assert status["delivered_total"] == 1
    # 基线之前发生、现在才出现在列表里的旧事件(接口不保证顺序)不投递
    watch.github.add_event("acme/webshop", "IssuesEvent", {"action": "closed"},
                           created_at=BEFORE)
    assert watch.poller.poll("watch", now=1180.0)["delivered_total"] == 1
    [run] = watch.store.list_automation_runs(watch.automation.id, 5)
    assert _event_of(run)["payload"]["action"] == "merged"    # 合并以 action=merged 送达


def test_event_allowlist_filters_repository_events(watch):
    save_webhook_source(watch.store, id="watch", github={
        "repos": ["acme/webshop"], "events": ["pull_request"]})
    watch.poller.poll("watch", now=1000.0)
    watch.github.add_event("acme/webshop", "WatchEvent", {"action": "started"})
    watch.github.add_event("acme/webshop", "PullRequestEvent", {"action": "opened", "number": 8})
    watch.github.add_event("acme/webshop", "IssueCommentEvent", {"action": "created"})
    assert watch.poller.poll("watch", now=1060.0)["delivered_total"] == 1
    [run] = _wait_runs(watch.store, watch.automation.id, 1)
    assert "event=pull_request " in run["stdout"]
    _wait_idle(watch.service)
    # 被过滤的事件已记为已见,放开白名单后也不会补投
    save_webhook_source(watch.store, id="watch", github={"repos": ["acme/webshop"]})
    assert watch.poller.poll("watch", now=1120.0)["delivered_total"] == 1


def test_backlog_beyond_first_page_is_fetched(watch):
    watch.poller.poll("watch", now=1000.0)
    for number in range(130):
        watch.github.add_event("acme/webshop", "WatchEvent", {"action": "started", "n": number})
    save_automation(watch.store, "webshop", id="on-event", webhook_source="")   # 无订阅者:只记已见
    watch.github.calls.clear()
    status = watch.poller.poll("watch", now=1060.0)
    assert [call.params.get("page", "1") for call in watch.github.calls] == ["1", "2"]
    assert status["delivered_total"] == 0 and not status["pending"]
    assert len(watch.store.get_webhook_source_state("watch")["streams"]
               ["repo:acme/webshop"]["seen"]) == 130


# ---- 通知流 ----

def test_notifications_deliver_each_update_once(watch):
    save_webhook_source(watch.store, id="watch", github={"notifications": True})
    watch.github.notify("t1", "subscribed", BEFORE)
    watch.poller.poll("watch", now=1000.0)              # 基线:收件箱里已有的不回放
    assert watch.store.list_automation_runs(watch.automation.id, 5) == []

    watch.github.notify("t2", "mention", "2026-11-10T03:31:00Z", title="Please review")
    assert watch.poller.poll("watch", now=1060.0)["delivered_total"] == 1
    [run] = _wait_runs(watch.store, watch.automation.id, 1)
    assert "event=notification delivery=poll-notification-t2-2026-11-10T03:31:00Z" in run["stdout"]
    payload = _event_of(run)["payload"]
    assert payload["reason"] == "mention" and payload["subject"]["title"] == "Please review"
    assert payload["subject"]["latest_comment_url"].endswith("/issues/comments/9")
    assert payload["repository"] == {"full_name": "acme/webshop"}
    assert payload["notification"]["id"] == "t2"
    _wait_idle(watch.service)

    # 没有新动静:不重复投递,并且带着游标与条件请求头
    watch.github.calls.clear()
    assert watch.poller.poll("watch", now=1120.0)["delivered_total"] == 1
    [call] = watch.github.calls
    assert call.params["since"] == "2026-11-10T03:31:00Z" and call.params["all"] == "true"
    assert call.headers.get("If-Modified-Since") == "2026-11-10T03:31:00Z"
    # 同一条通知有了新动静(updated_at 变了)再投一次
    watch.github.notify("t2", "mention", "2026-11-10T03:35:00Z")
    assert watch.poller.poll("watch", now=1180.0)["delivered_total"] == 2
    _wait_runs(watch.store, watch.automation.id, 2)


def test_notifications_need_a_scoped_token_but_other_streams_keep_working(watch):
    save_credential(watch.store, project_id="", name="GITHUB_TOKEN", value=FINE)
    save_webhook_source(watch.store, id="watch", github={
        "repos": ["acme/webshop"], "notifications": True, "participating": True})
    status = watch.poller.poll("watch", now=1000.0)
    assert "通知: 通知接口只接受 classic PAT" in status["last_error"]
    assert "fine-grained PAT" in status["last_error"]
    assert {item["key"]: item["ready"] for item in status["streams"]} == {
        "repo:acme/webshop": True, "notifications": False}
    assert all(call.path != "/notifications" for call in watch.github.calls)   # 不白发请求
    watch.github.add_event("acme/webshop", "PushEvent", {"ref": "refs/heads/main"})
    assert watch.poller.poll("watch", now=1060.0)["delivered_total"] == 1

    save_credential(watch.store, project_id="", name="GITHUB_TOKEN", value=CLASSIC)
    assert watch.poller.poll("watch", now=1120.0)["last_error"] == ""
    assert watch.github.calls[-1].params["participating"] == "true"


# ---- 背压、持久化与订阅 ----

def test_events_wait_for_queue_room_instead_of_being_dropped(watch, monkeypatch):
    monkeypatch.setattr(automation_mod, "WEBHOOK_QUEUE_LIMIT", 1)
    save_automation(watch.store, "webshop", id="on-event",
                    script='sleep 0.4\necho "$MISSIONCREW_WEBHOOK_DELIVERY"\n')
    watch.poller.poll("watch", now=1000.0)
    ids = [watch.github.add_event("acme/webshop", "IssuesEvent", {"action": "opened", "n": n})
           for n in range(3)]
    first = watch.poller.poll("watch", now=1060.0)      # 排队余量 1:只交出最早的一个
    assert first["delivered_total"] == 1 and first["pending"] is True
    second = watch.poller.poll("watch", now=1061.0)     # 脚本在跑,第二个进排队
    assert second["delivered_total"] == 2 and second["pending"] is True
    assert watch.poller.poll("watch", now=1062.0)["delivered_total"] == 2   # 排队已满,先不取
    _wait_runs(watch.store, watch.automation.id, 2)
    _wait_idle(watch.service)
    last = watch.poller.poll("watch", now=1063.0)
    assert last["delivered_total"] == 3 and last["pending"] is False
    runs = _wait_runs(watch.store, watch.automation.id, 3)
    assert [run["stdout"].strip() for run in runs] == [f"poll-{item}" for item in ids]
    assert not [row for row in watch.store.list_audit(limit=200)
                if row["action"] == "automation_webhook_dropped"]


def test_progress_survives_restart_and_catches_up_on_downtime_events(watch):
    watch.poller.poll("watch", now=1000.0)
    watch.github.add_event("acme/webshop", "IssuesEvent", {"action": "opened"})
    watch.poller.poll("watch", now=1060.0)
    _wait_runs(watch.store, watch.automation.id, 1)
    _wait_idle(watch.service)

    # 服务停机期间又来了一个事件;新进程的轮询器从库里的进度接着取
    missed = watch.github.add_event("acme/webshop", "IssuesEvent", {"action": "closed"})
    restarted = GitHubEventPoller(watch.store, watch.service, transport=watch.github)
    assert restarted.poll_due(now=5000.0) == ["watch"]
    runs = _wait_runs(watch.store, watch.automation.id, 2)
    assert f"delivery=poll-{missed}" in runs[-1]["stdout"]
    assert restarted.poll_due(now=5001.0) == []         # 没到下一次轮询时间


def test_poll_due_skips_disabled_sources_and_events_without_subscribers_are_not_replayed(watch):
    save_automation(watch.store, "webshop", id="on-event", webhook_source="")
    watch.poller.poll("watch", now=1000.0)
    watch.github.add_event("acme/webshop", "IssuesEvent", {"action": "opened"})
    assert watch.poller.poll("watch", now=1060.0)["delivered_total"] == 0
    # 与外部 webhook 一致:没人订阅时到达的事件不留给后来的订阅者
    save_automation(watch.store, "webshop", id="on-event", webhook_source="watch")
    assert watch.poller.poll("watch", now=1120.0)["delivered_total"] == 0

    save_webhook_source(watch.store, id="watch", enabled=False)
    assert watch.poller.poll_due(now=9000.0) == []
    save_webhook_source(watch.store, id="watch", enabled=True)
    assert watch.poller.poll_due(now=9000.0) == ["watch"]


# ---- 身份与出错 ----

def test_source_uses_the_chosen_project_token(watch):
    save_credential(watch.store, project_id="webshop", name="GITHUB_TOKEN", value=PROJECT_TOKEN)
    watch.poller.poll("watch", now=1000.0)
    assert watch.github.calls[-1].token == CLASSIC       # 默认用全局凭据
    save_webhook_source(watch.store, id="watch", github={
        "repos": ["acme/webshop"], "project_id": "webshop"})
    watch.poller.poll("watch", now=1060.0)
    assert watch.github.calls[-1].token == PROJECT_TOKEN


def test_errors_are_reported_per_stream_and_audited_once(watch):
    save_webhook_source(watch.store, id="watch", github={"repos": ["acme/webshop", "acme/gone"]})
    watch.github.failures["/repos/acme/gone/events"] = GitHubResponse(
        404, {}, b'{"message": "Not Found"}')
    for now in (1000.0, 1060.0):
        status = watch.poller.poll("watch", now=now)
    assert status["last_error"].startswith("acme/gone: 找不到")
    assert {item["key"]: bool(item["error"]) for item in status["streams"]} == {
        "repo:acme/webshop": False, "repo:acme/gone": True}
    failed = [row for row in watch.store.list_audit(limit=200)
              if row["action"] == "github_poll_failed"]
    assert len(failed) == 1                              # 同一个错误只记一次

    # 限流:按 GitHub 给的重置时间推迟下一次轮询
    watch.github.failures["/repos/acme/gone/events"] = GitHubResponse(
        403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999"},
        b'{"message": "API rate limit exceeded"}')
    limited = watch.poller.poll("watch", now=1120.0)
    assert "限流" in limited["last_error"] and limited["next_poll_at"] == 99999.0

    from missioncrew.collab.credentials import delete_credential
    delete_credential(watch.store, project_id="", name="GITHUB_TOKEN")
    assert "全局凭据里没有 GITHUB_TOKEN" in watch.poller.poll("watch", now=1180.0)["last_error"]


# ---- HTTP API、Agent Tool 与页面 ----

def test_source_api_creates_polls_and_never_accepts_external_posts(seeded, monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(github_mod, "http_transport", github)
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=CLASSIC)
    with TestClient(create_app()) as client:
        assert client.post("/api/webhook-sources", json={
            "id": "watch", "kind": "github", "github": {}}).status_code == 400
        created = client.post("/api/webhook-sources", json={
            "id": "watch", "kind": "github", "name": "webshop 动态",
            "github": {"repos": ["acme/webshop"], "events": ["issues"]}})
        assert created.status_code == 200, created.text
        data = created.json()
        assert data["kind"] == "github" and data["webhook_path"] == "" and "secret" not in data
        assert data["github"]["repos"] == ["acme/webshop"] and "poll" in data
        assert client.post("/api/webhook-sources", json={
            "id": "watch", "kind": "webhook"}).status_code == 400      # 类型不能改

        saved = client.post("/api/projects/webshop/automations", json={
            "id": "on-issue", "script": "echo got $MISSIONCREW_WEBHOOK_EVENT",
            "webhook_source": "watch"})
        assert saved.status_code == 200 and saved.json()["webhook_path"] == ""
        polled = client.post("/api/webhook-sources/watch/poll").json()   # 建立基线
        assert polled["poll"]["streams"][0]["ready"] is True
        github.add_event("acme/webshop", "IssuesEvent", {"action": "opened"})
        polled = client.post("/api/webhook-sources/watch/poll").json()
        assert polled["poll"]["delivered_total"] == 1
        [run] = _wait_runs(seeded, "webshop:on-issue", 1)
        assert run["stdout"].strip() == "got issues"

        listed = client.get("/api/webhook-sources").json()
        assert "issues" in listed["github_events"] and "pull_request" in listed["github_events"]
        assert CLASSIC not in json.dumps(listed)
        client.post("/api/webhook-sources", json={"id": "plain"})
        assert client.post("/api/webhook-sources/plain/poll").status_code == 400
        client.post("/api/webhook-sources", json={"id": "watch", "enabled": False})
        assert client.post("/api/webhook-sources/watch/poll").status_code == 409

    # 专用 webhook 端口不接受发给轮询来源的外部投递
    ctx = SimpleNamespace(store=seeded, automations=None)
    hooks = StarletteClient(webhook_api.create_webhook_app(ctx))
    assert hooks.post("/hooks/watch", content=b"{}").status_code == 404
    # 删除来源连同进度一起清掉
    save_automation(seeded, "webshop", id="on-issue", webhook_source="")
    from missioncrew.collab.webhooks import delete_webhook_source
    delete_webhook_source(seeded, "watch")
    assert seeded.get_webhook_source_state("watch") == {}


def test_orchestrator_sees_what_a_polling_source_emits(watch):
    channel = watch.store.get_channel("general")
    chat = ChatEngine(watch.store, max_workers=2)
    root_id = watch.store.add_message(channel.id, "human", "human", "@lead 执行", ["lead"])
    run_id = watch.store.add_chat_run(channel.id, "lead", root_id, root_id, 0)
    watch.store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, watch.store.get_role("webshop", "lead"),
                            watch.store.get_backend("std-1"), root_id, run_id=run_id)
    identity = chat.agent_tools.authenticate(
        Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip())
    listed = chat.agent_tools.execute(identity, "automation.list", {}, run_id, "list-1")
    assert listed["webhook_sources"] == [{
        "id": "watch", "name": "watch", "description": "", "enabled": True,
        "kind": "github", "webhook_path": "",
        "github": {"repos": ["acme/webshop"], "events": [],
                   "notifications": False, "participating": False}}]
    manual = Path(config.env["MISSIONCREW_MANUAL"]).read_text()
    assert "GitHub polling source" in manual


def test_settings_page_offers_polling_sources(seeded):
    with TestClient(create_app()) as client:
        html = client.get("/").text
        js = client.get("/assets/js/settings-webhooks.js").text
        automations = client.get("/assets/js/automations.js").text
    assert "editWebhookSource(null, { kind: 'github' })" in html
    assert "function editGithubSource" in js and "/poll`" in js
    assert "githubPollStateHtml(source)" in automations
