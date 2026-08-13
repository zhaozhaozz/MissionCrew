"""Runtime(后端)端点:注册表管理、检测、模型目录与升级。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi import Body

from ..core import config as core_config
from ..core import seed as seed_mod
from ..core.models import TIER_ORDER
from ..runtime import runtime_manager
from ..runtime.pi import SUPPORTED_PROVIDER_APIS, validate_pi_providers
from .context import ApiContext
from .schemas import BackendInput

# 平台对外提供的能力是"接入任意 OpenAI / Anthropic 兼容 API",这些自定义
# 模型当前交给 pi 执行,配置也复用它的 models.json。换执行后端时只需改这个
# 常量与下面的读写实现,对外的 /api/model-providers 契约和页面都不用动。
CUSTOM_MODEL_RUNTIME = "pi"


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

    def _read_model_providers() -> dict:
        path = core_config.pi_models_path()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"providers": {}}
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"模型接入配置不可读: {exc}") from exc

    def _executor_state() -> dict:
        """执行这些自定义模型的 Runtime 现状,供页面提示先装/先启用。"""
        backend = store.get_backend(CUSTOM_MODEL_RUNTIME)
        installed = any(item["id"] == CUSTOM_MODEL_RUNTIME and item["installed"]
                        for item in runtime_manager.detect_report(with_version=False))
        return {
            "id": CUSTOM_MODEL_RUNTIME,
            "installed": installed,
            "registered": backend is not None,
            "enabled": bool(backend and backend.enabled),
            "models": list(backend.models) if backend else [],
        }

    @app.get("/api/model-providers")
    def model_providers():
        """用户自定义的 OpenAI / Anthropic 兼容 API 接入配置。"""
        providers = {}
        for name, spec in (_read_model_providers().get("providers") or {}).items():
            if not isinstance(spec, dict):
                continue
            # 字面量密钥不回传浏览器,只告知"已保存";$ENV_VAR 是引用不是
            # 密钥本身,原样返回以便页面显示引用了哪个环境变量。
            key = str(spec.get("apiKey") or "")
            providers[name] = {**spec,
                               "apiKey": key if key.startswith("$") else "",
                               "apiKeySaved": bool(key)}
        return {
            "path": str(core_config.pi_models_path()),
            "config": {"providers": providers},
            "apis": sorted(SUPPORTED_PROVIDER_APIS),
            "executor": _executor_state(),
        }

    @app.put("/api/model-providers")
    def put_model_providers(body: dict = Body(...)):
        """写入自定义 API 接入配置并刷新对应 Runtime 的执行单元清单。

        配置整体落在平台数据目录,不触碰用户级 CLI 配置;apiKey 支持字面量
        或 $ENV_VAR 引用。某个 provider 的 apiKey 留空表示沿用已保存的值,
        这样编辑界面不需要先把明文密钥读出来再写回去。"""
        incoming = body.get("providers")
        if not isinstance(incoming, dict):
            raise HTTPException(400, "缺少 providers 对象")
        stored = _read_model_providers().get("providers") or {}
        providers = {}
        for name, spec in incoming.items():
            if not isinstance(spec, dict):
                raise HTTPException(400, f"provider {name} 必须是对象")
            spec = {k: v for k, v in spec.items() if k != "apiKeySaved"}
            if not str(spec.get("apiKey") or ""):
                kept = str((stored.get(name) or {}).get("apiKey") or "")
                if kept:
                    spec["apiKey"] = kept
            providers[name] = spec
        payload = {**body, "providers": providers}
        error = validate_pi_providers(payload)
        if error:
            raise HTTPException(400, error)
        path = core_config.pi_models_path()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        path.chmod(0o600)   # 文件含密钥,收紧权限
        backend = store.get_backend(CUSTOM_MODEL_RUNTIME)
        if backend is not None:
            backend.models = runtime_manager.list_models(backend)
            store.put_backend(backend)
        store.audit("human", "model_providers_updated",
                    detail=f"providers={sorted(providers)}")
        return {"ok": True, "executor": _executor_state()}

    @app.get("/api/backends/{backend_id}/models")
    def backend_models(backend_id: str, refresh: bool = False):
        """runtime 可用模型:configured 为工具自带清单(别名,含 ""=CLI 默认),
        discovered 为向工具本体查询的型号目录(缓存 10 分钟)。

        efforts 是同一次探测里工具自报的按模型推理力度(模型 -> 档位,低到高);
        只有能自报的工具(目前是 grok)才有条目,其余为空 = 角色编辑器回退到
        /api/traits 的 adapter 级档位。"""
        b = store.get_backend(backend_id)
        if b is None:
            raise HTTPException(404, "后端不存在")
        models, efforts = ctx.discovered_catalog(b, refresh=refresh)
        return {
            "configured": list(b.models),
            "discovered": models,
            "efforts": efforts,
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
