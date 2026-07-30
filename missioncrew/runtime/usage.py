"""Claude、Kimi 与 Grok 的账户限额探测和统一解析。

所有凭据只在本进程内用于本机 CLI 已登录账户的限额请求；过期 Kimi OAuth
凭据会按 CLI 的锁和原子写约定刷新。API 只返回规范化的百分比、重置时间与
非敏感账户指标，不透传上游响应或异常正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import tomllib
except ImportError:  # Python 3.10 仍可使用默认 Kimi 配置路径。
    tomllib = None

from ..core.models import Backend
from .base import (RuntimeUsageMetric, RuntimeUsageSnapshot,
                   RuntimeUsageWindow)


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_KIMI_REFRESH_GUARD = threading.Lock()


class _UsageHttpError(RuntimeError):
    def __init__(self, status: int = 0):
        super().__init__("usage request failed")
        self.status = status


def _percent(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(100.0, number)), 2)


def _ratio_percent(used: object, limit: object,
                   remaining: object = None) -> float | None:
    try:
        total = float(limit)
        if total <= 0:
            return None
        if used is not None:
            return _percent(float(used) / total * 100)
        if remaining is not None:
            return _percent((total - float(remaining)) / total * 100)
    except (TypeError, ValueError):
        pass
    return None


def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        number = float(text)
        return number / 1000 if number > 10_000_000_000 else number
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _first(mapping: dict, *keys: str) -> object:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def _request_json(url: str, token: str, timeout: int,
                  headers: dict[str, str] | None = None) -> dict:
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "MissionCrew/0.2",
        **(headers or {}),
    }
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status_code = int(exc.code)
        exc.close()
        raise _UsageHttpError(status_code) from None
    except (URLError, OSError, TimeoutError):
        raise _UsageHttpError() from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _UsageHttpError()
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _UsageHttpError() from None
    if not isinstance(payload, dict):
        raise _UsageHttpError()
    return payload


def _load_private_json(path: Path) -> dict | None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _snapshot(backend: Backend, status: str, source: str, *,
              plan: str = "",
              windows: list[RuntimeUsageWindow] | None = None,
              metrics: list[RuntimeUsageMetric] | None = None,
              message: str = "") -> RuntimeUsageSnapshot:
    return RuntimeUsageSnapshot(
        backend_id=backend.id, backend_name=backend.name,
        adapter=backend.adapter, status=status, source=source, plan=plan,
        windows=tuple(windows or ()), metrics=tuple(metrics or ()),
        message=message,
    )


def _claude_reset_timestamp(value: str, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    cleaned = re.sub(r"\s+", " ", value.strip()).upper().replace("(UTC)", "UTC")
    for pattern in (
            "%b %d, %I:%M%p UTC", "%b %d %I:%M%p UTC",
            "%b %d, %I%p UTC", "%b %d %I%p UTC"):
        try:
            parsed = datetime.strptime(
                f"{cleaned} {now.year}", f"{pattern} %Y").replace(
                    tzinfo=timezone.utc)
            # 年末显示的下一次重置可能已经落在下一年。
            if parsed.timestamp() < now.timestamp() - 30 * 86400:
                parsed = parsed.replace(year=now.year + 1)
            return parsed.timestamp()
        except ValueError:
            continue
    return _timestamp(value)


def parse_claude_usage(
        backend: Backend, text: str,
        now: datetime | None = None) -> RuntimeUsageSnapshot:
    """解析 Claude Code ``/usage`` 的稳定人类可读窗口行。"""
    windows: list[RuntimeUsageWindow] = []
    line_pattern = re.compile(
        r"^(?P<label>.+?):\s*(?P<used>\d+(?:\.\d+)?)%\s+used"
        r"(?:\s*[·•]\s*resets\s+(?P<reset>.+))?$", re.IGNORECASE)
    labels = {
        "current session": ("session", "当前会话", 5 * 60),
        "current week (all models)": ("weekly", "本周 · 全部模型", 7 * 24 * 60),
    }
    for raw_line in _ANSI_RE.sub("", text).splitlines():
        match = line_pattern.match(raw_line.strip())
        if not match:
            continue
        raw_label = match.group("label").strip()
        key, label, duration = labels.get(
            raw_label.lower(),
            (re.sub(r"\W+", "-", raw_label.lower()).strip("-"),
             raw_label.replace("Current week", "本周"), 7 * 24 * 60),
        )
        used = _percent(match.group("used"))
        if used is None:
            continue
        windows.append(RuntimeUsageWindow(
            key=key or f"window-{len(windows) + 1}", label=label,
            used_percent=used,
            resets_at=_claude_reset_timestamp(match.group("reset") or "", now),
            duration_minutes=duration,
        ))
    if not windows:
        status = "auth_required" if re.search(
            r"\b(login|authenticate|sign in)\b", text, re.IGNORECASE) else "unavailable"
        return _snapshot(
            backend, status, "claude_usage_command",
            message=("Claude Code 尚未登录" if status == "auth_required"
                     else "Claude Code 未返回可识别的 /usage 限额"),
        )
    return _snapshot(
        backend, "ok", "claude_usage_command", windows=windows)


def probe_claude_usage(
        backend: Backend, command: list[str],
        timeout: int = 15) -> RuntimeUsageSnapshot:
    try:
        result = subprocess.run(
            [*command, "-p", "/usage", "--output-format", "json"],
            cwd=str(Path.home()), env=dict(os.environ), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _snapshot(
            backend, "unavailable", "claude_usage_command",
            message="Claude Code 用量命令暂时不可用")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    text = str(payload.get("result") or "") if isinstance(payload, dict) else ""
    if not text and result.returncode:
        return _snapshot(
            backend, "unavailable", "claude_usage_command",
            message="Claude Code 用量命令执行失败")
    return parse_claude_usage(backend, text)


def _window_label(duration: int | None, unit: str, fallback: str) -> tuple[str, int | None]:
    normalized = unit.lower().rstrip("s").rsplit("_", 1)[-1]
    multipliers = {"minute": 1, "hour": 60, "day": 1440, "week": 10080}
    minutes = duration * multipliers[normalized] if duration and normalized in multipliers else None
    if minutes == 5 * 60:
        return "5 小时", minutes
    if minutes == 7 * 24 * 60:
        return "本周", minutes
    unit_labels = {"minute": "分钟", "hour": "小时", "day": "天", "week": "周"}
    if duration and normalized in unit_labels:
        return f"{duration} {unit_labels[normalized]}", minutes
    return fallback, minutes


def parse_kimi_usage(backend: Backend, payload: dict) -> RuntimeUsageSnapshot:
    windows: list[RuntimeUsageWindow] = []
    candidates = payload.get("limits")
    if not isinstance(candidates, list):
        candidates = []
    summary = payload.get("usage")
    if isinstance(summary, dict):
        candidates = [summary, *candidates]

    seen: set[str] = set()
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        detail = item.get("detail")
        detail = detail if isinstance(detail, dict) else item
        window = item.get("window")
        window = window if isinstance(window, dict) else {}
        duration_raw = _first(window, "duration", "value")
        try:
            duration = int(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration = None
        unit = str(_first(window, "timeUnit", "time_unit", "unit") or "")
        fallback = "本周" if index == 0 and isinstance(summary, dict) else f"限额 {index + 1}"
        label, duration_minutes = _window_label(duration, unit, fallback)
        percent = _percent(_first(
            detail, "usedPercent", "used_percent", "usagePercent",
            "usage_percent", "percentage"))
        if percent is None:
            percent = _ratio_percent(
                _first(detail, "used", "usage"),
                _first(detail, "limit", "total", "quota"),
                _first(detail, "remaining", "left"))
        if percent is None:
            continue
        is_summary = index == 0 and isinstance(summary, dict)
        key = ("weekly" if is_summary or duration_minutes == 10080 else
               "five-hour" if duration_minutes == 300 else
               str(_first(item, "id", "type", "name") or f"window-{index + 1}"))
        if key in seen:
            continue
        seen.add(key)
        windows.append(RuntimeUsageWindow(
            key=key, label=label, used_percent=percent,
            resets_at=_timestamp(_first(
                detail, "resetsAt", "resetAt", "reset_at", "resetTime",
                "reset_time", "endTime", "end_time")),
            duration_minutes=duration_minutes,
        ))

    metrics: list[RuntimeUsageMetric] = []
    wallet = payload.get("boosterWallet")
    if isinstance(wallet, dict):
        balance = wallet.get("balance")
        balance = balance if isinstance(balance, dict) else {}
        amount_left = _first(balance, "amountLeft", "amount_left")
        try:
            cents = round(int(amount_left) / 1_000_000)
        except (TypeError, ValueError):
            cents = 0
        if cents > 0:
            monthly = wallet.get("monthlyChargeLimit")
            monthly = monthly if isinstance(monthly, dict) else {}
            currency = str(monthly.get("currency") or "USD")
            metrics.append(RuntimeUsageMetric(
                "Booster 余额", f"{currency} {cents / 100:.2f}"))
    parallel = payload.get("parallel")
    if isinstance(parallel, dict) and parallel.get("limit") is not None:
        metrics.append(RuntimeUsageMetric("并发上限", str(parallel["limit"])))
    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    membership = user.get("membership")
    membership = membership if isinstance(membership, dict) else {}
    plan = str(membership.get("level") or payload.get("subType") or "")
    if not windows:
        return _snapshot(
            backend, "unavailable", "kimi_usage_api",
            plan=plan,
            message="Kimi 未返回可识别的限额窗口")
    return _snapshot(
        backend, "ok", "kimi_usage_api", plan=plan,
        windows=windows, metrics=metrics)


def _kimi_settings() -> tuple[Path, str, str, str]:
    home = Path(os.environ.get(
        "KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))).expanduser()
    base_url = "https://api.kimi.com/coding/v1"
    oauth_host = "https://auth.kimi.com"
    oauth_key = "oauth/kimi-code"
    explicit_oauth_key = False
    config_path = home / "config.toml"
    if tomllib is not None:
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            providers = config.get("providers") or {}
            provider = providers.get("managed:kimi-code") or {}
            if isinstance(provider, dict):
                base_url = str(provider.get("base_url") or base_url).rstrip("/")
                oauth = provider.get("oauth") or {}
                if isinstance(oauth, dict):
                    if oauth.get("key"):
                        oauth_key = str(oauth["key"])
                        explicit_oauth_key = True
                    oauth_host = str(
                        oauth.get("oauth_host") or oauth.get("oauthHost")
                        or oauth_host).rstrip("/")
        except (OSError, ValueError):
            pass
    base_url = os.environ.get("KIMI_CODE_BASE_URL", base_url).rstrip("/")
    oauth_host = os.environ.get(
        "KIMI_CODE_OAUTH_HOST",
        os.environ.get("KIMI_OAUTH_HOST", oauth_host)).rstrip("/")
    if (not explicit_oauth_key
            and (base_url != "https://api.kimi.com/coding/v1"
                 or oauth_host != "https://auth.kimi.com")):
        scope = json.dumps(
            {"oauthHost": oauth_host, "baseUrl": base_url},
            ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        oauth_key = f"oauth/kimi-code-env-{digest}"
    return home, base_url, oauth_key, oauth_host


def _kimi_token(home: Path, oauth_key: str) -> str:
    payload = _load_private_json(_kimi_credential_path(home, oauth_key))
    return str(payload.get("access_token") or "") if payload else ""


def _kimi_credential_path(home: Path, oauth_key: str) -> Path:
    return home / "credentials" / f"{oauth_key.rsplit('/', 1)[-1]}.json"


def _post_form_json(url: str, fields: dict[str, str], timeout: int,
                    headers: dict[str, str]) -> dict | None:
    body = urlencode(fields).encode("ascii")
    request = Request(
        url, data=body, method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "MissionCrew/0.2",
            **headers,
        })
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        return None
    except (URLError, OSError, TimeoutError):
        return None
    if len(raw) > _MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _save_private_json(path: Path, payload: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", delete=False) as stream:
            temp_path = Path(stream.name)
            os.chmod(temp_path, 0o600)
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        return True
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        return False


def _refresh_kimi_token(
        backend: Backend, home: Path, oauth_key: str,
        oauth_host: str, timeout: int) -> str:
    """按 Kimi CLI 的 OAuth 协议刷新，并复用其跨进程锁目录约定。"""
    credential_path = _kimi_credential_path(home, oauth_key)
    lock_target = home / "oauth" / oauth_key.rsplit("/", 1)[-1]
    lock_dir = Path(f"{lock_target}.lock")
    with _KIMI_REFRESH_GUARD:
        try:
            lock_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_target.touch(exist_ok=True)
        except OSError:
            return ""
        deadline = time.monotonic() + min(timeout, 10)
        while True:
            try:
                lock_dir.mkdir()
                break
            except FileExistsError:
                try:
                    if time.time() - lock_dir.stat().st_mtime > 5:
                        lock_dir.rmdir()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return ""
                time.sleep(0.1)
            except OSError:
                return ""
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(1):
                try:
                    os.utime(lock_dir)
                except OSError:
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            credentials = _load_private_json(credential_path)
            if not credentials:
                return ""
            expires_at = _timestamp(credentials.get("expires_at")) or 0
            access_token = str(credentials.get("access_token") or "")
            if access_token and expires_at > time.time() + 30:
                return access_token
            refresh_token = str(credentials.get("refresh_token") or "")
            if not refresh_token:
                return ""
            device_id = ""
            try:
                device_id = (home / "device_id").read_text(
                    encoding="utf-8").strip()
            except OSError:
                pass
            version = backend.version or "unknown"
            payload = _post_form_json(
                f"{oauth_host}/api/oauth/token",
                {
                    "client_id": "17e5f671-d194-4dfb-9706-5516cb48c098",
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout,
                {
                    "X-Msh-Platform": "kimi_code_cli",
                    "X-Msh-Version": version,
                    "X-Msh-Device-Name": socket.gethostname(),
                    "X-Msh-Device-Model": platform.machine(),
                    "X-Msh-Os-Version": platform.release(),
                    **({"X-Msh-Device-Id": device_id} if device_id else {}),
                },
            )
            if not payload or not payload.get("access_token"):
                return ""
            try:
                expires_in = int(payload.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0
            updated = {
                **credentials,
                **payload,
                "refresh_token": payload.get("refresh_token") or refresh_token,
                "expires_at": int(time.time()) + expires_in if expires_in else 0,
            }
            return (str(updated["access_token"])
                    if _save_private_json(credential_path, updated) else "")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            try:
                lock_dir.rmdir()
            except OSError:
                pass


def probe_kimi_usage(
        backend: Backend, timeout: int = 15) -> RuntimeUsageSnapshot:
    home, base_url, oauth_key, oauth_host = _kimi_settings()
    token = _kimi_token(home, oauth_key)
    if not token:
        return _snapshot(
            backend, "auth_required", "kimi_usage_api",
            message="未找到权限安全的 Kimi 登录凭据")
    try:
        payload = _request_json(f"{base_url}/usages", token, timeout)
    except _UsageHttpError as exc:
        if exc.status in {401, 403}:
            token = _refresh_kimi_token(
                backend, home, oauth_key, oauth_host, timeout)
            if not token:
                return _snapshot(
                    backend, "auth_required", "kimi_usage_api",
                    message="Kimi 登录已过期，请重新登录")
            try:
                payload = _request_json(f"{base_url}/usages", token, timeout)
            except _UsageHttpError as retry_exc:
                if retry_exc.status in {401, 403}:
                    return _snapshot(
                        backend, "auth_required", "kimi_usage_api",
                        message="Kimi 登录已过期，请重新登录")
                payload = None
        else:
            payload = None
    if payload is None:
        return _snapshot(
            backend, "unavailable", "kimi_usage_api",
            message="Kimi 限额服务暂时不可用")
    return parse_kimi_usage(backend, payload)


def parse_grok_usage(backend: Backend, payload: dict) -> RuntimeUsageSnapshot:
    config = payload.get("config")
    config = config if isinstance(config, dict) else payload
    percent = _percent(_first(
        config, "creditUsagePercent", "credit_usage_percent"))
    period = config.get("currentPeriod")
    period = period if isinstance(period, dict) else {}
    start = _timestamp(_first(period, "start", "startTime", "start_time"))
    reset = _timestamp(_first(period, "end", "endTime", "end_time"))
    period_type = str(_first(period, "type", "periodType") or "").lower()
    duration_minutes = (
        round((reset - start) / 60) if start and reset and reset >= start else
        10080 if "weekly" in period_type else None)
    label = "本周" if "weekly" in period_type or duration_minutes == 10080 else "当前周期"
    windows = ([RuntimeUsageWindow(
        key="credits", label=label, used_percent=percent,
        resets_at=reset, duration_minutes=duration_minutes,
    )] if percent is not None else [])

    metrics: list[RuntimeUsageMetric] = []
    for key, label in (
        ("prepaidBalance", "预付余额"),
        ("onDemandUsed", "按需已用"),
        ("onDemandCap", "按需上限"),
    ):
        value = config.get(key)
        if isinstance(value, dict):
            value = _first(value, "val", "value", "amount")
        if value is not None:
            metrics.append(RuntimeUsageMetric(label, str(value)))
    product_usage = config.get("productUsage")
    if isinstance(product_usage, list):
        for item in product_usage:
            if not isinstance(item, dict) or item.get("usagePercent") is None:
                continue
            usage_percent = _percent(item["usagePercent"])
            if usage_percent is None:
                continue
            metrics.append(RuntimeUsageMetric(
                str(item.get("product") or "产品用量"),
                f"{usage_percent:g}%"))
    plan = str(_first(config, "subscription_tier", "subscriptionTier") or "")
    if not windows:
        return _snapshot(
            backend, "unavailable", "grok_billing_api",
            plan=plan, metrics=metrics,
            message="Grok 未返回可识别的额度周期")
    return _snapshot(
        backend, "ok", "grok_billing_api", plan=plan,
        windows=windows, metrics=metrics)


def _grok_token() -> str:
    auth_path = Path(os.environ.get(
        "GROK_AUTH_FILE", str(Path.home() / ".grok" / "auth.json"))).expanduser()
    payload = _load_private_json(auth_path)
    if not payload:
        return ""
    tokens = [value for value in payload.values()
              if isinstance(value, dict) and value.get("key")]
    if not tokens and payload.get("key"):
        tokens = [payload]
    if not tokens:
        return ""
    selected = max(tokens, key=lambda item: (
        _timestamp(item.get("expires_at") or item.get("create_time")) or 0))
    return str(selected.get("key") or "")


def probe_grok_usage(
        backend: Backend, command: list[str], timeout: int = 15,
        refresh_auth: Callable[[], None] | None = None) -> RuntimeUsageSnapshot:
    token = _grok_token()
    if not token:
        return _snapshot(
            backend, "auth_required", "grok_billing_api",
            message="未找到权限安全的 Grok 登录凭据")
    base_url = os.environ.get(
        "GROK_CLI_CHAT_PROXY_BASE_URL",
        "https://cli-chat-proxy.grok.com/v1").rstrip("/")
    try:
        payload = _request_json(
            f"{base_url}/billing?format=credits", token, timeout,
            {"x-grok-client-mode": "grok-build"})
    except _UsageHttpError as exc:
        if exc.status == 401:
            try:
                if refresh_auth:
                    refresh_auth()
                else:
                    subprocess.run(
                        [*command, "models"], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            token = _grok_token()
            if not token:
                return _snapshot(
                    backend, "auth_required", "grok_billing_api",
                    message="Grok 登录已过期，请重新登录")
            try:
                payload = _request_json(
                    f"{base_url}/billing?format=credits", token, timeout,
                    {"x-grok-client-mode": "grok-build"})
            except _UsageHttpError as retry_exc:
                if retry_exc.status in {401, 403}:
                    return _snapshot(
                        backend, "auth_required", "grok_billing_api",
                        message="Grok 登录已过期，请重新登录")
                payload = None
        else:
            payload = None
    if payload is None:
        return _snapshot(
            backend, "unavailable", "grok_billing_api",
            message="Grok 额度服务暂时不可用")
    return parse_grok_usage(backend, payload)
