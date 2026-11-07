"""凭据端点:全局凭据与项目凭据的维护,只由人在设置页操作。

值只写不读:保存时可以提交新值,任何端点都不返回值,页面只显示末 4 位指纹;
要改就整份替换。项目列表同时带上继承来的全局凭据,并标出被项目同名覆盖的。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.credentials import delete_credential, save_credential
from ..core.models import Credential
from .context import ApiContext
from .schemas import CredentialInput


def credential_data(credential: Credential, **extra) -> dict:
    return {**credential.to_dict(), "id": credential.id, "scope": credential.scope, **extra}


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    def _save(project_id: str, body: CredentialInput) -> dict:
        try:
            credential, created = save_credential(
                store, project_id=project_id, name=body.name,
                description=body.description, value=body.value)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return credential_data(credential, created=created)

    def _delete(project_id: str, name: str) -> dict:
        try:
            delete_credential(store, project_id=project_id, name=name)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"deleted": True}

    def _must_project(project_id: str) -> None:
        if store.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在")

    @app.get("/api/credentials")
    def list_global():
        return {"credentials": [credential_data(item)
                                for item in store.list_credentials("")]}

    @app.post("/api/credentials")
    def save_global(body: CredentialInput):
        return _save("", body)

    @app.delete("/api/credentials/{name}")
    def delete_global(name: str):
        return _delete("", name)

    @app.get("/api/projects/{project_id}/credentials")
    def list_project(project_id: str):
        _must_project(project_id)
        own = store.list_credentials(project_id)
        overridden = {item.name for item in own}
        return {
            "credentials": [credential_data(item) for item in own],
            # 继承的全局凭据:overridden = 本项目有同名凭据,Agent 读到的是项目那份
            "inherited": [credential_data(item, overridden=item.name in overridden)
                          for item in store.list_credentials("")],
        }

    @app.post("/api/projects/{project_id}/credentials")
    def save_project(project_id: str, body: CredentialInput):
        _must_project(project_id)
        return _save(project_id, body)

    @app.delete("/api/projects/{project_id}/credentials/{name}")
    def delete_project(project_id: str, name: str):
        _must_project(project_id)
        return _delete(project_id, name)
