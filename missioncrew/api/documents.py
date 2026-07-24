"""版本化文档库端点:文件树、历史、读写与版本恢复。"""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response

from ..collab.documents import document_resource_url, library_for
from ..collab.recycle_bin import recycle_document
from .context import ApiContext
from .diffutil import (_NonTextDocumentError, _decode_pure_text,
                       build_diff_ops, build_text_diff)
from .schemas import DocumentCompare, DocumentRestore, DocumentWrite


MAX_DOCUMENT_UPLOAD_BYTES = 50 * 1024 * 1024


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
        label_a = f"{body.path}@{body.from_revision[:10]}"
        label_b = f"{body.path}@{body.to_revision[:10]}"
        return {
            "path": body.path,
            "from_revision": body.from_revision,
            "to_revision": body.to_revision,
            **build_text_diff(before, after, label_a, label_b),
            "ops": build_diff_ops(before, after),
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
                          revision: Optional[str] = None,
                          inline: bool = False):
        """下载文本或二进制文档；可指定历史 revision；inline 时浏览器内联预览。"""
        ctx.must_project(project_id)
        try:
            content = library_for(project_id).read_bytes(file_path, revision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        filename = PurePosixPath(file_path).name
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        disposition = "inline" if inline else "attachment"
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}"),
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
        project = ctx.must_project(project_id)
        try:
            item = recycle_document(store, project, file_path, actor=actor)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        return {"ok": True, "recycle_item": item,
                "resource_url": document_resource_url(project_id, file_path),
                "revision": item["revision"]}
