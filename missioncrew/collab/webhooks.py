"""Webhook 触发自动化:全局 webhook 来源的维护、验签、事件整理与投递扇出。

webhook 来源(WebhookSource)是全局的投递入口:外部系统(GitHub、GitCode、GitLab
或自建服务)把事件 POST 到专用 webhook 端口的 `/hooks/<来源路径>`。每个来源有
自己的密钥,投递至少带一种用它验得过的凭据:GitHub 风格 HMAC-SHA256 签名头
(`X-Hub-Signature-256` / `X-GitCode-Signature-256`)、Standard Webhooks 签名
(`webhook-id` / `webhook-timestamp` / `webhook-signature`,svix-* 同义)或密码头
(`X-GitCode-Token` / `X-Gitlab-Token` / `Authorization: Bearer`)。验签通过的投递
整理成事件,交给所有订阅该来源的自动化脚本(各项目都可以订阅同一个来源),由
脚本自己决定处理哪些事件;平台只代答 GitHub 的 ping。HTTP 监听本身在
api/webhooks.py,这里不依赖任何 Web 框架,便于单测。

来源还有一种类型 kind=github:不接收外部投递,由 github_events.py 用 GitHub token
轮询生成同形的事件,同样经 fan_out_event 交给订阅脚本。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs, quote

from ..core.models import WebhookSource
from ..core.store import Store

# GitHub 单次投递上限 25 MiB;超出直接拒收,不读完正文
WEBHOOK_MAX_BODY = 25 * 1024 * 1024
# GitHub 风格签名头:对原始正文做 HMAC-SHA256;GitCode 的摘要可能是 hex 也可能是 base64
SIGNATURE_HEADERS = ("x-hub-signature-256", "x-gitcode-signature-256")
TOKEN_HEADERS = ("x-gitcode-token", "x-gitlab-token")
# Standard Webhooks(standardwebhooks.com)的 (id, 时间戳, 签名) 三个头;
# svix-* 是同一规范的原名(Svix 及基于它的服务仍在用)
STANDARD_HEADER_SETS = (
    ("webhook-id", "webhook-timestamp", "webhook-signature"),
    ("svix-id", "svix-timestamp", "svix-signature"),
)
# 规范建议的时间戳容差:超出即视为重放
STANDARD_TOLERANCE_SECONDS = 5 * 60
STANDARD_SECRET_PREFIX = "whsec_"
# 携带密钥的请求头不写进事件文件,脚本打印事件时不会泄露
SECRET_HEADERS = frozenset({"authorization", *TOKEN_HEADERS})
# (来源, 事件名头, 投递 id 头);按顺序识别,都没有时来源为空
PROVIDER_HEADERS = (
    ("github", "x-github-event", "x-github-delivery"),
    ("gitcode", "x-gitcode-event", "x-gitcode-delivery"),
    ("gitlab", "x-gitlab-event", "x-gitlab-event-uuid"),
)


class WebhookRejected(Exception):
    """投递被拒:status 是回给发送方的 HTTP 状态码,message 只含简短原因。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def new_webhook_secret() -> str:
    """Standard Webhooks 格式的密钥(`whsec_` + 32 字节 base64):规范的签名库可以
    直接使用;GitHub 风格签名与密码方式把整串当作密钥原文,同样适用。"""
    return STANDARD_SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()


# 来源 id 与路径段的字符集同其他平台短 id;路径最多 4 段(旧版迁移来的是 `<项目>/<脚本>`)
SOURCE_ID_RE = re.compile(r"[\w-]{1,100}")
SOURCE_PATH_RE = re.compile(r"[\w-]+(?:/[\w-]+){0,3}")
SOURCE_PATH_MAX = 200


def webhook_path(source: WebhookSource) -> str:
    """来源的投递路径(专用 webhook 端口上的 URL path)。"""
    return "/hooks/" + "/".join(quote(part, safe="") for part in source.path.split("/"))


def normalize_source_path(raw: str) -> str:
    """去掉首尾斜杠与可选的 `/hooks/` 前缀后校验;不合法抛 ValueError。"""
    path = str(raw or "").strip().strip("/")
    path = path.removeprefix("hooks/")
    if len(path) > SOURCE_PATH_MAX or not SOURCE_PATH_RE.fullmatch(path):
        raise ValueError("投递路径由 1~4 段字母、数字、下划线、连字符组成,段之间用 / 分隔")
    return path


