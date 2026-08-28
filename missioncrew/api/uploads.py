"""频道附件端点:输入框上传的文件落到频道 uploads 目录并回传本地路径。

上传走原始字节流(与文档库上传同一模式,不引入 multipart 依赖);文件保存在
collab.workspace.channel_uploads_dir,该目录在装配执行配置时授权给频道内
所有角色,消息正文中携带的绝对路径 Agent 可直接读取。
"""
from __future__ import annotations

import mimetypes
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response

from ..collab.workspace import channel_uploads_dir
from .context import ApiContext

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
# 文件名只保留可移植字符;路径分隔符与控制字符一律替换,防止越界写入
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-\u4e00-\u9fff]+")
# 以“文档”身份内联打开会执行脚本的类型,交给 CSP sandbox 隔离(同文档库口径)
_SANDBOXED_INLINE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    def must_channel(channel_id: str):
        channel = store.get_channel(channel_id)
        if channel is None:
            raise HTTPException(404, f"频道不存在: {channel_id}")
        return channel

    @app.post("/api/chat/{channel_id}/uploads")
    async def upload_attachment(channel_id: str, request: Request,
                                filename: str = ""):
        """接收单个文件原始字节;多文件由前端逐个调用。"""
        channel = must_channel(channel_id)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_ATTACHMENT_BYTES:
                    raise HTTPException(413, "单个附件不能超过 50 MB")
            except ValueError:
                raise HTTPException(400, "Content-Length 不合法")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_ATTACHMENT_BYTES:
                raise HTTPException(413, "单个附件不能超过 50 MB")
            content.extend(chunk)
        if not content:
            raise HTTPException(400, "附件内容为空")
        base = (_UNSAFE_NAME_RE.sub("_", PurePosixPath(filename).name)
                .strip("._") or "file")
        directory = channel_uploads_dir(channel.project_id or "", channel.id)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        name = f"{stamp}-{base}"
        counter = 0
        while (directory / name).exists():
            counter += 1
            name = f"{stamp}-{counter}-{base}"
        target = directory / name
        target.write_bytes(bytes(content))
        media_type = mimetypes.guess_type(base)[0] or "application/octet-stream"
        store.audit("human", "chat_attachment_uploaded",
                    detail=f"channel={channel.id} name={name} size={len(content)}")
        return {"name": name, "path": str(target), "size": len(content),
                "is_image": media_type.startswith("image/"),
                "url": (f"/api/chat/{quote(channel.id, safe='')}"
                        f"/uploads/{quote(name)}")}

    @app.get("/api/chat/{channel_id}/uploads/{name}")
    def download_attachment(channel_id: str, name: str):
        """回读附件,供消息里的图片预览与人工下载。"""
        channel = must_channel(channel_id)
        directory = channel_uploads_dir(channel.project_id or "", channel.id)
        target = (directory / name).resolve()
        if target.parent != directory or not target.is_file():
            raise HTTPException(404, "附件不存在")
        media_type = (mimetypes.guess_type(target.name)[0]
                      or "application/octet-stream")
        headers = {
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{quote(target.name, safe='')}"),
            "X-Content-Type-Options": "nosniff",
        }
        if media_type in _SANDBOXED_INLINE_TYPES:
            headers["Content-Security-Policy"] = (
                "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox")
        return Response(content=target.read_bytes(), media_type=media_type,
                        headers=headers)
