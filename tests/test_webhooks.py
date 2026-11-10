"""Webhook 触发自动化:全局来源、验签、扇出、事件文件、排队,以及只有 /hooks 的专用端口。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from missioncrew.api import create_app
from missioncrew.api import webhooks as webhook_api
from missioncrew.collab import automations as automation_mod
from missioncrew.collab.automations import (AUTOMATION_RUNS_RETENTION_SECONDS,
                                           AutomationService, save_automation)
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.webhooks import (WebhookRejected, delete_webhook_source,
                                         new_webhook_secret, save_webhook_source,
                                         verify_webhook, webhook_event)
from missioncrew.core.models import Project
from missioncrew.core.store import Store


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _standard(key: bytes, msg_id: str, timestamp: int, body: bytes) -> str:
    """Standard Webhooks 的 `v1,<base64>` 签名(签 `<id>.<时间戳>.<正文>`)。"""
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _wait_runs(store, automation_id, count, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = store.list_automation_runs(automation_id, 50)
        if len(runs) >= count and all(run["status"] != "running" for run in runs):
            return list(reversed(runs))
        time.sleep(0.05)
    raise AssertionError("automation runs did not finish in time")


def _wait_idle(service, timeout=5.0):
    """运行记录先落库、随后才解除运行中标记;断言排队结果前等它真正空闲。"""
    deadline = time.time() + timeout
    while service.running_ids() and time.time() < deadline:
        time.sleep(0.02)
    assert service.running_ids() == set()


@pytest.fixture()
def hook(seeded):
    """全局来源 gh + 订阅它的脚本 + 只含 webhook 路由的应用(不经主服务)。"""
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    source, _ = save_webhook_source(seeded, id="gh", name="GitHub org")
    automation, _ = save_automation(
        seeded, "webshop", id="on-issue", webhook_source="gh",
        script=('echo "trigger=$MISSIONCREW_TRIGGER source=$MISSIONCREW_WEBHOOK_SOURCE '
                'provider=$MISSIONCREW_WEBHOOK_PROVIDER '
                'event=$MISSIONCREW_WEBHOOK_EVENT delivery=$MISSIONCREW_WEBHOOK_DELIVERY"\n'
                'cat "$MISSIONCREW_WEBHOOK_EVENT_FILE"\n'))
    ctx = SimpleNamespace(store=seeded, automations=service)
    client = StarletteClient(webhook_api.create_webhook_app(ctx))

    def post(body: bytes, path: str = "/hooks/gh", secret: str = "", **headers):
        return client.post(path, content=body, headers={
            "X-Hub-Signature-256": _sign(secret or source.secret, body), **headers})

    return SimpleNamespace(store=seeded, service=service, automation=automation,
                           source=source, secret=source.secret, client=client, post=post)


# ---- 验签与事件整理 ----

def test_verify_accepts_github_gitcode_and_token_styles():
    body = b'{"action":"opened"}'
    secret = "s3cret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    assert verify_webhook(secret, {"x-hub-signature-256": _sign(secret, body)}, body) == "hmac"
    # GitCode 的摘要可能是 base64
    gitcode = "sha256=" + base64.b64encode(digest).decode()
    assert verify_webhook(secret, {"x-gitcode-signature-256": gitcode}, body) == "hmac"
    for headers in ({"x-gitcode-token": secret}, {"x-gitlab-token": secret},
                    {"authorization": f"Bearer {secret}"}):
        assert verify_webhook(secret, headers, body) == "token"

    for headers, reason in (
            ({}, "missing signature"),
            ({"x-hub-signature-256": _sign("other", body)}, "signature mismatch"),
            ({"x-hub-signature-256": "sha256=zz"}, "signature mismatch"),
            ({"x-gitcode-token": "wrong"}, "token mismatch"),
            # 多种凭据都没验过时,原因按出现顺序列出
            ({"x-hub-signature-256": _sign("other", body), "authorization": "Bearer wrong"},
             "signature mismatch; token mismatch")):
        with pytest.raises(WebhookRejected) as exc:
            verify_webhook(secret, headers, body)
        assert (exc.value.status, exc.value.message) == (401, reason)


def test_relayed_delivery_passes_with_any_matching_credential():
    """转发服务原样保留上游平台的签名(用上游的密钥签,这里验不过),同时用本脚本的
    密钥另签或带密码头:任一凭据验过就接受,不按第一个签名头定论。"""
    body = b'{"action":"opened"}'
    secret, now = new_webhook_secret(), 1_760_000_000
    upstream = {"x-github-event": "pull_request",
                "x-hub-signature-256": _sign("github-app-secret", body)}
    key = base64.b64decode(secret.removeprefix("whsec_"))
    relay = {"webhook-id": "msg_1", "webhook-timestamp": str(now),
             "webhook-signature": _standard(key, "msg_1", now, body)}
    assert verify_webhook(secret, {**upstream, **relay}, body, now=now) == "standard"
    assert verify_webhook(secret, {**upstream, "authorization": f"Bearer {secret}"},
                          body, now=now) == "token"
    # 只有上游签名和本平台不认识的私有签名头时仍然拒收
    with pytest.raises(WebhookRejected) as exc:
        verify_webhook(secret, {**upstream, "x-sdbot-signature-256": _sign(secret, body)},
                       body, now=now)
    assert exc.value.message == "signature mismatch"


def test_standard_webhooks_signature():
    body = b'{"type":"invoice.paid"}'
    now = 1_760_000_000
    secret = new_webhook_secret()
    key = base64.b64decode(secret.removeprefix("whsec_"))
    assert secret.startswith("whsec_") and len(key) == 32

    def headers(signature, timestamp=now, prefix="webhook"):
        return {f"{prefix}-id": "msg_2", f"{prefix}-timestamp": str(timestamp),
                f"{prefix}-signature": signature}

    good = _standard(key, "msg_2", now, body)
    assert verify_webhook(secret, headers(good), body, now=now) == "standard"
    assert verify_webhook(secret, headers(good, prefix="svix"), body, now=now) == "standard"
    # 密钥轮换期间签名头带多个签名,任一匹配即可;未知版本忽略
    rotating = f"v1a,xyz {_standard(b'old-key', 'msg_2', now, body)} {good}"
    assert verify_webhook(secret, headers(rotating), body, now=now) == "standard"
    # 非 whsec_ 的旧密钥按原文字节签名(签名库的 raw 格式)
    legacy = "legacy-token-secret"
    raw = _standard(legacy.encode(), "msg_2", now, body)
    assert verify_webhook(legacy, headers(raw), body, now=now) == "standard"
    # GitHub 风格签名与密码方式把 whsec_ 密钥整串当作原文
    assert verify_webhook(secret, {"x-hub-signature-256": _sign(secret, body)}, body) == "hmac"

    for bad, reason in (
            (headers(good, timestamp=now - 301), "timestamp outside tolerance"),
            (headers(good, timestamp="soon"), "invalid timestamp"),
            (headers(_standard(key, "msg_2", now, b"{}")), "signature mismatch"),
            (headers("v1,not-base64!"), "signature mismatch")):
        with pytest.raises(WebhookRejected) as exc:
            verify_webhook(secret, bad, body, now=now)
        assert (exc.value.status, exc.value.message) == (401, reason)


def test_webhook_event_parses_json_and_form_and_strips_secret_headers():
    headers = {"content-type": "application/json", "x-github-event": "issues",
               "x-github-delivery": "d-1", "authorization": "Bearer x",
               "x-gitcode-token": "y", "user-agent": "GitHub-Hookshot"}
    event = webhook_event(headers, b'{"action":"opened"}')
    assert (event["provider"], event["event"], event["delivery"]) == ("github", "issues", "d-1")
    assert event["payload"] == {"action": "opened"} and event["body"] is None
    assert "authorization" not in event["headers"] and "x-gitcode-token" not in event["headers"]
    assert event["headers"]["user-agent"] == "GitHub-Hookshot"

    form = webhook_event({"content-type": "application/x-www-form-urlencoded",
                          "x-gitcode-event": "Issue Hook"},
                         b"payload=%7B%22a%22%3A1%7D")
    assert (form["provider"], form["payload"]) == ("gitcode", {"a": 1})
    plain = webhook_event({"content-type": "text/plain"}, b"hello")
    assert plain["payload"] is None and plain["body"] == "hello" and plain["provider"] == ""
    with pytest.raises(WebhookRejected) as exc:
        webhook_event({"content-type": "application/json"}, b"{bad")
    assert exc.value.status == 400


# ---- webhook 应用:投递 → 运行 ----

def test_signed_delivery_runs_script_with_event_file(hook):
    body = json.dumps({"action": "labeled", "issue": {"number": 7}}).encode()
    response = hook.post(body, **{"Content-Type": "application/json",
                                  "X-GitHub-Event": "issues", "X-GitHub-Delivery": "abc-1"})
    assert response.status_code == 202, response.text
    [run_id] = response.json()["run_ids"]
    assert response.json()["queued"] == 0
    [run] = _wait_runs(hook.store, hook.automation.id, 1)
    assert run["id"] == run_id and run["status"] == "succeeded", run
    assert (run["trigger"], run["trigger_detail"]) == ("webhook", "gh: github issues abc-1")
    assert ("trigger=webhook source=gh provider=github event=issues delivery=abc-1"
            in run["stdout"])
    event = json.loads(run["stdout"].split("\n", 1)[1])
    assert event["source"] == "gh" and event["payload"]["issue"]["number"] == 7
    assert "x-hub-signature-256" in event["headers"]   # 签名不是密钥,保留给脚本
    # 事件文件只在运行期间存在
    workdir = automation_mod.automation_workdir(hook.automation)
    assert not (workdir / automation_mod.WEBHOOK_EVENT_FILE).exists()


def test_one_source_fans_out_to_every_subscriber_across_projects(hook):
    """多个脚本(含其他项目的)订阅同一来源:一次投递各跑一次,各自拿到同一事件。"""
    hook.store.put_project(Project(id="ops", name="Ops"))
    other, _ = save_automation(hook.store, "ops", id="notify", webhook_source="gh",
                               script='echo "ops $MISSIONCREW_WEBHOOK_DELIVERY"')
    save_automation(hook.store, "webshop", id="unrelated", script="echo no")
    response = hook.post(b"{}", **{"X-GitHub-Event": "push", "X-GitHub-Delivery": "fan-1"})
    assert response.status_code == 202 and len(response.json()["run_ids"]) == 2
    [mine] = _wait_runs(hook.store, hook.automation.id, 1)
    [theirs] = _wait_runs(hook.store, other.id, 1)
    assert "delivery=fan-1" in mine["stdout"] and theirs["stdout"].strip() == "ops fan-1"
    assert hook.store.list_automation_runs("webshop:unrelated") == []
    accepted = next(row for row in hook.store.list_audit(None, 20)
                    if row["action"] == "webhook_delivery_accepted")
    assert "source=gh" in accepted["detail"] and "ops:notify=#" in accepted["detail"]


def test_source_without_subscribers_accepts_and_runs_nothing(hook):
    save_automation(hook.store, "webshop", id="on-issue", webhook_source="")
    response = hook.post(b"{}")
    assert response.status_code == 202
    assert response.json() == {"ok": True, "run_ids": [], "queued": 0}
    assert hook.store.list_automation_runs(hook.automation.id) == []


def test_relayed_github_delivery_is_accepted_by_relay_signature(hook):
    """转发服务保留 GitHub 原签名与事件头,用 Standard Webhooks 另签:接受并按 GitHub 事件交给脚本。"""
    body = json.dumps({"action": "opened", "number": 9}).encode()
    timestamp = int(time.time())
    key = base64.b64decode(hook.secret.removeprefix("whsec_"))
    response = hook.client.post("/hooks/gh", content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "gh-9", "X-Hub-Signature-256": _sign("github-app-secret", body),
        "webhook-id": "relay-9", "webhook-timestamp": str(timestamp),
        "webhook-signature": _standard(key, "relay-9", timestamp, body)})
    assert response.status_code == 202, response.text
    [run] = _wait_runs(hook.store, hook.automation.id, 1)
    assert run["trigger_detail"] == "gh: github pull_request gh-9"
    assert any(row["action"] == "webhook_delivery_accepted" and "auth=standard" in row["detail"]
               for row in hook.store.list_audit(None, 20))


def test_ping_and_rejections_do_not_run(hook):
    body = b"{}"
    ping = hook.post(body, **{"Content-Type": "application/json", "X-GitHub-Event": "ping"})
    assert ping.status_code == 200 and ping.json() == {"ok": True, "pong": True}

    bad = hook.post(body, secret="wrong")
    assert bad.status_code == 401 and bad.json()["error"] == "signature mismatch"
    assert any(row["action"] == "webhook_delivery_rejected" and "source=gh" in row["detail"]
               for row in hook.store.list_audit(None, 20))

    # 不存在的来源、停用的来源、非法路径一律 404,不区分原因
    save_webhook_source(hook.store, id="paused", enabled=False)
    for path in ("/hooks/missing", "/hooks/paused", "/hooks/gh/extra", "/hooks/bad%20id",
                 "/hooks/webshop/on-issue"):
        assert hook.post(body, path=path).status_code == 404, path
    # 端口上没有其他东西:其他路径 404,其他方法 405
    assert hook.client.get("/hooks/gh").status_code == 405
    for path in ("/", "/api/overview", "/assets/js/api.js", "/docs", "/openapi.json"):
        assert hook.client.get(path).status_code == 404, path
    assert hook.store.list_automation_runs(hook.automation.id) == []


def test_oversized_body_is_rejected(hook, monkeypatch):
    monkeypatch.setattr(webhook_api, "WEBHOOK_MAX_BODY", 10)
    assert hook.post(b"x" * 11).status_code == 413


def test_deliveries_queue_while_running_and_run_in_order(hook, monkeypatch):
    save_automation(hook.store, "webshop", id="on-issue",
                    script='sleep 0.4; echo "$MISSIONCREW_WEBHOOK_DELIVERY"')
    monkeypatch.setattr(automation_mod, "WEBHOOK_QUEUE_LIMIT", 2)

    def deliver(delivery):
        body = json.dumps({"n": delivery}).encode()
        return hook.post(body, **{"Content-Type": "application/json",
                                  "X-GitHub-Event": "push", "X-GitHub-Delivery": delivery})

    first = deliver("d1")
    assert first.status_code == 202 and first.json()["run_ids"]
    assert [deliver(d).json() for d in ("d2", "d3")] == [
        {"ok": True, "run_ids": [], "queued": 1}] * 2
    # 唯一的订阅者排满时回 503,让发送方稍后重试
    full = deliver("d4")
    assert full.status_code == 503
    # cron/手动撞上运行中仍然直接拒绝,不排队
    with pytest.raises(ValueError):
        hook.service.trigger(hook.store.get_automation(hook.automation.id))

    runs = _wait_runs(hook.store, hook.automation.id, 3)
    assert [run["stdout"].strip() for run in runs] == ["d1", "d2", "d3"]
    _wait_idle(hook.service)


def test_full_queue_of_one_subscriber_only_drops_its_copy(hook, monkeypatch):
    save_automation(hook.store, "webshop", id="on-issue", script="sleep 0.5")
    fast, _ = save_automation(hook.store, "webshop", id="fast", webhook_source="gh",
                              script="true")
    monkeypatch.setattr(automation_mod, "WEBHOOK_QUEUE_LIMIT", 0)
    assert len(hook.post(b"{}").json()["run_ids"]) == 2
    _wait_runs(hook.store, fast.id, 1)
    # on-issue 仍在运行且排队上限为 0:它的这份丢弃,fast 照常运行,整体仍是 202
    second = hook.post(b"{}")
    assert second.status_code == 202, second.text
    assert second.json()["dropped"] == 1 and len(second.json()["run_ids"]) == 1
    assert any(row["action"] == "automation_webhook_dropped"
               and "automation=webshop:on-issue" in row["detail"]
               for row in hook.store.list_audit(None, 20))
    _wait_runs(hook.store, fast.id, 2)
    _wait_idle(hook.service)


def test_queued_delivery_dropped_after_unsubscribing_or_source_disabled(hook):
    save_automation(hook.store, "webshop", id="on-issue", script="sleep 0.4")
    assert hook.post(b"{}").status_code == 202
    assert hook.post(b"{}").json() == {"ok": True, "run_ids": [], "queued": 1}
    save_automation(hook.store, "webshop", id="on-issue", webhook_source="")
    runs = _wait_runs(hook.store, hook.automation.id, 1)
    _wait_idle(hook.service)
    assert len(hook.store.list_automation_runs(hook.automation.id)) == len(runs) == 1
    assert any(row["action"] == "automation_webhook_dropped" and "不再订阅" in row["detail"]
               for row in hook.store.list_audit(None, 20))

    save_automation(hook.store, "webshop", id="on-issue", webhook_source="gh")
    assert hook.post(b"{}").status_code == 202
    assert hook.post(b"{}").json()["queued"] == 1
    save_webhook_source(hook.store, id="gh", enabled=False)
    _wait_runs(hook.store, hook.automation.id, 2)
    _wait_idle(hook.service)
    assert len(hook.store.list_automation_runs(hook.automation.id)) == 2
    assert any(row["action"] == "automation_webhook_dropped" and "停用" in row["detail"]
               for row in hook.store.list_audit(None, 20))


def test_manual_and_cron_runs_see_their_trigger(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    automation, _ = save_automation(seeded, "webshop", id="who", script=(
        'echo "$MISSIONCREW_TRIGGER event_file=${MISSIONCREW_WEBHOOK_EVENT_FILE:-none}"'))
    service.trigger(automation, trigger="cron")
    [cron] = _wait_runs(seeded, automation.id, 1)
    service.trigger(automation)
    manual = _wait_runs(seeded, automation.id, 2)[1]
    assert cron["stdout"].strip() == "cron event_file=none"
    assert manual["stdout"].strip() == "manual event_file=none"


def test_run_records_are_pruned_by_age(seeded):
    noisy, _ = save_automation(seeded, "webshop", id="noisy", script="true")
    rare, _ = save_automation(seeded, "webshop", id="rare", script="true")
    ids = [seeded.start_automation_run(noisy.id, "webshop", "webhook") for _ in range(5)]
    rare_id = seeded.start_automation_run(rare.id, "webshop", "cron")
    old = time.time() - AUTOMATION_RUNS_RETENTION_SECONDS - 60
    seeded._execute("UPDATE automation_runs SET started_at=? WHERE id IN (?,?,?)",
                    (old, ids[0], ids[1], rare_id))
    AutomationService(seeded, None).prune_runs()
    assert [run["id"] for run in seeded.list_automation_runs(noisy.id)] == ids[:1:-1]
    # 过了保留期但仍是该脚本最近一条,保留
    assert [run["id"] for run in seeded.list_automation_runs(rare.id)] == [rare_id]


# ---- 来源维护 ----

def test_source_paths_are_validated_unique_and_default_to_id(seeded):
    source, created = save_webhook_source(seeded, id="gitcode")
    assert created and source.path == "gitcode" and source.secret.startswith("whsec_")
    secret = source.secret
    # 更新不换密钥;路径可多段,前缀 /hooks/ 与首尾斜杠被去掉
    source, created = save_webhook_source(seeded, id="gitcode", path="/hooks/team/gitcode/")
    assert not created and source.path == "team/gitcode" and source.secret == secret
    for bad in ("a b", "a//b", "a/b/c/d/e", "x" * 201):
        with pytest.raises(ValueError):
            save_webhook_source(seeded, id="gitcode", path=bad)
    with pytest.raises(ValueError, match="已被来源 gitcode 使用"):
        save_webhook_source(seeded, id="other", path="team/gitcode")
    with pytest.raises(ValueError):
        save_webhook_source(seeded, id="bad id")
    assert save_webhook_source(seeded, id="gitcode", path="")[0].path == "gitcode"


def test_automation_must_reference_existing_source_and_source_in_use_cannot_be_deleted(seeded):
    with pytest.raises(ValueError, match="webhook 来源不存在"):
        save_automation(seeded, "webshop", id="x", script="true", webhook_source="nope")
    save_webhook_source(seeded, id="gh")
    save_automation(seeded, "webshop", id="x", script="true", webhook_source="gh")
    with pytest.raises(ValueError, match="webshop:x"):
        delete_webhook_source(seeded, "gh")
    save_automation(seeded, "webshop", id="x", webhook_source="")
    delete_webhook_source(seeded, "gh")
    assert seeded.get_webhook_source("gh") is None
    with pytest.raises(LookupError):
        delete_webhook_source(seeded, "gh")


def test_legacy_automation_webhook_migrates_to_source_keeping_path_and_secret(tmp_path):
    """旧版脚本自带 webhook 与密钥:迁成全局来源,沿用 `<项目>/<脚本>` 路径与原密钥。"""
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    legacy = {"id": "webshop:hook", "project_id": "webshop", "name": "PR hook",
              "script": "true", "webhook": True, "webhook_secret": "old-secret"}
    off = {"id": "ops:hook", "project_id": "ops", "script": "true",
           "webhook": False, "webhook_secret": "stale"}
    clash = {"id": "ops:pr", "project_id": "ops", "script": "true",
             "webhook": True, "webhook_secret": "ops-secret"}
    store._put("webhook_sources", "pr", {"id": "pr", "path": "pr", "secret": "x"})
    for item in (legacy, off, clash):
        store._put("automations", item["id"], item)
    store = Store(path)   # 重新打开即迁移

    migrated = store.get_automation("webshop:hook")
    assert migrated.webhook_source == "hook"
    source = store.get_webhook_source("hook")
    assert (source.path, source.secret, source.name) == ("webshop/hook", "old-secret", "PR hook")
    assert store.get_automation("ops:hook").webhook_source == ""
    # 来源 id 已被占用时加项目前缀
    assert store.get_automation("ops:pr").webhook_source == "ops-pr"
    assert store.get_webhook_source("ops-pr").path == "ops/pr"
    raw = json.loads(store._query("SELECT data FROM automations WHERE id='ops:hook'")[0]["data"])
    assert "webhook" not in raw and "webhook_secret" not in raw
    assert len(store.list_webhook_sources()) == 3
    # 幂等:再打开不会重复迁移
    reopened = Store(path)
    assert sorted((item.id, item.path) for item in reopened.list_webhook_sources()) == [
        ("hook", "webshop/hook"), ("ops-pr", "ops/pr"), ("pr", "pr")]


def test_migrated_legacy_path_still_accepts_deliveries(seeded):
    seeded._put("automations", "webshop:old", {
        "id": "webshop:old", "project_id": "webshop", "script": "echo legacy",
        "webhook": True, "webhook_secret": "legacy-secret"})
    store = Store(seeded.path)
    service = AutomationService(store, ChatEngine(store, max_workers=2).agent_tools)
    client = StarletteClient(webhook_api.create_webhook_app(
        SimpleNamespace(store=store, automations=service)))
    response = client.post("/hooks/webshop/old", content=b"{}", headers={
        "X-Hub-Signature-256": _sign("legacy-secret", b"{}")})
    assert response.status_code == 202, response.text
    [run] = _wait_runs(store, "webshop:old", 1)
    assert run["stdout"].strip() == "legacy"


# ---- 密钥只给人看 ----

def test_source_api_and_secret_only_in_secret_endpoint(seeded):
    with TestClient(create_app()) as client:
        created = client.post("/api/webhook-sources", json={"id": "gh", "name": "GitHub"})
        assert created.status_code == 200, created.text
        data = created.json()
        assert "secret" not in data and data["webhook_path"] == "/hooks/gh"
        assert data["enabled"] is True and data["automations"] == []
        assert client.post("/api/webhook-sources", json={"id": "bad id"}).status_code == 400

        saved = client.post("/api/projects/webshop/automations", json={
            "id": "hooked", "script": "echo ok", "webhook_source": "gh"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["webhook_source"] == "gh"
        assert saved.json()["webhook_path"] == "/hooks/gh"
        assert client.post("/api/projects/webshop/automations", json={
            "id": "hooked", "webhook_source": "missing"}).status_code == 400

        listed = client.get("/api/webhook-sources").json()
        assert listed["listener"] == {"enabled": False}   # 测试不起专用端口
        [row] = listed["sources"]
        assert row["automations"] == [
            {"id": "webshop:hooked", "project_id": "webshop", "name": "hooked"}]
        everything = json.dumps([
            listed, client.get("/api/projects/webshop/automations").json(),
            client.get("/api/projects/webshop/overview").json(),
            client.get("/api/overview").json()], ensure_ascii=False)
        secret = client.get("/api/webhook-sources/gh/secret").json()["secret"]
        assert len(secret) >= 32 and secret not in everything

        rotated = client.post("/api/webhook-sources/gh/secret").json()["secret"]
        assert rotated != secret and seeded.get_webhook_source("gh").secret == rotated
        assert client.get("/api/webhook-sources/missing/secret").status_code == 404

        # 仍被订阅时拒绝删除;取消订阅后可以删除
        assert client.delete("/api/webhook-sources/gh").status_code == 409
        client.post("/api/projects/webshop/automations",
                    json={"id": "hooked", "webhook_source": ""})
        assert client.delete("/api/webhook-sources/gh").json() == {"deleted": True}
        assert client.delete("/api/webhook-sources/gh").status_code == 404


def test_global_settings_page_manages_webhook_sources(seeded):
    with TestClient(create_app()) as client:
        html = client.get("/").text
        js = client.get("/assets/js/settings-webhooks.js").text
        settings = client.get("/assets/js/settings-runtime.js").text
    assert 'id="webhook-source-table"' in html
    assert html.index('src="/assets/js/settings-webhooks.js"') < html.index(
        'src="/assets/js/main.js"')
    assert "renderWebhookSources()" in settings
    # 密钥只在打开来源编辑框时经 /secret 端点读取
    assert "/secret`" in js and "function editWebhookSource" in js


def _lead_call(store, chat):
    """在 general 起一个主控活动 Run,返回按该身份调用 Agent Tool 动作的函数。"""
    channel = store.get_channel("general")
    root_id = store.add_message(channel.id, "human", "human", "@lead 执行", ["lead"])
    run_id = store.add_chat_run(channel.id, "lead", root_id, root_id, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, store.get_role("webshop", "lead"),
                            store.get_backend("std-1"), root_id, run_id=run_id)
    token = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip()
    identity = chat.agent_tools.authenticate(token)
    return lambda action, arguments: chat.agent_tools.execute(
        identity, action, arguments, run_id,
        f"{action.replace('.', '-')}-{time.time_ns()}")


def test_orchestrator_subscribes_to_source_without_seeing_secret(seeded):
    source, _ = save_webhook_source(seeded, id="gh", name="GitHub org", path="org/github")
    call = _lead_call(seeded, ChatEngine(seeded, max_workers=2))
    result = call("automation.save", {
        "id": "gh-sync", "script": "echo ok", "cron": "*/30 * * * *", "webhook_source": "gh"})
    assert result["automation"]["webhook_path"] == "/hooks/org/github"
    assert "cron `*/30 * * * *` + webhook 来源 gh" in result["summary"]
    assert seeded.get_automation("webshop:gh-sync").webhook_source == "gh"
    listed = call("automation.list", {})
    row = next(item for item in listed["automations"] if item["id"] == "gh-sync")
    assert row["webhook_source"] == "gh" and row["webhook_path"] == "/hooks/org/github"
    assert listed["webhook_sources"] == [{
        "id": "gh", "name": "GitHub org", "description": "", "enabled": True,
        "kind": "webhook", "webhook_path": "/hooks/org/github"}]
    assert source.secret not in json.dumps(listed) + json.dumps(result)
    # 旧参数 webhook 已不再接受;不存在的来源返回参数错误
    for arguments in ({"id": "gh-sync", "webhook": True},
                      {"id": "gh-sync", "webhook_source": "missing"}):
        with pytest.raises(Exception) as exc:
            call("automation.save", arguments)
        assert getattr(exc.value, "code", "") == "invalid_arguments", exc.value


# ---- 投递记录 ----

def _issue(hook, delivery, action="opened", repo="acme/webshop", event="issues"):
    body = json.dumps({"action": action, "issue": {"number": 7},
                       "repository": {"full_name": repo}}).encode()
    return hook.post(body, **{"Content-Type": "application/json",
                              "X-GitHub-Event": event, "X-GitHub-Delivery": delivery})


def test_every_verified_delivery_is_recorded_with_its_outcome(hook):
    [run_id] = _issue(hook, "d-1").json()["run_ids"]
    _wait_runs(hook.store, hook.automation.id, 1)
    _wait_idle(hook.service)
    hook.post(b"{}", **{"Content-Type": "application/json", "X-GitHub-Event": "ping"})
    assert hook.post(b"{}", secret="wrong").status_code == 401       # 拒收的不记
    save_automation(hook.store, "webshop", id="on-issue", webhook_source="")
    _issue(hook, "d-2", action="closed")                              # 没有订阅者也记

    closed, ping, opened = hook.store.list_webhook_deliveries("gh")
    assert (opened["event"], opened["action"], opened["repository"], opened["delivery"]) == (
        "issues", "opened", "acme/webshop", "d-1")
    assert opened["transport"] == "webhook" and opened["auth"] == "hmac"
    assert opened["outcomes"] == [
        {"automation": "webshop:on-issue", "result": "run", "run_id": run_id}]
    assert ping["event"] == "ping" and ping["outcomes"] == []
    assert closed["action"] == "closed" and closed["outcomes"] == []
    assert "data" not in opened                                       # 列表不带正文

    detail = hook.store.get_webhook_delivery(opened["id"])
    assert detail["event_data"]["payload"]["issue"]["number"] == 7
    assert detail["event_data"]["source"] == "gh"
    assert hook.secret not in json.dumps(detail)


def test_delivery_history_filters_pages_and_bounds_what_it_keeps(hook, monkeypatch):
    from missioncrew.collab import webhooks as webhooks_mod
    from missioncrew.collab.credentials import save_credential

    save_automation(hook.store, "webshop", id="on-issue", webhook_source="")   # 只看记录
    token = "ghp_" + "a1B2c3D4e5" * 4
    save_credential(hook.store, project_id="", name="GITHUB_TOKEN", value=token)
    _issue(hook, "a-1")
    _issue(hook, "a-2", action="closed")
    _issue(hook, "a-3", repo="Acme/API", event="pull_request", action="merged")
    leaked = json.dumps({"action": "created", "comment": {"body": f"token is {token}"}}).encode()
    hook.post(leaked, **{"Content-Type": "application/json", "X-GitHub-Event": "issue_comment"})

    listed = hook.store.list_webhook_deliveries
    assert [row["delivery"] for row in listed("gh", event="issues")] == ["a-2", "a-1"]
    assert [row["delivery"] for row in listed("gh", action="merged")] == ["a-3"]
    assert [row["delivery"] for row in listed("gh", repository="acme/api")] == ["a-3"]
    newest = listed("gh", limit=2)
    assert len(newest) == 2
    assert [row["delivery"] for row in listed("gh", before_id=newest[-1]["id"])] == ["a-2", "a-1"]
    # 正文里出现已保存的凭据时,落库前同样脱敏
    comment = hook.store.get_webhook_delivery(newest[0]["id"])["event_data"]
    assert comment["payload"]["comment"]["body"] == "token is [credential:GITHUB_TOKEN]"

    # 过大的事件只留摘要与请求头
    monkeypatch.setattr(webhooks_mod, "DELIVERY_MAX_BYTES", 600)
    big = json.dumps({"action": "opened", "repository": {"full_name": "acme/webshop"},
                      "issue": {"body": "x" * 2000}}).encode()
    hook.post(big, **{"Content-Type": "application/json", "X-GitHub-Event": "issues"})
    [row] = listed("gh", limit=1)
    stored = hook.store.get_webhook_delivery(row["id"])
    assert row["action"] == "opened" and row["size"] > 2000
    assert stored["event_data"]["payload"] is None and stored["event_data"]["payload_omitted"]

    # 超出保留期的清掉;删除来源连同记录一起删
    hook.store._execute("UPDATE webhook_deliveries SET received_at=? WHERE delivery='a-1'",
                        (time.time() - webhooks_mod.DELIVERY_RETENTION_SECONDS - 60,))
    webhooks_mod.prune_deliveries(hook.store)
    assert "a-1" not in [item["delivery"] for item in listed("gh")]
    delete_webhook_source(hook.store, "gh")
    assert hook.store.count_webhook_deliveries("gh") == 0


def test_delivery_history_api_and_page(hook):
    _issue(hook, "api-1")
    _wait_runs(hook.store, hook.automation.id, 1)
    with TestClient(create_app()) as client:
        [source] = client.get("/api/webhook-sources").json()["sources"]
        assert source["delivery_count"] == 1
        listed = client.get("/api/webhook-sources/gh/deliveries?event=issues").json()
        assert listed["total"] == 1
        [row] = listed["deliveries"]
        assert row["delivery"] == "api-1" and row["outcomes"][0]["result"] == "run"
        assert client.get("/api/webhook-sources/gh/deliveries?event=push").json()[
            "deliveries"] == []
        detail = client.get(f"/api/webhook-sources/gh/deliveries/{row['id']}").json()
        assert detail["event_file"]["payload"]["issue"]["number"] == 7
        assert hook.secret not in json.dumps([listed, detail])
        assert client.get("/api/webhook-sources/gh/deliveries/999999").status_code == 404
        assert client.get("/api/webhook-sources/missing/deliveries").status_code == 404
        # 记录属于它的来源,不能从别的来源读到
        client.post("/api/webhook-sources", json={"id": "other"})
        assert client.get(f"/api/webhook-sources/other/deliveries/{row['id']}").status_code == 404
        js = client.get("/assets/js/settings-webhooks.js").text
        automations = client.get("/assets/js/automations.js").text
    assert "function openWebhookDeliveries" in js and "/deliveries?" in js
    assert "openWebhookDeliveries(this.dataset.id)" in automations


def test_orchestrator_reads_delivery_history_to_work_out_filters(hook):
    _issue(hook, "h-1")
    _issue(hook, "h-2", event="pull_request", action="merged")
    _wait_runs(hook.store, hook.automation.id, 2)
    chat = ChatEngine(hook.store, max_workers=2)
    call = _lead_call(hook.store, chat)
    listed = call("webhook.history", {"source": "gh"})
    assert listed["total"] == 2 and "共保留 2 条" in listed["summary"]
    assert [(row["event"], row["action"]) for row in listed["deliveries"]] == [
        ("pull_request", "merged"), ("issues", "opened")]
    [merged] = call("webhook.history", {"source": "gh", "action": "merged"})["deliveries"]
    one = call("webhook.history", {"source": "gh", "id": merged["id"]})
    # 事件全文与脚本读到的事件文件同形,可以直接拿来试过滤条件
    assert one["event_file"]["event"] == "pull_request"
    assert one["event_file"]["payload"]["repository"]["full_name"] == "acme/webshop"
    assert one["delivery"]["outcomes"][0]["automation"] == "webshop:on-issue"
    assert hook.secret not in json.dumps([listed, one])
    assert call("webhook.history", {"source": "gh", "limit": 1,
                                    "before_id": merged["id"]})["deliveries"][0]["event"] == "issues"
    for arguments, code in (({"source": "missing"}, "not_found"),
                            ({"source": "gh", "id": 999999}, "not_found"),
                            ({"source": "gh", "limit": 0}, "invalid_arguments"),
                            ({"source": "gh", "event": 5}, "invalid_arguments")):
        with pytest.raises(Exception) as exc:
            call("webhook.history", arguments)
        assert getattr(exc.value, "code", "") == code, exc.value
    # 只读查询不在频道留回执
    assert not [row for row in hook.store.recent_messages("general", 50)
                if "webhook.history" in str(row.get("content"))]


# ---- 专用端口:真实监听 ----

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_listener_serves_only_hooks_on_its_own_port(hook):
    ctx = SimpleNamespace(store=hook.store, automations=hook.service)
    listener = webhook_api.WebhookListener(ctx, "127.0.0.1", _free_port())
    listener.start()
    try:
        assert listener.status()["listening"] is True, listener.status()
        base = f"http://127.0.0.1:{listener.port}"
        body = b"{}"
        request = urllib.request.Request(
            f"{base}/hooks/gh", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": _sign(hook.secret, body)})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202 and "server" not in response.headers
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/overview", timeout=5)
        assert exc.value.code == 404
    finally:
        listener.stop()
    assert listener.status()["listening"] is False
    _wait_runs(hook.store, hook.automation.id, 1)


def test_listener_reports_port_in_use(hook):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        ctx = SimpleNamespace(store=hook.store, automations=hook.service)
        listener = webhook_api.WebhookListener(ctx, "127.0.0.1", sock.getsockname()[1])
        listener.start()
        status = listener.status()
        listener.stop()
    assert status["listening"] is False and "端口可能已被占用" in status["error"]
