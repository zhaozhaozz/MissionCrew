"""供 Runtime 调用的、Bearer Token 鉴权的 MissionCrew Agent Tool API。"""
from __future__ import annotations

import logging
import uuid

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..collab.agent_tools import AgentToolError
from .context import ApiContext
from .schemas import AgentToolCallInput


LOGGER = logging.getLogger(__name__)


def _error_response(error: AgentToolError, request_id: str = "") -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
    return JSONResponse(
        status_code=error.status_code,
        headers=headers,
        content={
            "ok": False,
            "request_id": request_id,
            "error": error.to_dict(),
        },
    )


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise AgentToolError(
            "missing_token", "请使用 Authorization: Bearer <token>", 401)
    return token.strip()


def register(app: FastAPI, ctx: ApiContext) -> None:
    service = ctx.chat.agent_tools

    @app.get("/api/agent/v1/actions")
    def actions(request: Request):
        try:
            identity = service.authenticate(_bearer_token(request))
            return {"ok": True, "result": service.capabilities(identity)}
        except AgentToolError as exc:
            return _error_response(exc)
        except Exception:
            LOGGER.exception("Agent Tool capability lookup failed")
            return _error_response(AgentToolError(
                "internal_error", "MissionCrew 读取工具能力时发生内部错误", 500,
                retryable=True))

    @app.post("/api/agent/v1/actions")
    def call_action(request: Request, body: object = Body(...)):
        request_id = uuid.uuid4().hex
        try:
            body = AgentToolCallInput.model_validate(body)
            request_id = body.request_id or request_id
            identity = service.authenticate(_bearer_token(request))
            result = service.execute(
                identity, body.action, body.arguments, body.run_id, request_id)
            return {
                "ok": True,
                "request_id": request_id,
                "action": body.action,
                "result": result,
            }
        except ValidationError as exc:
            error = AgentToolError(
                "invalid_request", exc.errors(include_url=False)[0]["msg"], 422)
            return _error_response(error, request_id)
        except AgentToolError as exc:
            return _error_response(exc, request_id)
        except Exception:
            LOGGER.exception("Agent Tool request failed: request=%s", request_id)
            return _error_response(AgentToolError(
                "internal_error", "MissionCrew 执行动作时发生内部错误", 500,
                retryable=True), request_id)
