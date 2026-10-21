"""Webhook 触发自动化:验签、事件文件、排队,以及只有 /hooks 的专用端口。"""
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
from missioncrew.collab.webhooks import (WebhookRejected, new_webhook_secret,
                                         verify_webhook, webhook_event)


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


@pytest.fixture()
def hook(seeded):
    """开启 webhook 的脚本 + 只含 webhook 路由的应用(不经主服务)。"""
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    automation, _ = save_automation(
        seeded, "webshop", id="on-issue", webhook=True,
        script=('echo "trigger=$MISSIONCREW_TRIGGER provider=$MISSIONCREW_WEBHOOK_PROVIDER '
                'event=$MISSIONCREW_WEBHOOK_EVENT delivery=$MISSIONCREW_WEBHOOK_DELIVERY"\n'
                'cat "$MISSIONCREW_WEBHOOK_EVENT_FILE"\n'))
    ctx = SimpleNamespace(store=seeded, automations=service)
    client = StarletteClient(webhook_api.create_webhook_app(ctx))
    return SimpleNamespace(store=seeded, service=service, automation=automation,
                           secret=automation.webhook_secret, client=client)


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
    response = hook.client.post(
        "/hooks/webshop/on-issue", content=body,
        headers={"Content-Type": "application/json", "X-GitHub-Event": "issues",
                 "X-GitHub-Delivery": "abc-1", "X-Hub-Signature-256": _sign(hook.secret, body)})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    [run] = _wait_runs(hook.store, hook.automation.id, 1)
    assert run["id"] == run_id and run["status"] == "succeeded", run
    assert (run["trigger"], run["trigger_detail"]) == ("webhook", "github issues abc-1")
    assert "trigger=webhook provider=github event=issues delivery=abc-1" in run["stdout"]
    event = json.loads(run["stdout"].split("\n", 1)[1])
    assert event["payload"]["issue"]["number"] == 7
    assert "x-hub-signature-256" in event["headers"]   # 签名不是密钥,保留给脚本
    # 事件文件只在运行期间存在
    workdir = automation_mod.automation_workdir(hook.automation)
    assert not (workdir / automation_mod.WEBHOOK_EVENT_FILE).exists()


def test_relayed_github_delivery_is_accepted_by_relay_signature(hook):
    """转发服务保留 GitHub 原签名与事件头,用 Standard Webhooks 另签:接受并按 GitHub 事件交给脚本。"""
    body = json.dumps({"action": "opened", "number": 9}).encode()
    timestamp = int(time.time())
    key = base64.b64decode(hook.secret.removeprefix("whsec_"))
    response = hook.client.post("/hooks/webshop/on-issue", content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "gh-9", "X-Hub-Signature-256": _sign("github-app-secret", body),
        "webhook-id": "relay-9", "webhook-timestamp": str(timestamp),
        "webhook-signature": _standard(key, "relay-9", timestamp, body)})
    assert response.status_code == 202, response.text
    [run] = _wait_runs(hook.store, hook.automation.id, 1)
    assert run["trigger_detail"] == "github pull_request gh-9"
    assert any(row["action"] == "automation_webhook_accepted" and "auth=standard" in row["detail"]
               for row in hook.store.list_audit(None, 20))


def test_ping_and_rejections_do_not_run(hook):
    body = b"{}"
    ping = hook.client.post("/hooks/webshop/on-issue", content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "ping",
        "X-Hub-Signature-256": _sign(hook.secret, body)})
    assert ping.status_code == 200 and ping.json() == {"ok": True, "pong": True}

    bad = hook.client.post("/hooks/webshop/on-issue", content=body, headers={
        "X-Hub-Signature-256": _sign("wrong", body)})
    assert bad.status_code == 401 and bad.json()["error"] == "signature mismatch"
    assert any(row["action"] == "automation_webhook_rejected"
               for row in hook.store.list_audit(None, 20))

    save_automation(hook.store, "webshop", id="cron-only", script="echo hi", cron="0 9 * * *")
    for path in ("/hooks/webshop/cron-only", "/hooks/webshop/missing",
                 "/hooks/other/on-issue", "/hooks/webshop/bad%20id"):
        assert hook.client.post(path, content=body, headers={
            "X-Hub-Signature-256": _sign(hook.secret, body)}).status_code == 404, path
    # 端口上没有其他东西:其他路径 404,其他方法 405
    assert hook.client.get("/hooks/webshop/on-issue").status_code == 405
    for path in ("/", "/api/overview", "/assets/js/api.js", "/docs", "/openapi.json"):
        assert hook.client.get(path).status_code == 404, path
    assert hook.store.list_automation_runs(hook.automation.id) == []


def test_oversized_body_is_rejected(hook, monkeypatch):
    monkeypatch.setattr(webhook_api, "WEBHOOK_MAX_BODY", 10)
    response = hook.client.post("/hooks/webshop/on-issue", content=b"x" * 11,
                                headers={"X-Hub-Signature-256": _sign(hook.secret, b"x" * 11)})
    assert response.status_code == 413