def source_subscribers(store: Store, source_id: str) -> list:
    """订阅该来源的全部自动化脚本(跨项目),按项目与创建时间排序。"""
    return [item for item in store.list_automations()
            if item.webhook_source == source_id]


def save_webhook_source(store: Store, *, id: str, name: Optional[str] = None,
                        description: Optional[str] = None,
                        path: Optional[str] = None,
                        enabled: Optional[bool] = None,
                        kind: Optional[str] = None,
                        github: Optional[dict] = None,
                        actor: str = "human") -> tuple[WebhookSource, bool]:
    """新建或按字段合并更新来源;新建时生成密钥。返回 (source, 是否新建)。

    kind 只在新建时指定(默认 webhook),之后不可改:两类来源的投递方式不同,
    改类型等于换了一个来源。kind=github 的轮询配置经 github 传入并整份替换。
    """
    source_id = str(id or "").strip()
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError("来源 id 只能包含字母、数字、下划线、连字符")
    existing = store.get_webhook_source(source_id)
    created = existing is None
    wanted_kind = str(kind or "").strip() or (existing.kind if existing else "webhook")
    if existing is not None and wanted_kind != existing.kind:
        raise ValueError("来源类型创建后不能修改;请新建一个来源")
    source = existing or WebhookSource(id=source_id, path=source_id, kind=wanted_kind,
                                       secret=new_webhook_secret())
    if source.kind == "github":
        from .github_events import normalize_github_config   # 延迟导入,避免环形依赖
        if github is not None or created:
            source.github = normalize_github_config(store, github or {})
    elif github:
        raise ValueError("只有 GitHub 轮询来源可以带 github 配置")
    if name is not None:
        source.name = str(name).strip()
    if not source.name:
        source.name = source_id
    if description is not None:
        source.description = str(description)
    if path is not None:
        source.path = normalize_source_path(path) if str(path).strip() else source_id
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        source.enabled = enabled
    owner = store.find_webhook_source_by_path(source.path)
    if owner is not None and owner.id != source.id:
        raise ValueError(f"投递路径 /hooks/{source.path} 已被来源 {owner.id} 使用")
    store.put_webhook_source(source)
    store.audit(actor, "webhook_source_saved",
                detail=f"source={source.id} kind={source.kind} path={source.path} "
                       f"enabled={source.enabled}")
    return source, created


def rotate_webhook_source_secret(store: Store, source: WebhookSource,
                                 actor: str = "human") -> WebhookSource:
    """换一个新密钥;外部系统要同步改成新值,旧密钥签名的投递随即被拒。"""
    source.secret = new_webhook_secret()
    store.put_webhook_source(source)
    store.audit(actor, "webhook_source_secret_rotated", detail=f"source={source.id}")
    return source


def delete_webhook_source(store: Store, source_id: str,
                          actor: str = "human") -> WebhookSource:
    """删除来源;仍有脚本订阅时拒绝,避免脚本悄悄失去 webhook 触发。"""
    source = store.get_webhook_source(source_id)
    if source is None:
        raise LookupError(f"webhook 来源不存在: {source_id}")
    users = source_subscribers(store, source.id)
    if users:
        names = "、".join(item.id for item in users)
        raise ValueError(f"仍有 {len(users)} 个自动化脚本使用该来源: {names};"
                         "先在脚本里改选其他来源或取消 webhook 触发")
    store.delete_webhook_source(source.id)
    store.audit(actor, "webhook_source_deleted", detail=f"source={source.id}")
    return source


def _signature_bytes(value: str) -> Optional[bytes]:
    """解析 `sha256=<hex 或 base64>`;格式不对返回 None。"""
    text = value.strip()
    if text.lower().startswith("sha256="):
        text = text[len("sha256="):]
    try:
        if len(text) == 64:
            return bytes.fromhex(text)
        decoded = base64.b64decode(text, validate=True)
        return decoded if len(decoded) == 32 else None
    except (ValueError, binascii.Error):
        return None


