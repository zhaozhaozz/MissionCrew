"""专用 webhook 端口:只接收并答复投递,不暴露平台的任何其他接口。

主服务(看板 + REST API)没有身份验证,只适合在可信网络里访问;webhook 要让
GitHub 这类外部系统够得着,所以单独监听一个端口,上面只有一条路由
`POST /hooks/<webhook 来源路径>`,其他路径一律 404、其他方法 405,不挂静态
文件、文档或 API。需要对外暴露时只转发这个端口。

监听器在主服务进程里用独立线程跑一个 uvicorn 实例(非主线程不接管信号,
停机由主服务 lifespan 负责);端口被占用等启动失败只记录状态,不影响主服务。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..collab.webhooks import WEBHOOK_MAX_BODY, WebhookRejected, accept_webhook
from .context import ApiContext

# 同时处理的连接上限:投递只做验签与入队,很快;超过时 uvicorn 直接回 503
WEBHOOK_MAX_CONCURRENCY = 32
WEBHOOK_START_TIMEOUT = 5.0


async def _read_body(request: Request) -> bytes:
    """按上限读取正文;声明或实际超过 WEBHOOK_MAX_BODY 时拒收,不读完。"""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > WEBHOOK_MAX_BODY:
        raise WebhookRejected(413, "payload too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > WEBHOOK_MAX_BODY:
            raise WebhookRejected(413, "payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_webhook_app(ctx: ApiContext) -> Starlette:
    async def receive(request: Request) -> JSONResponse:
        try:
            body = await _read_body(request)
            headers = {key.lower(): value for key, value in request.headers.items()}
            # 查库、验签、写运行记录都是同步调用,放线程池,不阻塞事件循环
            status, payload = await run_in_threadpool(
                accept_webhook, ctx.store, ctx.automations,
                request.path_params["source_path"], headers, body)
        except WebhookRejected as exc:
            return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)
        return JSONResponse(payload, status_code=status)

    return Starlette(routes=[
        # 来源路径可以有多段(旧版脚本迁移来的是 `<项目>/<脚本>`),按整段查来源
        Route("/hooks/{source_path:path}", receive, methods=["POST"]),
    ])


class WebhookListener:
    """在独立线程里运行 webhook 端口的 uvicorn 实例,并记录监听状态。"""

    def __init__(self, ctx: ApiContext, host: str, port: int):
        self.ctx = ctx
        self.host = host
        self.port = port
        self.error = ""
        self._server = None
        self._thread: Optional[threading.Thread] = None

    @property
    def listening(self) -> bool:
        return bool(self._server and self._server.started
                    and self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {"enabled": True, "host": self.host, "port": self.port,
                "listening": self.listening, "error": self.error}

    def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            create_webhook_app(self.ctx), host=self.host, port=self.port,
            lifespan="off", log_level="warning", access_log=False,
            server_header=False, date_header=False,
            limit_concurrency=WEBHOOK_MAX_CONCURRENCY)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._serve, name="webhook-listener", daemon=True)
        self._thread.start()
        # 等到真正监听或失败(端口占用时 uvicorn 退出线程),状态才可信
        deadline = time.time() + WEBHOOK_START_TIMEOUT
        while time.time() < deadline and self._thread.is_alive() \
                and not self._server.started:
            time.sleep(0.02)
        if not self.listening and not self.error:
            self.error = f"无法监听 {self.host}:{self.port}(端口可能已被占用)"
        if self.error:
            self.ctx.store.audit("platform", "webhook_listener_failed",
                                 detail=f"host={self.host} port={self.port} "
                                        f"error={self.error}")

    def _serve(self) -> None:
        server = self._server
        assert server is not None
        try:
            server.run()
        except SystemExit:   # uvicorn 绑定失败时 sys.exit(1),线程内吞掉并记原因
            self.error = f"无法监听 {self.host}:{self.port}(端口可能已被占用)"
        except Exception as exc:
            self.error = f"webhook 监听异常退出: {exc}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None


def webhook_listener_status(ctx: ApiContext) -> dict:
    listener = ctx.webhook_listener
    return listener.status() if listener else {"enabled": False}
