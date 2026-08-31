"""总览与词表端点。

总览分层:`/api/overview` 只返回全局数据(项目列表/后端/角色模板),
项目内数据走 `/api/projects/{id}/overview`(角色/面板/自动化),
频道与任务由各自模块提供带 scope 的按项目列表,按页面状态按需拉取。
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request

from ..collab.skills import sync_all_project_skill_libraries
from ..collab.resource_urls import (automation_resource_url,
                                    dashboard_resource_url,
                                    guideline_resource_url,
                                    skill_resource_url)
from ..core.models import (BOARD_WIDGET_TYPES, ROLE_ABILITIES, TIER_ORDER,
                           text_fingerprint)
from ..runtime import runtime_manager
from .context import ApiContext, etag_json_response


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    @app.get("/api/overview")
    def overview(request: Request):
        # 用户可直接向项目 skills/ 投放目录；轮询总览时自动发现并同步。
        sync_all_project_skill_libraries(store)

        def project_data(project):
            data = project.to_dict()
            # 总览每 8s 轮询一次,准则/Skill 只带元信息 + 内容指纹;
            # 正文由准则单条端点和 skills/library 按需拉取,指纹供前端检测外部修改
            data["guidelines"] = [
                {"name": item["name"], "description": item["description"],
                 "enabled": item["enabled"],
                 "markdown_fingerprint": text_fingerprint(item["markdown"]),
                 "resource_url": guideline_resource_url(project.id, item["name"])}
                for item in data["guidelines"]
            ]
            data["skills"] = [
                {"id": item["id"], "name": item["name"],
                 "description": item["description"], "enabled": item["enabled"],
                 "instructions_fingerprint": text_fingerprint(item["instructions"]),
                 "resource_url": skill_resource_url(project.id, item["id"])}
                for item in data["skills"]
            ]
            return data

        return etag_json_response(request, {
            "projects": [project_data(p) for p in store.list_projects()],
            "backends": [b.to_dict() for b in store.list_backends()],
            "role_templates": [r.to_dict() for r in store.list_role_templates()],
        })

    @app.get("/api/projects/{project_id}/overview")
    def project_overview(project_id: str, request: Request):
        """项目内总览:角色(含限额停用状态)、面板、自动化。"""
        ctx.must_project(project_id)

        usage_blocks = {
            item["role_id"]: item
            for item in store.list_role_usage_blocks()
            if item["project_id"] == project_id
        }

        def role_data(role):
            data = role.to_dict()
            block = usage_blocks.get(role.id)
            data["usage_auto_disabled"] = block is not None
            data["usage_disabled_until"] = (
                block["disabled_until"] if block else None)
            data["usage_window_keys"] = block["window_keys"] if block else []
            return data

        return etag_json_response(request, {
            "roles": [role_data(r) for r in store.list_roles(project_id)],
            "boards": [{**b.to_dict(),
                        "resource_url": dashboard_resource_url(project_id, b.id)}
                       for b in store.list_boards(project_id)],
            "automations": [
                {**a.to_dict(),
                 "resource_url": automation_resource_url(project_id, a.id)}
                for a in store.list_automations(project_id)],
        })

    @app.get("/api/traits")
    def traits():
        from ..collab.agent_tools import AUTOMATION_ACTIONS
        from ..core.models import AUTOMATION_DEFAULT_ACTIONS
        return {"abilities": ROLE_ABILITIES, "tiers": TIER_ORDER,
                "board_widget_types": sorted(BOARD_WIDGET_TYPES),
                "effort_options": runtime_manager.effort_catalog(),
                "automation_actions": sorted(AUTOMATION_ACTIONS),
                "automation_default_actions": list(AUTOMATION_DEFAULT_ACTIONS)}

    @app.get("/api/runtime/status")
    def runtime_status():
        """系统级 Runtime 实例快照；前端轮询实现实时状态页。"""
        return runtime_manager.status(store.list_backends())

    @app.get("/api/runtime/history")
    def runtime_history(limit: int = 100, backend_id: str = ""):
        """系统级 Runtime 调用历史，数据跨服务重启保留。"""
        return {
            "generated_at": time.time(),
            "history": store.list_runtime_usage(limit, backend_id),
        }

    @app.get("/api/runtime/usage")
    def runtime_account_usage(refresh: bool = False):
        """读取本机已登录 Runtime 账户的限额窗口；凭据不会离开服务进程。"""
        result = runtime_manager.account_usage(
            store.list_backends(), refresh=refresh)
        result["role_linkage"] = ctx.role_usage_linkage.reconcile(
            result, now=result.get("generated_at"))
        return result