def _standard_keys(secret: str) -> list[bytes]:
    """Standard Webhooks 用 `whsec_<base64>` 解码后的字节作 HMAC 密钥;其他格式的
    密钥(含旧版生成的)按原文字节,对应规范签名库的 raw 格式。"""
    keys = [secret.encode()]
    if secret.startswith(STANDARD_SECRET_PREFIX):
        try:
            keys.insert(0, base64.b64decode(
                secret[len(STANDARD_SECRET_PREFIX):], validate=True))
        except (ValueError, binascii.Error):
            pass
    return keys


def _verify_standard(secret: str, headers: dict[str, str], body: bytes,
                     now: float) -> Optional[str]:
    """验 Standard Webhooks 签名:验过返回 None,否则返回失败原因。

    签名内容是 `<id>.<时间戳>.<正文>`;签名头可带多个以空格分隔的 `v1,<base64>`
    (密钥轮换期间),任一匹配即可。
    """
    id_name, ts_name, sig_name = next(
        names for names in STANDARD_HEADER_SETS if names[2] in headers)
    timestamp = headers.get(ts_name, "").strip()
    try:
        if abs(now - int(timestamp)) > STANDARD_TOLERANCE_SECONDS:
            return "timestamp outside tolerance"
    except ValueError:
        return "invalid timestamp"
    signed = f"{headers.get(id_name, '').strip()}.{timestamp}.".encode() + body
    given = []
    for item in headers[sig_name].split():
        version, _, value = item.partition(",")
        if version == "v1":
            try:
                given.append(base64.b64decode(value, validate=True))
            except (ValueError, binascii.Error):
                continue
    for key in _standard_keys(secret):
        expected = hmac.new(key, signed, hashlib.sha256).digest()
        if any(hmac.compare_digest(item, expected) for item in given):
            return None
    return "signature mismatch"


def verify_webhook(secret: str, headers: dict[str, str], body: bytes,
                   now: Optional[float] = None) -> str:
    """带的凭据里任一种验过即通过,返回方式(hmac/standard/token);都没验过抛 401。

    不能按第一个出现的签名头定论:转发服务会原样保留上游平台的签名头(那是用
    上游的密钥签的,这里验不过),同时用本脚本的密钥另签一份或带上密码头。每种
    凭据都要知道密钥才能伪造,逐个验证不降低安全性。
    """
    if not secret:
        raise WebhookRejected(401, "webhook secret not configured")
    failures = []
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    for name in SIGNATURE_HEADERS:
        if name in headers:
            given = _signature_bytes(headers[name])
            if given is not None and hmac.compare_digest(given, expected):
                return "hmac"
            failures.append("signature mismatch")
    if any(names[2] in headers for names in STANDARD_HEADER_SETS):
        reason = _verify_standard(secret, headers, body,
                                  time.time() if now is None else now)
        if reason is None:
            return "standard"
        failures.append(reason)
    tokens = [headers[name] for name in TOKEN_HEADERS if name in headers]
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tokens.append(auth[len("bearer "):].strip())
    if tokens:
        if any(hmac.compare_digest(token.encode(), secret.encode()) for token in tokens):
            return "token"
        failures.append("token mismatch")
    # 失败原因去重后按出现顺序回给发送方,便于对照自己发了哪些凭据
    raise WebhookRejected(401, "; ".join(dict.fromkeys(failures)) or "missing signature")


def webhook_event(headers: dict[str, str], body: bytes, source: str = "") -> dict:
    """整理成交给脚本的事件:webhook 来源 id、发送平台、事件名、投递 id、去掉密钥的
    请求头与正文。

    JSON 正文与 GitHub 表单格式(`payload=<json>`)解析进 payload;其他正文
    payload 为 null,原文放 body。JSON 声明了却解析失败按坏请求拒收。
    """
    provider, event, delivery = "", "", ""
    for name, event_header, delivery_header in PROVIDER_HEADERS:
        if event_header in headers:
            provider, event = name, headers[event_header]
            delivery = headers.get(delivery_header, "")
            break
    # Standard Webhooks 的发送方没有平台事件头,投递 id 取 webhook-id
    delivery = delivery or headers.get("webhook-id") or headers.get("svix-id") or ""
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    text = body.decode("utf-8", "replace")
    payload = None
    try:
        if content_type == "application/json" or content_type.endswith("+json"):
            payload = json.loads(text) if text.strip() else None
        elif content_type == "application/x-www-form-urlencoded":
            form = parse_qs(text)
            if "payload" in form:
                payload = json.loads(form["payload"][0])
    except json.JSONDecodeError as exc:
        raise WebhookRejected(400, "invalid JSON body") from exc
    return {
        "source": source, "provider": provider, "event": event, "delivery": delivery,
        "transport": "webhook",
        "received_at": time.time(), "content_type": content_type,
        "headers": {key: value for key, value in headers.items()
                    if key not in SECRET_HEADERS},
        "payload": payload,
        "body": text if payload is None else None,
    }