def test_deliveries_queue_while_running_and_run_in_order(hook, monkeypatch):
    save_automation(hook.store, "webshop", id="on-issue",
                    script='sleep 0.4; echo "$MISSIONCREW_WEBHOOK_DELIVERY"')
    monkeypatch.setattr(automation_mod, "WEBHOOK_QUEUE_LIMIT", 2)

    def deliver(delivery):
        body = json.dumps({"n": delivery}).encode()
        return hook.client.post("/hooks/webshop/on-issue", content=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "push",
            "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": _sign(hook.secret, body)})

    first = deliver("d1")
    assert first.status_code == 202 and first.json()["run_id"]
    assert [deliver(d).json() for d in ("d2", "d3")] == [{"ok": True, "queued": True}] * 2
    full = deliver("d4")
    assert full.status_code == 503
    # cron/手动撞上运行中仍然直接拒绝,不排队
    with pytest.raises(ValueError):
        hook.service.trigger(hook.store.get_automation(hook.automation.id))

    runs = _wait_runs(hook.store, hook.automation.id, 3)
    assert [run["stdout"].strip() for run in runs] == ["d1", "d2", "d3"]
    # 运行记录先落库、随后才解除运行中标记
    deadline = time.time() + 5
    while hook.service.running_ids() and time.time() < deadline:
        time.sleep(0.02)
    assert hook.service.running_ids() == set()


def test_queued_delivery_dropped_after_webhook_disabled(hook):
    save_automation(hook.store, "webshop", id="on-issue", script="sleep 0.4")
    body = b"{}"
    headers = {"X-Hub-Signature-256": _sign(hook.secret, body)}
    assert hook.client.post("/hooks/webshop/on-issue", content=body, headers=headers).status_code == 202
    assert hook.client.post("/hooks/webshop/on-issue", content=body,
                            headers=headers).json() == {"ok": True, "queued": True}
    save_automation(hook.store, "webshop", id="on-issue", webhook=False)
    runs = _wait_runs(hook.store, hook.automation.id, 1)
    time.sleep(0.3)
    assert len(hook.store.list_automation_runs(hook.automation.id)) == len(runs) == 1
    assert any(row["action"] == "automation_webhook_dropped"
               for row in hook.store.list_audit(None, 20))
    assert hook.service.running_ids() == set()


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


# ---- 密钥只给人看 ----

def test_secret_only_in_webhook_endpoint_and_rotates(seeded):
    with TestClient(create_app()) as client:
        saved = client.post("/api/projects/webshop/automations", json={
            "id": "hooked", "script": "echo ok", "webhook": True})
        assert saved.status_code == 200, saved.text
        data = saved.json()
        assert data["webhook"] is True and "webhook_secret" not in data
        assert data["webhook_path"] == "/hooks/webshop/hooked"
        listed = client.get("/api/projects/webshop/automations").json()
        assert listed["webhook_listener"] == {"enabled": False}   # 测试不起专用端口
        assert all("webhook_secret" not in item for item in listed["automations"])
        overview = client.get("/api/projects/webshop/overview").json()["automations"]
        assert all("webhook_secret" not in item for item in overview)

        config = client.get("/api/projects/webshop/automations/hooked/webhook").json()
        assert config["enabled"] and len(config["secret"]) >= 32
        rotated = client.post("/api/projects/webshop/automations/hooked/webhook/secret").json()
        assert rotated["secret"] != config["secret"]
        assert seeded.get_automation("webshop:hooked").webhook_secret == rotated["secret"]
        # 关闭后保留密钥,但不能再轮换
        client.post("/api/projects/webshop/automations", json={"id": "hooked", "webhook": False})
        assert client.post(
            "/api/projects/webshop/automations/hooked/webhook/secret").status_code == 409
        assert seeded.get_automation("webshop:hooked").webhook_secret == rotated["secret"]


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


def test_orchestrator_enables_webhook_without_seeing_secret(seeded):
    call = _lead_call(seeded, ChatEngine(seeded, max_workers=2))
    result = call("automation.save", {
        "id": "gh-sync", "script": "echo ok", "cron": "*/30 * * * *", "webhook": True})
    assert "webhook_secret" not in result["automation"]
    assert result["automation"]["webhook_path"] == "/hooks/webshop/gh-sync"
    assert "cron `*/30 * * * *` + webhook" in result["summary"]
    assert seeded.get_automation("webshop:gh-sync").webhook_secret
    listed = call("automation.list", {})
    row = next(item for item in listed["automations"] if item["id"] == "gh-sync")
    assert row["webhook"] is True and row["webhook_path"] == "/hooks/webshop/gh-sync"
    assert "webhook_secret" not in json.dumps(listed)


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
            f"{base}/hooks/webshop/on-issue", data=body, method="POST",
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
