"""Runtime 可调用的 MissionCrew Agent Tool CLI。

客户端只负责读取工作区令牌、编码本地文件和呈现结构化结果；权限与业务校验全部
由 MissionCrew Agent Tool API 完成。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional


def _base_url() -> str:
    return os.environ.get(
        "MISSIONCREW_AGENT_TOOL_URL",
        "http://127.0.0.1:8321/api/agent/v1",
    ).rstrip("/")


def _token_file() -> Path:
    value = os.environ.get("MISSIONCREW_AGENT_TOKEN_FILE", "")
    if not value:
        raise ValueError("未设置 MISSIONCREW_AGENT_TOKEN_FILE")
    return Path(value)


def _load_token() -> str:
    try:
        token = _token_file().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取 Agent Tool token 文件: {exc}") from exc
    if not token:
        raise ValueError("Agent Tool token 文件为空")
    return token


def _request_json(method: str, payload: Optional[dict] = None) -> tuple[int, dict]:
    body = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None)
    request = urllib.request.Request(
        _base_url() + "/actions", data=body, method=method,
        headers={
            "Authorization": f"Bearer {_load_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            data = {
                "ok": False,
                "error": {"code": "http_error", "message": str(exc),
                          "retryable": exc.code >= 500},
            }
        return exc.code, data
    except urllib.error.URLError as exc:
        return 0, {
            "ok": False,
            "error": {
                "code": "connection_failed",
                "message": f"无法连接 MissionCrew Agent Tool API: {exc.reason}",
                "retryable": True,
            },
        }


def _default_run_id() -> Optional[int]:
    value = os.environ.get("MISSIONCREW_AGENT_RUN_ID", "").strip()
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _require_run_id(value: Optional[int]) -> int:
    if value is None or value <= 0:
        raise ValueError("必须通过 --run-id 显式提供当前 Prompt 中的 run_id")
    return value


def _request_payload(action: str, arguments: dict,
                     run_id: Optional[int]) -> dict:
    return {
        "action": action,
        "arguments": arguments,
        "run_id": _require_run_id(run_id),
        "request_id": uuid.uuid4().hex,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="missioncrew-tool",
        description="Call the authenticated MissionCrew Agent Tool API.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("actions", help="List actions allowed for this role token.")

    call = subparsers.add_parser("call", help="Call a canonical MissionCrew action.")
    call.add_argument("action")
    call.add_argument("--run-id", type=int, default=_default_run_id())
    call.add_argument("--arguments", default="{}", help="JSON object")

    message = subparsers.add_parser("publish-message", help="Publish a channel message.")
    message.add_argument("--run-id", type=int, default=_default_run_id())
    message.add_argument("--channel", required=True)
    message.add_argument("--content", required=True)
    message.add_argument("--mention", action="append", default=[])

    document = subparsers.add_parser("publish-file", help="Publish a local file.")
    document.add_argument("--run-id", type=int, default=_default_run_id())
    document.add_argument("--source", type=Path, required=True)
    document.add_argument("--path", required=True)
    document.add_argument("--message", default="")
    document.add_argument("--overwrite", action="store_true")
    return parser


def _local_error(message: str) -> dict:
    return {
        "ok": False,
        "error": {"code": "client_error", "message": message, "retryable": False},
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "actions":
            status, result = _request_json("GET")
        elif args.command == "call":
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("--arguments 必须是 JSON 对象")
            status, result = _request_json(
                "POST", _request_payload(args.action, arguments, args.run_id))
        elif args.command == "publish-message":
            status, result = _request_json("POST", _request_payload(
                "message.publish",
                {"channel": args.channel, "content": args.content,
                 "mentions": args.mention},
                args.run_id,
            ))
        else:
            try:
                content = args.source.read_bytes()
            except OSError as exc:
                raise ValueError(f"无法读取源文件 {args.source}: {exc}") from exc
            status, result = _request_json("POST", _request_payload(
                "document.publish",
                {"path": args.path,
                 "content_base64": base64.b64encode(content).decode("ascii"),
                 "overwrite": args.overwrite, "message": args.message},
                args.run_id,
            ))
    except (ValueError, json.JSONDecodeError) as exc:
        status, result = 0, _local_error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status and result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