def accept_webhook(store: Store, service, path: str,
                   headers: dict[str, str], body: bytes) -> tuple[int, dict]:
    """处理一次投递,返回 (HTTP 状态码, 响应 JSON);拒收抛 WebhookRejected。

    headers 的键须已转成小写。来源不存在或已停用一律 404,不区分原因。验签通过后
    扇出给所有订阅该来源的脚本:空闲的立即运行,正在运行的排队;某个脚本排队已满
    只丢弃给它的这一份(记审计),全部目标都满时回 503 让发送方稍后重试。
    """
    source = (store.find_webhook_source_by_path(path)
              if len(path) <= SOURCE_PATH_MAX and SOURCE_PATH_RE.fullmatch(path) else None)
    # 轮询来源的投递由平台自己生成,不接受外部 POST
    if source is None or not source.enabled or source.kind != "webhook":
        raise WebhookRejected(404, "not found")
    try:
        mode = verify_webhook(source.secret, headers, body)
    except WebhookRejected as exc:
        store.audit("platform", "webhook_delivery_rejected",
                    detail=f"source={source.id} reason={exc.message}")
        raise
    event = webhook_event(headers, body, source=source.id)
    if event["provider"] == "github" and event["event"] == "ping":
        return 200, {"ok": True, "pong": True}
    result = fan_out_event(store, service, source, event, auth=mode)
    if result["targets"] and result["dropped"] == result["targets"]:
        raise WebhookRejected(503, "webhook queue full, retry later")
    response = {"ok": True, "run_ids": result["run_ids"], "queued": result["queued"]}
    if result["dropped"]:
        response["dropped"] = result["dropped"]
    return 202, response


def fan_out_event(store: Store, service, source: WebhookSource, event: dict,
                  auth: str, targets: Optional[list] = None) -> dict:
    """把一份事件交给订阅该来源的脚本,返回 {targets, run_ids, queued, dropped}。

    外部投递(accept_webhook)与平台轮询生成的投递(github_events.py)共用:空闲的
    脚本立即运行,正在运行的排队,排队已满的只丢弃给它的这一份并记审计。auth 记
    进审计,说明这份投递凭什么被接受(验签方式,或轮询来源的 poll)。targets 缺省
    是该来源的全部订阅脚本;轮询来源按各脚本订阅的仓库与事件传入其中一部分。
    """
    if targets is None:
        targets = source_subscribers(store, source.id)
    run_ids: list[int] = []
    queued = dropped = 0
    outcomes = []
    for automation in targets:
        try:
            run_id = service.trigger_webhook(automation, event)
        except ValueError as exc:   # 该脚本排队已满
            dropped += 1
            outcomes.append(f"{automation.id}=dropped")
            store.audit("platform", "automation_webhook_dropped",
                        detail=f"automation={automation.id} source={source.id} "
                               f"delivery={event['delivery']} reason={exc}")
            continue
        if run_id is None:
            queued += 1
            outcomes.append(f"{automation.id}=queued")
        else:
            run_ids.append(run_id)
            outcomes.append(f"{automation.id}=#{run_id}")
    store.audit("platform", "webhook_delivery_accepted",
                detail=f"source={source.id} auth={auth} "
                       f"event={event['provider']}:{event['event']} "
                       f"delivery={event['delivery']} "
                       f"targets={','.join(outcomes) or '(none)'}")
    return {"targets": len(targets), "run_ids": run_ids, "queued": queued,
            "dropped": dropped}
