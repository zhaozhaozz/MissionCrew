"""Runtime(后端)端点:注册表管理、检测、模型目录与升级。"""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ..core import seed as seed_mod
from ..core.models import TIER_ORDER
from ..runtime import adapters
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
        if body.models is not None:
            for m in body.models:
                if m.get("tier") not in TIER_ORDER:
                    raise HTTPException(400, f"模型 {m.get('name') or '(默认)'} 的档位必须是 {TIER_ORDER} 之一")
                try:
                    m["cost"] = float(m.get("cost", 1.0))
                except (TypeError, ValueError):
                    raise HTTPException(400, f"模型 {m.get('name') or '(默认)'} 的成本必须是数字")
                m["name"] = str(m.get("name", ""))
            model_names = {m["name"] for m in body.models}
            invalid_roles = [r for r in store.list_roles()
                             if r.runtime_id == b.id and model_names and r.model not in model_names]
            if invalid_roles:
                names = ", ".join(f"{r.project_id}/@{r.id}" for r in invalid_roles)
                raise HTTPException(400, f"模型仍被角色使用,请先修改角色: {names}")
        for field in ("name", "model", "tier", "cost_per_run", "security_level",
                      "capabilities", "models", "enabled"):
            v = getattr(body, field)
            if v is not None:
                setattr(b, field, v)
        store.put_backend(b)
        store.audit("human", "backend_updated", detail=f"backend={b.id}")
        return b.to_dict()

    @app.get("/api/backends/tools")
    def tools():
        """支持的工具矩阵 + 安装/注册状态(仿 Multica Runtime 页;不探测版本,快速渲染)。"""
        report = adapters.detect_report(with_version=False)
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
                "models": [m.get("name") or "(默认)" for m in (b.models if b else [])],
                "updatable": bool(b and adapters.update_plan(b)),
                **usage(item["id"]),
            })
        # 注册表里的非内置工具(mock/自定义)也列出来
        for b in registered.values():
            rows.append({
                "binary": b.adapter, "adapter": b.adapter, "id": b.id,
                "installed": True, "path": b.binary_path, "version": b.version,
                "registered": True, "enabled": b.enabled,
                "models": [m.get("name") or "(默认)" for m in b.models],
                "updatable": bool(adapters.update_plan(b)),
                **usage(b.id),
            })
        return rows

    @app.get("/api/backends/{backend_id}/models")
    def backend_models(backend_id: str, refresh: bool = False):
        """runtime 可用模型:向工具本体动态查询(缓存 10 分钟),
        configured 为工具的模型阶梯(带档位/成本),discovered 为 runtime 目录。"""
        b = store.get_backend(backend_id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        return {
            "configured": [{"name": m.get("name", ""), "tier": m.get("tier", ""),
                            "cost": m.get("cost")} for m in b.models],
            "discovered": ctx.discovered_models(b, refresh=refresh),
        }

    @app.post("/api/backends/check_updates")
    def check_updates():
        """并行查询各工具的最新发布版本,与已装版本比对(仅注册且已安装的工具)。"""
        backends = [b for b in store.list_backends()
                    if b.adapter in adapters.UPDATE_SPECS and b.binary_path]
        with ThreadPoolExecutor(max_workers=8) as pool:
            latest = dict(zip(
                (b.id for b in backends),
                pool.map(lambda b: adapters.fetch_latest_version(b.adapter), backends),
            ))
        results = []
        for b in backends:
            lv = latest.get(b.id, "")
            results.append({
                "id": b.id, "installed": b.version, "latest": lv,
                "update_available": adapters.is_newer(lv, b.version),
                "updatable": adapters.update_plan(b) is not None,
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
            ok, log = adapters.run_update(b)
            old_version = b.version
            # 更新可长达数分钟:重取最新记录,只补检测字段,
            # 避免过期快照覆盖窗口期内的配额扣减/启停等修改
            fresh = store.get_backend(backend_id)
            if fresh is None:
                raise HTTPException(409, "后端在更新期间被删除")
            binary = Path(fresh.binary_path).name if fresh.binary_path else fresh.adapter
            new_path = shutil.which(binary)
            if new_path:   # 原生更新器可能切换版本目录
                fresh.binary_path = new_path
                fresh.version = adapters._cli_version(binary)
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
        report = adapters.detect_report()   # 含版本探测
        added, updated = [], []
        for b in adapters.detect_backends(report):
            existing = store.get_backend(b.id)
            if existing is None:
                store.put_backend(b)
                added.append(b.id)
            else:  # 只刷新检测信息,保留用户的启停/配额/模型阶梯调整
                existing.binary_path, existing.version = b.binary_path, b.version
                if not existing.models:
                    existing.models = b.models
                store.put_backend(existing)
                updated.append(b.id)
        seed_mod.ensure_default_project(store)
        seed_mod.ensure_role_bindings(store)
        return {"found": [i["id"] for i in report if i["installed"]],
                "added": added, "updated": updated}

    @app.delete("/api/backends/{backend_id}")
    def delete_backend(backend_id: str):
        if store.get_backend(backend_id) is None:
            raise HTTPException(404, "后端不存在")
        users = [r for r in store.list_roles() if r.runtime_id == backend_id]
        if users:
            names = ", ".join(f"{r.project_id}/@{r.id}" for r in users)
            raise HTTPException(409, f"runtime 仍被角色使用,请先修改角色: {names}")
        store.delete_backend(backend_id)
        store.audit("human", "backend_deleted", detail=f"backend={backend_id}")
        return {"ok": True}
