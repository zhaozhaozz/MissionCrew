"""版本化文档库端点:文件树、历史、读写与版本恢复。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException

from ..collab.documents import library_for
from .context import ApiContext
from .schemas import DocumentRestore, DocumentWrite


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/projects/{project_id}/documents")
    def list_documents(project_id: str):
        ctx.must_project(project_id)
        library = library_for(project_id)
        return {"root": str(library.root), "files": library.list_files(),
                "history": library.history(limit=20)}

    @app.get("/api/projects/{project_id}/documents/history")
    def document_history(project_id: str, path: Optional[str] = None, limit: int = 100):
        ctx.must_project(project_id)
        try:
            return library_for(project_id).history(path, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

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
        return {"path": file_path, "revision": revision, "content": content}

    @app.post("/api/projects/{project_id}/documents/restore")
    def restore_document(project_id: str, body: DocumentRestore):
        """把文件恢复到历史版本(作为新版本提交,历史保持完整)。"""
        ctx.must_project(project_id)
        try:
            revision = library_for(project_id).restore(body.path, body.revision, body.actor)
        except UnicodeDecodeError:   # ValueError 子类,先捕获
            raise HTTPException(415, "二进制文件请直接在文档库目录中恢复")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        store.audit(body.actor, "document_restored",
                    detail=f"project={project_id} path={body.path} "
                           f"from={body.revision[:10]} new={revision[:10]}")
        return {"path": body.path, "revision": revision}

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
        return {"path": file_path, "revision": revision}

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
        return {"ok": True, "revision": revision}
