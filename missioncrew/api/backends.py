"""Runtime(后端)端点:注册表管理、检测、模型目录与升级。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException

from ..core import seed as seed_mod
from ..core.models import TIER_ORDER
from ..runtime import runtime_manager
from .context import ApiContext
from .schemas import BackendInput


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.post("/api/backends")
    def update_backend(body: BackendInput):
        b = store.get_backend(body.id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        if body.tier is not None and body.tier not in TIER_ORDER:
            raise HTTPException(400, f"档位必须是 {TIER_ORDER} 之一")
        # models 不可编辑:工具自带清单由 KNOWN_MODELS 在检测时刷新,不接受写入。
        for field in ("name", "model", "tier", "cost_per_run", "security_level",
                      "capabilities", "enabled"):
            v = getattr(body, field)
            if v is not None:
                setattr(b, field, v)
        store.put_backend(b)
        if b.enabled:
            seed_mod.ensure_role_templates(store)
        store.audit("human", "backend_updated", detail=f"backend={b.id}")
        return b.to_dict()

    @app.get("/api/backends/tools")
    def tools():
        """支持的工具矩阵 + 安装/注册状态(仿 Multica Runtime 页;不探测版本,快速渲染)。"""
        report = runtime_manager.detect_report(with_version=False)
        registered = {b.id: b for b in store.list_backends()}
        project_names = {p.id: p.name for p in store.list_projects()}
        role_users: dict[str, list[dict]] = {}
        for role in store.list_roles():
            if not role.runtime_id:
                continue
            role_users.setdefault(role.runtime_id, []).append({
                "id": role.id,
                "name": role.name,
                "project_id": role.project_id,
                "project_name": project_names.get(role.project_id, role.project_id),
                "model": role.model,
            })
        for role in store.list_role_templates():
            if not role.runtime_id:
                continue
            role_users.setdefault(role.runtime_id, []).append({
                "id": role.id,
                "name": role.name,
                "project_id": "",
                "project_name": "全局角色模板",
                "model": role.model,
                "template": True,
            })
        for users in role_users.values():
            users.sort(key=lambda r: (r["project_name"], r["id"]))

        def usage(backend_id: str) -> dict:
            users = role_users.get(backend_id, [])
            return {"role_count": len(users), "role_users": users}

        rows = []
        for item in report:
            b = registered.pop(item["id"], None)
            rows.append({
                **item,
                "registered": b is not None,
                "enabled": b.enabled if b else False,
                "version": (b.version if b else "") or "",
                "path": (b.binary_path if b and b.binary_path else item["path"]),
                "models": [m or "(默认)" for m in (b.models if b else [])],
                "updatable": bool(b and runtime_manager.update_plan(b)),
                "operations": (runtime_manager.capabilities(b).to_dict() if b else {}),
                **usage(item["id"]),
            })
        # 注册表里的非内置工具(mock/自定义)也列出来
        for b in registered.values():
            rows.append({
                "binary": b.adapter, "adapter": b.adapter, "id": b.id,
                "installed": True, "path": b.binary_path, "version": b.version,
                "registered": True, "enabled": b.enabled,
                "models": [m or "(默认)" for m in b.models],
                "updatable": bool(runtime_manager.update_plan(b)),
                "operations": runtime_manager.capabilities(b).to_dict(),
                **usage(b.id),
            })
        # API 也保证已安装项优先，避免已打开页面仍运行旧版前端渲染逻辑时
        # 把支持矩阵末尾的已安装工具（例如 traecli）留在表格底部。
        rows.sort(key=lambda row: row["installed"], reverse=True)
        return rows

    @app.get("/api/backends/{backend_id}/models")
    def backend_models(backend_id: str, refresh: bool = False):
        """runtime 可用模型:configured 为工具自带清单(别名,含 ""=CLI 默认),
        discovered 为向工具本体查询的型号目录(缓存 10 分钟)。"""
        b = store.get_backend(backend_id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        return {
            "configured": list(b.models),
            "discovered": ctx.discovered_models(b, refresh=refresh),
        }

    @app.post("/api/backends/check_updates")
    def check_updates():
        """并行查询各工具的最新发布版本,与已装版本比对(仅注册且已安装的工具)。"""
        backends = [b for b in store.list_backends()
                    if runtime_manager.can_check_updates(b)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            latest = dict(zip(
                (b.id for b in backends),
                pool.map(runtime_manager.fetch_latest_version, backends),
            ))
        results = []
        for b in backends:
            lv = latest.get(b.id, "")
            results.append({
                "id": b.id, "installed": b.version, "latest": lv,
                "update_available": runtime_manager.is_newer(lv, b.version),
                "updatable": runtime_manager.update_plan(b) is not None,
            })
        return results

    @app.post("/api/backends/{backend_id}/update")
    def do_update(backend_id: str):
        """执行工具更新(自更新命令或 npm),完成后重新探测版本入库。

        互斥:同一后端同时只允许一个更新(并发 npm install -g 会写坏全局安装);
        更新期间该后端不再被派发新的聊天执行(见 ChatEngine.updating_backends)。
        """
        b = store.get_backend(backend_id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        with ctx.updating_guard:
            if backend_id in ctx.updating_backends:
                raise HTTPException(409, "该工具正在更新中,请等待完成")
            ctx.updating_backends.add(backend_id)
        try:
            ok, log = runtime_manager.update(b)
            old_version = b.version
            # 更新可长达数分钟:重取最新记录,只补检测字段,
            # 避免过期快照覆盖窗口期内的配额扣减/启停等修改
            fresh = store.get_backend(backend_id)
            if fresh is None:
                raise HTTPException(409, "后端在更新期间被删除")
            fresh = runtime_manager.refresh_installation(fresh)
            if fresh.binary_path:
                store.put_backend(fresh)
            store.audit("human", "backend_update", detail=(
                f"backend={backend_id} ok={ok} {old_version} -> {fresh.version}"))
            return {"ok": ok, "old_version": old_version, "version": fresh.version,
                    "log": log[-1500:]}
        finally:
            with ctx.updating_guard:
                ctx.updating_backends.discard(backend_id)

    @app.post("/api/backends/detect")
    def detect():
        report = runtime_manager.detect_report()   # 含版本探测
        added, updated = [], []
        for b in runtime_manager.detect_backends(report):
            existing = store.get_backend(b.id)
            if existing is None:
                store.put_backend(b)
                added.append(b.id)
            else:  # 刷新检测信息与工具自带模型清单,保留用户的启停/配额调整
                existing.binary_path, existing.version = b.binary_path, b.version
                existing.models = b.models
                store.put_backend(existing)
                updated.append(b.id)
        seed_mod.ensure_role_templates(store)
        seed_mod.ensure_default_project(store)
        seed_mod.ensure_role_bindings(store)
        return {"found": [i["id"] for i in report if i["installed"]],
                "added": added, "updated": updated}

    @app.post("/api/backends/{backend_id}/stop")
    def stop_backend(backend_id: str, session_key: str = ""):
        """通过统一 Runtime 生命周期接口停止活动执行或会话。"""
        backend = store.get_backend(backend_id)
        if backend is None:
            raise HTTPException(404, "后端不存在")
        stopped = runtime_manager.stop(backend, session_key)
        store.audit("human", "backend_stopped", detail=(
            f"backend={backend_id} session={session_key or '*'} stopped={stopped}"))
        return {"ok": True, "stopped": stopped}

    @app.post("/api/backends/{backend_id}/interrupt")
    def interrupt_backend(backend_id: str, session_key: str = ""):
        """中断当前 turn，原生 session/thread 保持可复用。"""
        backend = store.get_backend(backend_id)
        if backend is None:
            raise HTTPException(404, "后端不存在")
        interrupted = runtime_manager.interrupt(backend, session_key)
        store.audit("human", "backend_interrupted", detail=(
            f"backend={backend_id} session={session_key or '*'} "
            f"interrupted={interrupted}"))
        return {"ok": True, "interrupted": interrupted}

    @app.delete("/api/backends/{backend_id}")
    def delete_backend(backend_id: str):
        backend = store.get_backend(backend_id)
        if backend is None:
            raise HTTPException(404, "后端不存在")
        users = [r for r in store.list_roles() if r.runtime_id == backend_id]
        templates = [r for r in store.list_role_templates() if r.runtime_id == backend_id]
        if users or templates:
            names = ", ".join([
                *(f"{r.project_id}/@{r.id}" for r in users),
                *(f"全局角色模板/@{r.id}" for r in templates),
            ])
            raise HTTPException(409, f"runtime 仍被角色使用,请先修改角色: {names}")
        runtime_manager.stop(backend)
        store.delete_backend(backend_id)
        store.audit("human", "backend_deleted", detail=f"backend={backend_id}")
        return {"ok": True}
