"""版本化文档库端点:文件树、历史、读写与版本恢复。"""
from __future__ import annotations

import difflib
import mimetypes
import unicodedata
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response

from ..collab.documents import document_resource_url, library_for
from .context import ApiContext
from .schemas import DocumentCompare, DocumentRestore, DocumentWrite


MAX_DOCUMENT_UPLOAD_BYTES = 50 * 1024 * 1024


class _NonTextDocumentError(Exception):
    pass


def _decode_pure_text(content: bytes) -> str:
    """严格识别可比较文本，拒绝非 UTF-8 和二进制控制字符。"""
    text = content.decode("utf-8")
    if any(unicodedata.category(char) == "Cc" and char not in "\t\n\r"
           for char in text):
        raise _NonTextDocumentError
    return text


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/projects/{project_id}/documents")
    def list_documents(project_id: str):
        ctx.must_project(project_id)
        library = library_for(project_id)
        files = [
            {**item, "resource_url": document_resource_url(project_id, item["path"])}
            for item in library.list_files()
        ]
        return {"resource_url": document_resource_url(project_id), "files": files,
                "history": library.history(limit=20)}

    @app.get("/api/projects/{project_id}/documents/history")
    def document_history(project_id: str, path: Optional[str] = None, limit: int = 100):
        ctx.must_project(project_id)
        try:
            return library_for(project_id).history(path, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/projects/{project_id}/documents/compare")
    def compare_document_versions(project_id: str, body: DocumentCompare):
        """比较同一纯文本文件的两个历史版本，返回 unified diff。"""
        ctx.must_project(project_id)
        library = library_for(project_id)
        try:
            before = _decode_pure_text(
                library.read_history_bytes(body.path, body.from_revision))
            after = _decode_pure_text(
                library.read_history_bytes(body.path, body.to_revision))
        except (UnicodeDecodeError, _NonTextDocumentError) as exc:
            raise HTTPException(415, "二进制或非 UTF-8 文件不能比较版本") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        lines = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"{body.path}@{body.from_revision[:10]}",
            tofile=f"{body.path}@{body.to_revision[:10]}",
            lineterm="",
        ))
        identical = before == after
        if not identical and not lines:
            # splitlines() 会忽略文件末换行和 CRLF/LF 差异，但这些仍是文本变更。
            lines = [
                f"--- {body.path}@{body.from_revision[:10]}",
                f"+++ {body.path}@{body.to_revision[:10]}",
                "@@ line endings @@",
                "-A 版本的换行编码或文件末换行状态",
                "+B 版本的换行编码或文件末换行状态",
            ]
        additions = sum(line.startswith("+") and not line.startswith("+++")
                        for line in lines)
        deletions = sum(line.startswith("-") and not line.startswith("---")
                        for line in lines)
        return {
            "path": body.path,
            "from_revision": body.from_revision,
            "to_revision": body.to_revision,
            "additions": additions,
            "deletions": deletions,
            "identical": identical,
            "diff": "\n".join(lines),
        }

    @app.post("/api/projects/{project_id}/documents/upload")
    async def upload_document(project_id: str, request: Request, path: str,
                              overwrite: bool = False, actor: str = "human"):
        """接收单个文件原始字节；多文件上传由前端逐个调用并独立版本化。"""
        ctx.must_project(project_id)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_DOCUMENT_UPLOAD_BYTES:
                    raise HTTPException(413, "单个上传文件不能超过 50 MB")
            except ValueError:
                raise HTTPException(400, "Content-Length 不合法")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_DOCUMENT_UPLOAD_BYTES:
                raise HTTPException(413, "单个上传文件不能超过 50 MB")
            content.extend(chunk)
        library = library_for(project_id)
        try:
            revision = library.write_bytes(
                path, bytes(content), actor=actor, message=f"Upload {path}",
                overwrite=overwrite)
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.audit(actor, "document_uploaded",
                    detail=(f"project={project_id} path={path} "
                            f"size={len(content)} revision={revision}"))
        return {"path": path, "size": len(content),
                "resource_url": document_resource_url(project_id, path),
                "revision": revision}

    @app.get("/api/projects/{project_id}/documents/download/{file_path:path}")
    def download_document(project_id: str, file_path: str,
                          revision: Optional[str] = None):
        """下载文本或二进制文档；可指定历史 revision。"""
        ctx.must_project(project_id)
        try:
            content = library_for(project_id).read_bytes(file_path, revision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        filename = PurePosixPath(file_path).name
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(filename, safe='')}"),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/projects/{project_id}/documents/file/{file_path:path}")
    def read_document(project_id: str, file_path: str, revision: Optional[str] = None):
        ctx.must_project(project_id)
        try:
            content = library_for(project_id).read(file_path, revision)
        except UnicodeDecodeError:   # 注意:它是 ValueError 子类,必须先捕获
            raise HTTPException(415, "二进制或非 UTF-8 文件,无法在线查看(可直接在文档库目录中操作)")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        return {"path": file_path,
                "resource_url": document_resource_url(project_id, file_path),
                "revision": revision, "content": content}

    @app.post("/api/projects/{project_id}/documents/restore")
    def restore_document(project_id: str, body: DocumentRestore):
        """把文件恢复到历史版本(作为新版本提交,历史保持完整)。"""
        ctx.must_project(project_id)
        try:
            revision = library_for(project_id).restore(body.path, body.revision, body.actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        store.audit(body.actor, "document_restored",
                    detail=f"project={project_id} path={body.path} "
                           f"from={body.revision[:10]} new={revision[:10]}")
        return {"path": body.path,
                "resource_url": document_resource_url(project_id, body.path),
                "revision": revision}

    @app.put("/api/projects/{project_id}/documents/file/{file_path:path}")
    def write_document(project_id: str, file_path: str, body: DocumentWrite):
        ctx.must_project(project_id)
        try:
            revision = library_for(project_id).write(
                file_path, body.content, body.actor, body.message)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        store.audit(body.actor, "document_saved",
                    detail=f"project={project_id} path={file_path} revision={revision}")
        return {"path": file_path,
                "resource_url": document_resource_url(project_id, file_path),
                "revision": revision}

    @app.delete("/api/projects/{project_id}/documents/file/{file_path:path}")
    def delete_document(project_id: str, file_path: str, actor: str = "human"):
        ctx.must_project(project_id)
        try:
            revision = library_for(project_id).delete(file_path, actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        store.audit(actor, "document_deleted",
                    detail=f"project={project_id} path={file_path} revision={revision}")
        return {"ok": True,
                "resource_url": document_resource_url(project_id, file_path),
                "revision": revision}
