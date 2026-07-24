"""项目主控、频道、文档库、自定义面板和结构化上下文的集成测试。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.runtime import adapters, runtime_manager
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.documents import (document_resource_url, library_for,
                                          normalize_document_resource_urls)
from missioncrew.collab.project_context import guideline_context_dir
from missioncrew.collab.resource_urls import (channel_resource_url,
                                              dashboard_resource_url,
                                              guideline_resource_url,
                                              missioncrew_project_url,
                                              skill_resource_url,
                                              task_resource_url)
from missioncrew.collab.skills import (materialize_project_skills,
                                       project_skill_library_dir,
                                       skill_context_dir)
from missioncrew.collab.workspace import (migrate_legacy_workspace_layout,
                                          migrate_resource_workspace_links,
                                          write_task_files)
from missioncrew.core.models import (DEFAULT_MAX_CHAIN_RUNS, Backend, Channel,
                                     ExecutionConfig, GuidelineDocument,
                                     ProjectResource, ProjectSkill, RunResult)
from missioncrew.api import create_app


def _client(seeded):
    return TestClient(create_app())


def test_content_page_uses_stable_dedicated_channel_with_persisted_messages(seeded):
    client = _client(seeded)
    guideline = client.post("/api/projects/webshop/guidelines", json={
        "markdown": "---\nname: api-style\ndescription: API 风格\n---\n\n正文\n",
        "enabled": True,
    })
    assert guideline.status_code == 200
    assert not any(item.content_kind == "guidelines"
                   and item.content_key == "api-style"
                   for item in seeded.list_channels("webshop"))
    missing = client.get("/api/projects/webshop/content-channel", params={
        "content_kind": "guidelines",
        "content_key": "api-style",
    })
    assert missing.status_code == 404

    first = client.post("/api/projects/webshop/content-channel", json={
        "content_kind": "guidelines",
        "content_key": "api-style",
        "label": "api-style",
    })
    second = client.post("/api/projects/webshop/content-channel", json={
        "content_kind": "guidelines",
        "content_key": "api-style",
        "label": "api-style",
    })
    assert first.status_code == second.status_code == 200
    channel = first.json()
    assert second.json()["id"] == channel["id"]
    assert channel["content_kind"] == "guidelines"
    assert channel["content_key"] == "api-style"
    assert not channel["id"].endswith(":general")
    resolved = client.get("/api/projects/webshop/content-channel", params={
        "content_kind": "guidelines",
        "content_key": "api-style",
    })
    assert resolved.status_code == 200
    assert resolved.json()["id"] == channel["id"]

    posted = client.post(f"/api/chat/{channel['id']}/messages", json={
        "author": "human",
        "content": "解释选中的段落",
        "mentions": [],
        "context": {
            "page_collaboration": {
                "page_kind": "guidelines",
                "current_item": "api-style",
                "selection": {"line_start": 5, "line_end": 7},
            },
        },
    })
    assert posted.status_code == 200
    history = client.get(f"/api/chat/{channel['id']}/messages").json()["messages"]
    human = next(item for item in history if item["id"] == posted.json()["id"])
    assert human["content"] == "解释选中的段落"
    assert human["context"]["page_collaboration"]["selection"]["line_start"] == 5

    # 重新解析相当于刷新文章页：仍绑定同一个频道，消息无需另行保存。
    refreshed = client.post("/api/projects/webshop/content-channel", json={
        "content_kind": "guidelines",
        "content_key": "api-style",
        "label": "api-style",
    }).json()
    assert refreshed["id"] == channel["id"]
    assert any(item["id"] == posted.json()["id"] for item in
               client.get(f"/api/chat/{refreshed['id']}/messages").json()["messages"])
    assert client.post(f"/api/chat/channels/{channel['id']}/archive").status_code == 409
    assert client.delete(f"/api/chat/channels/{channel['id']}").status_code == 409

    renamed = client.post("/api/projects/webshop/guidelines", json={
        "original_name": "api-style",
        "markdown": "---\nname: api-contract\ndescription: API 风格\n---\n\n正文\n",
        "enabled": True,
    })
    assert renamed.status_code == 200
    rebound = next(item for item in seeded.list_channels("webshop")
                   if item.content_kind == "guidelines"
                   and item.content_key == "api-contract")
    assert rebound.id == channel["id"]
    assert any(item["id"] == posted.json()["id"]
               for item in seeded.list_messages(rebound.id))


def test_content_channels_are_not_precreated_by_startup_lists_or_saves(seeded):
    library_for("webshop").write(
        "lazy-channel.md", "# Lazy channel\n", "human", "Seed document")
    client = _client(seeded)

    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/projects/webshop/documents").status_code == 200
    assert client.get("/api/projects/webshop/guidelines").status_code == 200
    assert client.get("/api/projects/webshop/skills").status_code == 200
    assert client.put("/api/projects/webshop/documents/file/saved-lazily.md", json={
        "content": "# Saved lazily\n",
        "actor": "human",
        "message": "Save document",
    }).status_code == 200
    assert client.post("/api/projects/webshop/skills", json={
        "id": "lazy-skill",
        "markdown": (
            "---\nname: Lazy Skill\ndescription: Test lazy channel creation\n"
            "---\n\n# Lazy Skill\n"
        ),
        "enabled": True,
    }).status_code == 200

    assert not any(channel.content_kind
                   for channel in seeded.list_channels("webshop"))


def test_project_has_one_configurable_orchestrator_and_protects_it(seeded):
    client = _client(seeded)
    project = next(p for p in client.get("/api/overview").json()["projects"]
                   if p["id"] == "webshop")
    assert project["orchestrator_role_id"] == "lead"
    assert project["max_chain_runs"] == DEFAULT_MAX_CHAIN_RUNS == 100

    project.update({"orchestrator_role_id": "expert", "max_chain_runs": 1000})
    response = client.post("/api/projects", json=project)
    assert response.status_code == 200
    assert response.json()["orchestrator_role_id"] == "expert"
    assert response.json()["max_chain_runs"] == 1000
    assert client.delete("/api/roles/expert?project_id=webshop").status_code == 409

    chat = ChatEngine(seeded)
    msg_id = seeded.add_message("general", "human", "human", "@expert 调度任务", ["expert"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "expert"),
                         seeded.get_backend("exp-1"), msg_id)
    assert "项目主控权限" in cfg.prompt
    assert "单条协作链最多 1000 次 Agent 执行" in cfg.prompt
    dev_cfg = chat._assemble(seeded.get_channel("general"),
                             seeded.get_role("webshop", "dev"),
                             seeded.get_backend("std-1"), msg_id)
    assert "项目主控权限" not in dev_cfg.prompt

    project["max_chain_runs"] = 0
    assert client.post("/api/projects", json=project).status_code == 422


def test_orchestrator_can_create_task_channel_and_dynamic_board(seeded):
    client = _client(seeded)
    forbidden = client.post("/api/chat/channels", json={
        "id": "api-design", "project_id": "webshop", "actor_role_id": "dev",
    })
    assert forbidden.status_code == 403
    created = client.post("/api/chat/channels", json={
        "id": "api-design", "name": "API 设计", "project_id": "webshop",
        "purpose": "只讨论结算 API", "actor_role_id": "lead",
    })
    assert created.status_code == 200
    assert created.json()["purpose"] == "只讨论结算 API"
    assert created.json()["created_by_role_id"] == "lead"
    prompt = ChatEngine(seeded)._assemble(
        seeded.get_channel("webshop:api-design"), seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"),
        seeded.add_message("webshop:api-design", "human", "human", "@lead 开始", ["lead"]),
    ).prompt
    assert "频道用途/讨论边界:只讨论结算 API" in prompt

    layout = [{
        "id": "requirements", "type": "table", "title": "需求",
        "x": 0, "y": 0, "width": 8, "height": 6,
        "content": {"columns": ["需求", "状态"], "rows": []},
    }]
    board = client.post("/api/projects/webshop/boards", json={
        "id": "delivery", "name": "交付面板", "layout": layout,
        "actor_role_id": "lead",
    })
    assert board.status_code == 200
    assert board.json()["layout"][0]["type"] == "table"
    assert client.post("/api/projects/webshop/boards", json={
        "id": "bad", "layout": layout, "actor_role_id": "dev",
    }).status_code == 403

    # 主控 Runtime 也能通过受限动作协议创建频道和面板。
    chat = ChatEngine(seeded)
    reply = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '开始执行。<missioncrew-action>{"action":"create_channel",'
        '"id":"qa","name":"QA","purpose":"测试闭环"}</missioncrew-action>'
        '<missioncrew-action>{"action":"create_board","id":"quality",'
        '"name":"质量面板","layout":[]}</missioncrew-action>',
        root_id=1, depth=0,
    )
    assert seeded.get_channel("webshop:qa").purpose == "测试闭环"
    assert seeded.get_board("webshop:quality") is not None
    assert "missioncrew-action" not in reply and "平台操作" in reply
    assert "[#QA](/resources/webshop/channels/qa)" in reply
    assert "[质量面板](/resources/webshop/dashboards/quality)" in reply


def test_document_library_versions_and_context_use_links_on_demand(seeded):
    client = _client(seeded)
    url = "/api/projects/webshop/documents/file/specs/checkout.md"
    first = client.put(url, json={"content": "# Checkout v1\n", "actor": "alice"})
    second = client.put(url, json={"content": "# Checkout v2\n", "actor": "bob"})
    assert first.status_code == second.status_code == 200
    history = client.get(
        "/api/projects/webshop/documents/history?path=specs%2Fcheckout.md"
    ).json()
    assert [row["actor"] for row in history[:2]] == ["bob", "alice"]
    compared = client.post("/api/projects/webshop/documents/compare", json={
        "path": "specs/checkout.md",
        "from_revision": history[1]["revision"],
        "to_revision": history[0]["revision"],
    })
    assert compared.status_code == 200
    comparison = compared.json()
    assert comparison["additions"] == comparison["deletions"] == 1
    assert comparison["identical"] is False
    assert "-# Checkout v1" in comparison["diff"]
    assert "+# Checkout v2" in comparison["diff"]
    identical = client.post("/api/projects/webshop/documents/compare", json={
        "path": "specs/checkout.md",
        "from_revision": history[0]["revision"],
        "to_revision": history[0]["revision"],
    })
    assert identical.status_code == 200
    assert identical.json()["identical"] is True
    newline_only = client.put(url, json={"content": "# Checkout v2", "actor": "carol"})
    newline_comparison = client.post(
        "/api/projects/webshop/documents/compare", json={
            "path": "specs/checkout.md",
            "from_revision": history[0]["revision"],
            "to_revision": newline_only.json()["revision"],
        })
    assert newline_comparison.status_code == 200
    assert newline_comparison.json()["identical"] is False
    assert "line endings" in newline_comparison.json()["diff"]
    old = client.get(url + f"?revision={history[1]['revision']}").json()
    assert old["content"] == "# Checkout v1\n"
    assert old["resource_url"] == \
        "/resources/webshop/documents/specs/checkout.md"
    listing = client.get("/api/projects/webshop/documents").json()
    assert "root" not in listing
    assert listing["resource_url"] == "/resources/webshop/documents"
    assert next(item for item in listing["files"]
                if item["path"] == "specs/checkout.md")["resource_url"] == \
        "/resources/webshop/documents/specs/checkout.md"
    resource_page = client.get(
        "/resources/webshop/documents/specs/checkout.md")
    assert resource_page.status_code == 200 and "MissionCrew" in resource_page.text
    # HTTP 客户端会自行规范化 `..` URL；直接验证服务层的真实路径边界。
    with pytest.raises(ValueError, match="相对路径"):
        library_for("webshop").write("../escape.md", "no")
    with pytest.raises(ValueError, match="相对路径"):
        library_for("webshop").write("/absolute.md", "no")
    library = library_for("webshop")
    (library.root / "runtime-note.md").write_text("ordinary directory write\n")
    runtime_revision = library.commit_changes("role:dev", "Runtime document update")
    assert runtime_revision
    assert library.history("runtime-note.md")[0]["actor"] == "role:dev"
    project = seeded.get_project("webshop")
    project.skills.append(ProjectSkill(
        id="checkout-dev", name="结算开发",
        instructions="遵循 [结算说明](specs/checkout.md)",
    ))
    project.skills.append(ProjectSkill(
        id="expert-only", instructions="仅在疑难任务中使用",
    ))
    project.skills.append(ProjectSkill(id="common", instructions="所有角色通用"))
    project.guidelines.append(GuidelineDocument(
        name="dev-guide", description="开发代码或 API 时使用", content="开发相关任务准则"))
    project.guidelines.append(GuidelineDocument(
        name="tester-guide", description="设计或执行测试时使用", content="测试相关任务准则"))
    project.guidelines.append(GuidelineDocument(
        name="disabled-guide", description="停用摘要", content="停用准则正文", enabled=False))
    seeded.put_project(project)
    materialize_project_skills(project)
    chat = ChatEngine(seeded)
    msg_id = seeded.add_message("general", "human", "human", "@dev 开发", ["dev"])
    chat_cfg = chat._assemble(seeded.get_channel("general"),
                              seeded.get_role("webshop", "dev"),
                              seeded.get_backend("std-1"), msg_id)
    assert "checkout-dev" in chat_cfg.prompt and "expert-only" in chat_cfg.prompt
    assert "开发代码或 API 时使用" in chat_cfg.prompt
    assert "设计或执行测试时使用" in chat_cfg.prompt
    assert "开发相关任务准则" not in chat_cfg.prompt
    assert "测试相关任务准则" not in chat_cfg.prompt
    assert "停用摘要" not in chat_cfg.prompt and "停用准则正文" not in chat_cfg.prompt
    assert "[结算说明](specs/checkout.md)" not in chat_cfg.prompt
    checkout_skill = (Path(chat_cfg.env["MISSIONCREW_SKILLS_DIR"])
                      / "checkout-dev" / "SKILL.md")
    assert "[结算说明](specs/checkout.md)" in checkout_skill.read_text()
    assert str(checkout_skill) in chat_cfg.prompt
    assert "# Checkout v2" not in chat_cfg.prompt  # 链接文件不再预注入
    assert "仅在任务需要时读取链接文件" in chat_cfg.prompt
    assert chat_cfg.env["MISSIONCREW_DOCUMENTS_DIR"] in chat_cfg.prompt
    assert chat_cfg.env["MISSIONCREW_DOCUMENTS_URL"] == \
        "/resources/webshop/documents"
    assert chat_cfg.env["MISSIONCREW_PROJECT_URL"] == "/resources/webshop"
    assert "/resources/webshop/dashboards/<面板 id>" in chat_cfg.prompt
    assert "最终回复引用项目文档时必须写成" in chat_cfg.prompt
    assert "不得输出内部读写目录" in chat_cfg.prompt
    guideline_dir = Path(chat_cfg.env["MISSIONCREW_GUIDELINES_DIR"])
    dev_guideline = guideline_dir / "dev-guide.md"
    tester_guideline = guideline_dir / "tester-guide.md"
    assert str(guideline_dir) in chat_cfg.prompt
    assert guideline_dir.is_symlink()
    assert guideline_dir.resolve() == guideline_context_dir(project)
    assert str(dev_guideline) in chat_cfg.prompt
    assert str(guideline_dir.parent) in chat_cfg.allowed_dirs
    assert str(guideline_context_dir(project)) in chat_cfg.allowed_dirs
    assert str(skill_context_dir(project)) in chat_cfg.allowed_dirs
    assert not (guideline_dir / "disabled-guide.md").exists()
    assert dev_guideline.read_text() == (
        "---\nname: dev-guide\ndescription: 开发代码或 API 时使用\n---\n\n"
        "开发相关任务准则\n")
    assert tester_guideline.read_text().endswith("\n测试相关任务准则\n")
    (guideline_dir / "stale.md").write_text("stale")
    (guideline_dir.parent / "guidelines.json").write_text("{}")

    # description 不变但正文更新时，内容版本仍会改变公共上下文版本，已有 session
    # 下一轮会收到更新提示；完整文件也会原子刷新为新正文。
    first_context_version = chat_cfg.context_version
    next(row for row in project.guidelines if row.name == "dev-guide").content = "开发准则第二版"
    seeded.put_project(project)
    updated_cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
        seeded.get_backend("std-1"), msg_id)
    assert updated_cfg.context_version != first_context_version
    updated_dir = Path(updated_cfg.env["MISSIONCREW_GUIDELINES_DIR"])
    assert not (updated_dir / "stale.md").exists()
    assert not (updated_dir.parent / "guidelines.json").exists()
    assert (updated_dir / "dev-guide.md").read_text().endswith("\n开发准则第二版\n")


def test_all_missioncrew_resources_have_stable_web_urls(seeded):
    client = _client(seeded)
    created_channel = client.post("/api/chat/channels", json={
        "id": "release", "project_id": "webshop", "actor_role_id": "lead",
    }).json()
    created_board = client.post("/api/projects/webshop/boards", json={
        "id": "delivery", "name": "交付面板", "actor_role_id": "lead",
    }).json()
    created_task = client.post("/api/tasks", json={
        "project_id": "webshop", "title": "验证统一资源 URL",
    }).json()
    guideline = client.post("/api/projects/webshop/guidelines", json={
        "markdown": "---\nname: release-check\ndescription: 发布前检查\n---\n",
        "actor_role_id": "lead",
    }).json()
    skill = client.post("/api/projects/webshop/skills", json={
        "id": "release-helper",
        "markdown": "---\nname: Release helper\ndescription: 发布辅助\n---\n",
        "actor_role_id": "lead",
    }).json()

    assert missioncrew_project_url("webshop") == "/resources/webshop"
    assert created_channel["resource_url"] == "/resources/webshop/channels/release"
    assert created_board["resource_url"] == "/resources/webshop/dashboards/delivery"
    assert created_task["resource_url"] == task_resource_url(
        "webshop", created_task["id"])
    assert guideline["resource_url"] == "/resources/webshop/guidelines/release-check"
    assert skill["resource_url"] == "/resources/webshop/skills/release-helper"
    assert skill_resource_url(
        "webshop", "release-helper", "references/使用说明.md"
    ) == "/resources/webshop/skills/release-helper/references/%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md"
    assert channel_resource_url("webshop", "webshop:release") == created_channel["resource_url"]
    assert dashboard_resource_url("webshop", "webshop:delivery") == created_board["resource_url"]
    assert guideline_resource_url("webshop", "release-check") == guideline["resource_url"]

    overview = client.get("/api/overview").json()
    assert next(item for item in overview["channels"]
                if item["id"] == "webshop:release")["resource_url"] == created_channel["resource_url"]
    assert next(item for item in overview["boards"]
                if item["id"] == "webshop:delivery")["resource_url"] == created_board["resource_url"]
    project = next(item for item in overview["projects"] if item["id"] == "webshop")
    assert next(item for item in project["guidelines"]
                if item["name"] == "release-check")["resource_url"] == guideline["resource_url"]
    assert next(item for item in project["skills"]
                if item["id"] == "release-helper")["resource_url"] == skill["resource_url"]

    for url in (created_channel["resource_url"], created_board["resource_url"],
                created_task["resource_url"], guideline["resource_url"],
                skill["resource_url"], "/resources/webshop/dashboards/tasks"):
        assert client.get(url).status_code == 200


def test_document_resource_url_replaces_internal_agent_paths(tmp_path):
    root = tmp_path / ".missioncrew" / "projects" / "science_agent" / "documents"
    canonical = root / "architecture" / "current design.md"
    assert document_resource_url(
        "science_agent", "architecture/current design.md") == \
        "/resources/science_agent/documents/architecture/current%20design.md"
    reply = (
        f"正式文档：[架构]({canonical})\n"
        "兼容入口：[/doc](/srv/.missioncrew/agent-workspaces/science_agent/"
        "channels/general/lead/.missioncrew/documents/architecture/current.md)\n"
        "业务源码：/home/test/code/company/science_agent/server.ts"
    )
    normalized = normalize_document_resource_urls(reply, "science_agent", [root])
    assert str(root) not in normalized and "agent-workspaces" not in normalized
    assert "/resources/science_agent/documents/architecture/current design.md" in normalized
    assert "/resources/science_agent/documents/architecture/current.md" in normalized
    assert "/home/test/code/company/science_agent/server.ts" in normalized


def test_guideline_and_skill_management_have_no_binding_fields(seeded):
    client = _client(seeded)
    guideline = client.post("/api/projects/webshop/guidelines", json={
        "markdown": "---\nname: testing\ndescription: 修改行为时使用\n---\n\n"
                    "# 测试规范\n\n所有修复必须回归。\n",
        "actor_role_id": "lead",
    })
    assert guideline.status_code == 200
    assert guideline.json()["name"] == "testing"
    assert guideline.json()["description"] == "修改行为时使用"
    assert guideline.json()["markdown"].startswith(
        "---\nname: testing\ndescription: 修改行为时使用\n---\n")
    assert all(key not in guideline.json() for key in ("file_refs", "role_ids"))
    materialized_dir = guideline_context_dir(seeded.get_project("webshop"))
    assert (materialized_dir / "testing.md").read_text() == guideline.json()["markdown"]

    renamed = client.post("/api/projects/webshop/guidelines", json={
        "original_name": "testing",
        "markdown": "---\nname: regression-testing\n"
                    "description: 从 Markdown 文件头直接读取的新说明\n---\n\n正文\n",
        "actor_role_id": "lead",
    })
    assert renamed.status_code == 200
    assert renamed.json()["description"] == "从 Markdown 文件头直接读取的新说明"
    assert not (materialized_dir / "testing.md").exists()
    assert (materialized_dir / "regression-testing.md").read_text() == renamed.json()["markdown"]
    persisted = seeded.get_project("webshop")
    assert [g.name for g in persisted.guidelines].count("regression-testing") == 1
    assert all(g.name != "testing" for g in persisted.guidelines)
    assert client.post("/api/projects/webshop/guidelines", json={
        "markdown": "---\nname: forbidden\ndescription: no\n---\n",
        "actor_role_id": "tester",
    }).status_code == 403
    wrong_attributes = client.post("/api/projects/webshop/guidelines", json={
        "markdown": "---\nid: wrong\nsummary: old\n---\n", "actor_role_id": "lead",
    })
    assert wrong_attributes.status_code == 400
    assert "id, summary" in wrong_attributes.json()["detail"]
    skill = client.post("/api/projects/webshop/skills", json={
        "id": "local-ci", "name": "本地 CI", "instructions": "运行完整测试",
        "actor_role_id": "lead",
    })
    assert skill.status_code == 200
    assert all(key not in skill.json() for key in (
        "file_refs", "runtime_ids", "adapters", "runtime_instructions", "role_ids"))

    # 旧持久化字段仍可读取，但会被丢弃，不再造成预注入或 Runtime 绑定。
    legacy_guideline = GuidelineDocument.from_dict({
        "id": "legacy-guide", "file_refs": ["specs/testing.md"],
        "role_ids": ["tester"]})
    legacy_skill = ProjectSkill.from_dict({
        "id": "legacy-skill", "file_refs": ["specs/testing.md"],
        "runtime_ids": ["std-1"], "adapters": ["codex"],
        "runtime_instructions": {"std-1": "旧覆盖"},
    })
    assert "[specs/testing.md](specs/testing.md)" in legacy_guideline.content
    assert legacy_guideline.description == "相关文档"
    assert "[specs/testing.md](specs/testing.md)" in legacy_skill.instructions
    assert "旧覆盖" in legacy_skill.instructions
    assert not hasattr(legacy_guideline, "role_ids") and not hasattr(legacy_skill, "role_ids")

    # 兼容清理曾误写入正文的旧 frontmatter，避免运行时生成两层文件头。
    migrated_header = GuidelineDocument.from_dict({
        "id": "outer-name", "summary": "outer description",
        "content": "---\nid: legacy-name\ntitle: 旧标题\nsummary: 正文文件头摘要\n---\n\n正文",
    })
    assert migrated_header.name == "legacy-name"
    assert migrated_header.description == "正文文件头摘要"
    assert migrated_header.content == "正文"
    assert migrated_header.render_markdown().count("---") == 2


def test_guideline_git_history_follows_rename_and_restore(seeded):
    from missioncrew.collab.documents import guideline_library_for

    client = _client(seeded)
    base_url = "/api/projects/webshop/guidelines/task-validation"
    initial_history = client.get(base_url + "/history")
    assert initial_history.status_code == 200
    initial = initial_history.json()[0]
    assert initial["actor"] == "platform"
    assert initial["path"] == "task-validation.md"

    updated = client.post("/api/projects/webshop/guidelines", json={
        "original_name": "task-validation",
        "markdown": "---\nname: task-validation\n"
                    "description: 新验证说明\n---\n\n# 第二版\n",
        "actor_role_id": "lead",
    })
    assert updated.status_code == 200
    assert len(updated.json()["revision"]) == 40

    renamed = client.post("/api/projects/webshop/guidelines", json={
        "original_name": "task-validation",
        "markdown": "---\nname: delivery-validation\n"
                    "description: 交付验证\n---\n\n# 第三版\n",
        "actor_role_id": "lead",
    })
    assert renamed.status_code == 200
    history_url = "/api/projects/webshop/guidelines/delivery-validation/history"
    history = client.get(history_url).json()
    assert history[0]["revision"] == renamed.json()["revision"]
    assert any(row["path"] == "task-validation.md" for row in history)

    old = client.get(history_url + f"/{initial['revision']}")
    assert old.status_code == 200
    assert old.json()["name"] == "task-validation"
    assert "# 任务验证指导" in old.json()["markdown"]

    restored = client.post(
        "/api/projects/webshop/guidelines/delivery-validation/restore",
        json={"revision": initial["revision"], "actor_role_id": "lead"})
    assert restored.status_code == 200
    assert restored.json()["name"] == "task-validation"
    assert restored.json()["revision"] != initial["revision"]
    current = next(item for item in seeded.get_project("webshop").guidelines
                   if item.name == "task-validation")
    assert "# 任务验证指导" in current.content
    library = guideline_library_for("webshop")
    assert library.read("task-validation.md") == current.render_markdown()
    assert library.repo.name == "guideline-history.git"
    assert not seeded._query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='guideline_versions'")

    current_history_url = "/api/projects/webshop/guidelines/task-validation/history"
    assert client.get(current_history_url + "/deadbeef00").status_code == 404


def test_sandboxed_cli_commands_allow_the_document_library():
    docs = "/mc/projects/demo/documents"
    codex = adapters.render_command(
        adapters.DEFAULT_COMMANDS["codex"], "work", "gpt-test", docs)
    claude = adapters.render_command(
        adapters.DEFAULT_COMMANDS["claude_code"], "work", "sonnet", docs)
    assert codex[codex.index("--add-dir") + 1] == docs
    assert claude[claude.index("--add-dir") + 1] == docs


def test_all_project_directories_are_assembled_for_chat(seeded, tmp_path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    project = seeded.get_project("webshop")
    project.repos = [
        ProjectResource(id="a", kind="path", path=str(repo_a)),
        ProjectResource(id="b", kind="path", path=str(repo_b)),
        # 同一路径的重复资源只应授权一次。
        ProjectResource(id="b-copy", kind="path", path=str(repo_b)),
        ProjectResource(id="remote", kind="git", remote="https://example.com/x.git"),
    ]
    seeded.put_project(project)
    library = library_for("webshop")

    chat = ChatEngine(seeded)
    message = seeded.add_message("general", "human", "human", "@dev 检查两个仓库", ["dev"])
    chat_cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
        seeded.get_backend("std-1"), message,
    )
    shared = [str(repo_a.resolve()), str(repo_b.resolve()), str(library.root.resolve())]
    skill_root = str(project_skill_library_dir("webshop").resolve())
    guideline_view = str(guideline_context_dir(project))
    skill_view = str(skill_context_dir(project))
    chat_workspace = str(Path(chat_cfg.env["MISSIONCREW_WORKSPACE"]).resolve())
    assert chat_cfg.timeout is None
    assert chat_cfg.allowed_dirs == [
        *shared, chat_workspace, guideline_view, skill_view, skill_root]
    assert all(path in chat_cfg.prompt for path in shared)
    assert chat_workspace in chat_cfg.prompt


def test_all_runtime_commands_apply_directory_policy():
    dirs = ["/projects/a", "/projects/b"]
    workdir = "/workspace"

    rendered = {
        name: adapters.render_command(template, "work", "model",
                                      allowed_dirs=dirs, workdir=workdir)
        for name, template in adapters.DEFAULT_COMMANDS.items()
    }
    for name in ("claude_code", "codex", "codebuddy"):
        assert rendered[name].count("--add-dir") == len(dirs)
        assert all(path in rendered[name] for path in dirs)
    assert [arg for arg in rendered["copilot"] if arg.startswith("--add-dir=")] == [
        f"--add-dir={path}" for path in dirs]
    assert rendered["opencode"][rendered["opencode"].index("--dir") + 1] == workdir
    assert "--force" in rendered["cursor"]
    assert rendered["pi"][:2] == ["pi", "-p"]

    acp_commands = {
        name: adapters.render_command(
            template, "", "", allowed_dirs=dirs, workdir=workdir)
        for name, template in adapters.ACP_SERVE_COMMANDS.items()
    }
    for name in ("kimi", "qoder", "trae"):
        assert acp_commands[name].count("--add-dir") == len(dirs)
        assert all(path in acp_commands[name] for path in dirs)
    assert acp_commands["grok_build"][
        acp_commands["grok_build"].index("--cwd") + 1] == workdir
    assert acp_commands["grok_build"][-1] == "stdio"
    assert "--trust-all-tools" in acp_commands["kiro"]


def test_runtime_environment_syncs_pwd_and_scopes_opencode_external_dirs(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "shared"
    workspace.mkdir()
    external.mkdir()
    backend = Backend(id="oc", name="OpenCode", adapter="opencode")
    cfg = ExecutionConfig(
        task_id="t", stage_name="chat", backend=backend, prompt="work",
        workdir=str(workspace),
        allowed_dirs=[str(workspace), str(external)],
        env={"OPENCODE_CONFIG_CONTENT": json.dumps({
            "permission": {"bash": "ask", "external_directory": {"*": "deny"}}
        })},
    )

    for name in [*adapters.DEFAULT_COMMANDS, *adapters.ACP_SERVE_COMMANDS]:
        env = adapters._runtime_env(cfg, name)
        assert env["PWD"] == str(workspace.resolve())
        assert json.loads(env["MISSIONCREW_ALLOWED_DIRS"]) == cfg.allowed_dirs

    config = json.loads(adapters._runtime_env(cfg, "opencode")["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["bash"] == "ask"
    rules = config["permission"]["external_directory"]
    assert rules["*"] == "deny"
    assert rules[f"{external.resolve()}/**"] == "allow"
    assert f"{workspace.resolve()}/**" not in rules


# ---- 主控调度闭环:post_message / workdir / 布局保留 / 权限门 ----

def test_orchestrator_dispatches_into_new_channel(seeded):
    """主控建频道 + post_message 派工:被显式选择的角色在新频道真实执行。"""
    chat = ChatEngine(seeded)
    root = seeded.add_message("general", "human", "human", "@lead 开新任务", ["lead"])
    chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"create_channel","id":"pay",'
        '"name":"支付任务","purpose":"支付重构"}</missioncrew-action>'
        '<missioncrew-action>{"action":"post_message","channel":"pay",'
        '"content":"@[dev] 请实现支付重构,验收标准见频道用途。"}</missioncrew-action>',
        root_id=root, depth=0,
    )
    chat.wait_idle()
    msgs = seeded.list_messages("webshop:pay")
    authors = [(m["author"], m["author_type"]) for m in msgs]
    assert ("lead", "agent") in authors            # 主控的开工简报落在新频道
    assert ("dev", "agent") in authors             # dev 被真实触发并回复
    assert all(m["root_id"] == root for m in msgs)  # 共享同一协作链预算


def test_post_message_rejects_foreign_channel(seeded):
    chat = ChatEngine(seeded)
    reply = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"post_message","channel":"ghost",'
        '"content":"hi"}</missioncrew-action>', root_id=1, depth=0)
    assert "控制动作未执行" in reply and "不存在" in reply


def test_create_channel_workdir_validated_against_repos(seeded, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    project = seeded.get_project("webshop")
    from missioncrew.core.models import ProjectResource
    project.repos = [ProjectResource(id="repo", kind="path", path=str(repo))]
    seeded.put_project(project)
    chat = ChatEngine(seeded)
    # 合法:repo 子目录
    chat._apply_orchestrator_actions(
        project, "lead",
        '<missioncrew-action>{"action":"create_channel","id":"feat",'
        f'"name":"F","workdir":"{repo}/src"}}</missioncrew-action>',
        root_id=1, depth=0)
    assert seeded.get_channel("webshop:feat").workdir == str(repo / "src")
    # 非法:仓库之外
    reply = chat._apply_orchestrator_actions(
        project, "lead",
        '<missioncrew-action>{"action":"create_channel","id":"evil",'
        '"name":"E","workdir":"/etc"}</missioncrew-action>', root_id=1, depth=0)
    assert seeded.get_channel("webshop:evil") is None and "控制动作未执行" in reply
    # 未指定且只有一个仓:默认用它
    chat._apply_orchestrator_actions(
        project, "lead",
        '<missioncrew-action>{"action":"create_channel","id":"auto",'
        '"name":"A"}</missioncrew-action>', root_id=1, depth=0)
    assert seeded.get_channel("webshop:auto").workdir == str(repo)


def test_update_board_without_layout_preserves_widgets(seeded):
    client = _client(seeded)
    layout = [{"id": "w1", "type": "markdown", "title": "说明",
               "x": 0, "y": 0, "width": 6, "height": 4,
               "content": {"markdown": "hello"}}]
    client.post("/api/projects/webshop/boards",
                json={"id": "req", "name": "需求", "layout": layout})
    # REST:不带 layout 只改名 -> 布局保留
    client.post("/api/projects/webshop/boards", json={"id": "req", "name": "需求 v2"})
    board = seeded.get_board("webshop:req")
    assert board.name == "需求 v2" and len(board.layout) == 1
    # 聊天动作:不带 layout 键同样保留
    chat = ChatEngine(seeded)
    chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"update_board","id":"req",'
        '"name":"需求 v3"}</missioncrew-action>', root_id=1, depth=0)
    board = seeded.get_board("webshop:req")
    assert board.name == "需求 v3" and len(board.layout) == 1
    # 聊天动作:delete_board 生效
    chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"delete_board","id":"req"}</missioncrew-action>',
        root_id=1, depth=0)
    assert seeded.get_board("webshop:req") is None


def test_non_orchestrator_actions_are_stripped_end_to_end(seeded):
    """非主控回复中的控制动作:端到端验证被剥离且不生效(mock 回显动作块)。"""
    chat = ChatEngine(seeded)
    chat.post("general", "human",
              '@[dev] 试试越权 <missioncrew-action>{"action":"create_board",'
              '"id":"hack","name":"H"}</missioncrew-action>')
    chat.wait_idle()
    assert seeded.get_board("webshop:hack") is None
    dev_reply = next(m for m in seeded.list_messages("general")
                     if m["author"] == "dev")
    assert "missioncrew-action" not in dev_reply["content"]
    assert "只有项目主控可以执行" in dev_reply["content"]


def test_orchestrator_prompt_lists_channels_boards_and_budget(seeded):
    chat = ChatEngine(seeded)
    client = _client(seeded)
    seeded.put_channel(Channel(id="webshop:active-topic", name="active-topic",
                               project_id="webshop"))
    seeded.put_channel(Channel(id="webshop:archived-topic", name="archived-topic",
                               project_id="webshop", archived=True))
    client.post("/api/projects/webshop/boards",
                json={"id": "quality", "name": "质量面板", "layout": []})
    msg = seeded.add_message("general", "human", "human", "@lead 看看", ["lead"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "lead"),
                         seeded.get_backend("std-1"), msg)
    assert "## 现有频道" in cfg.prompt and "general" in cfg.prompt
    assert "active-topic" in cfg.prompt and "archived-topic" not in cfg.prompt
    assert "## 现有面板" in cfg.prompt and "quality" in cfg.prompt
    assert "协作链预算" in cfg.prompt and "message.publish" in cfg.prompt
    assert "missioncrew-action>" not in cfg.prompt
    assert "@[角色ID]" in cfg.prompt and "普通 @角色ID 只是正文引用" in cfg.prompt


def test_orchestrator_can_generate_project_config_and_documents(seeded):
    """配置生成复用主控执行链，并通过受限 action 真正写入项目。"""
    chat = ChatEngine(seeded)
    reply = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '配置已生成。'
        '<missioncrew-action>{"action":"save_guideline",'
        '"markdown":"---\\nname: api-style\\ndescription: 修改 API 时使用\\n---\\n\\n'
        '# API 规范\\n\\n保持兼容","enabled":true}'
        '</missioncrew-action>'
        '<missioncrew-action>{"action":"save_skill","id":"local-ci",'
        '"name":"本地 CI","instructions":"运行 [CI](runbooks/local-ci.md)",'
        '"enabled":true}'
        '</missioncrew-action>'
        '<missioncrew-action>{"action":"write_document","path":"specs/generated.md",'
        '"content":"# Generated\\n","message":"Generate spec"}</missioncrew-action>',
        root_id=1, depth=0,
    )
    project = seeded.get_project("webshop")
    guideline = next(g for g in project.guidelines if g.name == "api-style")
    assert guideline.content == "# API 规范\n\n保持兼容"
    assert guideline.description == "修改 API 时使用"
    assert next(s for s in project.skills if s.id == "local-ci").instructions == \
        "运行 [CI](runbooks/local-ci.md)"
    assert not hasattr(project, "rules")
    assert library_for("webshop").read("specs/generated.md") == "# Generated\n"
    assert "missioncrew-action" not in reply
    assert "已保存准则文档" in reply
    assert "[api-style](/resources/webshop/guidelines/api-style)" in reply
    assert "[本地 CI](/resources/webshop/skills/local-ci)" in reply
    assert ("已发布文档 [specs/generated.md]"
            "(/resources/webshop/documents/specs/generated.md)") in reply

    # 旧 save_rule 动作已被移除；其他配置仍执行各自的字段校验。
    update = chat._apply_orchestrator_actions(
        project, "lead",
        '<missioncrew-action>{"action":"save_rule","match":{"labels":["auth"]}}'
        '</missioncrew-action>'
        '<missioncrew-action>{"action":"save_skill","id":"bad-config",'
        '"enabled":"yes"}</missioncrew-action>',
        root_id=1, depth=0,
    )
    project = seeded.get_project("webshop")
    assert all(skill.id != "bad-config" for skill in project.skills)
    assert "不支持的动作: save_rule" in update and "enabled 必须是布尔值" in update

    msg = seeded.add_message("general", "human", "human", "@lead 生成配置", ["lead"])
    prompt = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "lead"),
        seeded.get_backend("std-1"), msg,
    ).prompt
    assert all(action in prompt for action in (
        "guideline.save", "skill.save", "document.publish"))
    assert "save_rule" not in prompt and "只提问或讨论时直接回答" in prompt
    assert "api-style:修改 API 时使用" in prompt
    assert "local-ci(本地 CI)" in prompt
    assert "## 现有 Runtime" in prompt and "std-1: adapter=" in prompt


def test_project_config_managers_are_full_pages_with_orchestrator_requests(seeded):
    client = _client(seeded)
    html = client.get("/").text
    js = client.get("/assets/js/project-configs.js").text
    router = client.get("/assets/js/router.js").text
    documents = client.get("/assets/js/documents.js").text
    boards = client.get("/assets/js/boards.js").text
    markdown = client.get("/assets/js/markdown.js").text
    viewer = client.get("/assets/js/viewer.js").text
    main = client.get("/assets/js/main.js").text

    for view in ("guidelines-view", "skills-view", "docs-view"):
        assert f'id="{view}"' in html
    assert 'id="rules-view"' not in html and 'id="sec-rules"' not in html
    assert 'id="config-chat"' in html
    assert 'id="config-chat-context"' in html
    assert 'id="config-chat-selection"' in html
    assert 'id="config-chat-thread"' in html
    assert 'id="config-chat-input"' in html
    assert 'id="config-chat-resize"' in html
    assert 'id="config-chat-toggle"' in html
    assert 'id="nav-board"' not in html
    assert 'id="sec-boards"' in html and 'id="board-list"' in html
    assert "content-topbar-actions" in html
    assert "skill-page-head" in html and "skill-editor-pane" in html
    assert "single-pane-editor" in html
    assert "篇目列表位于应用左侧栏" not in html
    assert 'id="guide-proj-label"' not in html
    assert 'id="docs-proj-label"' not in html
    assert 'id="docs-root"' not in html
    for removed in ("guideline-page-list", "skill-page-list", "rule-page-list",
                    "skill-file-list", "config-manager", "doc-tree", "docs-timeline",
                    "docs-layout"):
        assert removed not in html
    assert "config-generator" not in html
    assert "project-configs.js" in html
    assert "markdown.js" in html
    assert html.index("/assets/js/markdown.js") < html.index("/assets/js/viewer.js") \
        < html.index("/assets/js/project-configs.js")
    assert '"guidelines", "skills"' in router and '"rules"' not in router
    assert "projPanels()" in router and "builtin-badge" in router
    assert 'tab === "board" || tab === "custom"' in router
    assert "openFormDialog" not in js
    assert "uiPrompt" not in documents
    assert 'id="doc-new-path"' in documents
    assert 'id="doc-upload-input"' in html and "type=\"file\" multiple" in html
    assert "beginDocumentUpload" in documents and "uploadDocuments" in documents
    assert "/documents/upload?" in documents and 'overwrite: String(' in documents
    assert "documentDownloadUrl" in documents and "/documents/download/" in documents
    assert "不能在线编辑或比较版本" in viewer
    assert "documentSidebarHtml" in documents
    assert "const docExpanded = new Set()" in documents
    assert "const closed = !docExpanded.has(key)" in documents
    assert (
        "docExpanded.has(key) ? docExpanded.delete(key) : docExpanded.add(key)"
        in documents
    )
    assert "expandDocAncestors(docSelected)" in documents
    assert 'localStorage.getItem("mc.sideCollapsed") || "[]"' in router
    assert 'localStorage.setItem("mc.sideCollapsed"' in router
    # 文档库与准则共用的统一查看器：历史、对比、行内/左右 diff、行号、图片
    assert "createTextViewer" in viewer and "docViewer" in documents
    assert "版本历史" in viewer and "比较已选版本" in viewer and "最新" in viewer
    assert "/documents/compare" in documents and "/compare" in js
    assert "viewerInlineDiffHtml" in viewer and "viewerSplitDiffHtml" in viewer
    assert "compare-style" in viewer and "doc-diff-split" in viewer
    assert "lineNumberedTextHtml" in viewer and "doc-image" in viewer
    assert "viewer-dirty-badge" in viewer and "confirmDiscard" in viewer
    assert "documentSidebarHtml()" in router
    assert "sendConfigChat" in js and "pollConfigChat" in js
    assert "currentConfigDraft" in js and "configPageSnapshot" in js
    assert "stageConfigPage" in js and "captureConfigChatSelection" in js
    assert "startConfigChatResize" in js and "toggleConfigChatCollapsed" in js
    assert "CONFIG_CHAT_HEIGHT_KEY" in js and "CONFIG_CHAT_COLLAPSED_KEY" in js
    assert "guideline-markdown-preview" in js
    assert "createTextViewer" in js and "guidelineViewer" in js
    assert "guidelineViewer.confirmDiscard" in js
    assert "/history" in js and "恢复会写入一个新版本" in js
    assert "恢复此版本" in viewer
    assert "viewer-edit-preview" in viewer
    assert 'GUIDELINE_MARKDOWN_PLACEHOLDER = "---\\nname: \\ndescription: \\n---' in js
    assert "frontmatter_contract" in js and "current_draft.markdown" in js
    assert "current_page.content_path" in js and 'read_from: "current_page.content_path"' in js
    assert "current_page.document_path" in js
    assert "current_page.filename" in js and "current_page.resource_url" in js
    assert "text_snapshot_available: false" in js
    assert "snapshot.content === null" in js and "effectiveSelection" in js
    assert "MISSIONCREW_DOCUMENTS_DIR" in js and "content_base64" in js
    assert "docPaneContent" in documents
    assert 'docPaneContentType = "binary"' in documents
    assert "configChatSelection = null" in documents
    assert "original_name: selectedGuidelineName" in js
    assert all(old not in js for old in ('id="gf-id"', 'id="gf-title"', 'id="gf-summary"'))
    assert "roleBindingPicker" not in js and "role_ids" not in js
    assert "fileRefPicker" not in js and "runtime_instructions" not in js
    assert "line_start" in js and "selected_text" in js
    assert "context: { page_collaboration:" in js
    assert "content: request" in js
    assert "/content-channel" in js and "content_key: context.contentKey" in js
    assert "resolveConfigChatChannel" in js
    assert '"POST", `/api/projects/${encodeURIComponent(currentProject)}/content-channel`' in js
    assert "fetch(" in js and "response.status === 404" in js
    assert "只需回答，不要写入" in js
    assert "setInterval(pollConfigChat, 2000)" in main
    assert "openMarkdownDocumentLink" in documents
    assert "revealMissionCrewDocument" in documents
    assert "missionCrewResourceReference(raw)" in router
    assert 'missionCrewDocumentUrl(currentProject, docSelected || "")' in router
    assert "missionCrewDocumentReference" in markdown
    assert "missionCrewResourceReference" in markdown
    assert "openMissionCrewResourceLink" in markdown
    for resource_type in ("channels", "tasks", "dashboards", "guidelines", "skills"):
        assert f'"{resource_type}"' in router
    assert 'href="${esc(resource.url)}"' in markdown
    assert all(markup in markdown for markup in (
        "markdownInline", "<blockquote>", "<pre><code", "markdown-table-wrap"))
    assert "markdownPreviewHtml(markdown)" in js
    assert "markdownPreviewHtml(" in viewer
    assert "markdownPreviewHtml(c.markdown || c.text" in boards
    assert js.count("markdownPreviewHtml(") >= 2
    assert "markdownContentWithoutFrontmatter" not in js
    assert "skillMarkdownPreviewHtml" not in js
    assert "miniMarkdown(" not in js and "miniMarkdown(" not in documents
    assert "miniMarkdown(" not in boards
    assert all(action in js for action in (
        "guideline.save", "skill.save", "document.publish"))
    assert "save_rule" not in js and "验证规则" not in html


def test_binary_document_chat_context_keeps_file_identity_without_text_selection(seeded):
    client = _client(seeded)
    configs = client.get("/assets/js/project-configs.js").text
    documents = client.get("/assets/js/documents.js").text

    assert 'filename: documentPath.split("/").pop()' in configs
    assert "document_path: documentPath" in configs
    assert "resource_url: missionCrewDocumentUrl(currentProject, documentPath)" in configs
    assert 'if (docPaneContentType === "binary")' in configs
    assert "text_snapshot_available: false" in configs
    assert "if (snapshot.content === null) return snapshot.metadata" in configs
    assert "effectiveSelection = currentPage?.text_snapshot_available === false" in configs
    assert "? null : selection" in configs
    assert "没有 current_page.content_path、selection 或行号" in configs
    assert "非文本文件；主控将收到文件名与文档路径" in configs
    assert 'docPaneContentType = "binary"' in documents
    binary_branch = documents.index('docPaneContentType = "binary"')
    clear_selection = documents.index("configChatSelection = null", binary_branch)
    assert binary_branch < clear_selection


def test_guideline_and_document_chat_context_is_staged_as_a_file(seeded):
    client = _client(seeded)
    markdown = "---\nname: api-style\ndescription: API changes\n---\n\n# API\n"
    response = client.post("/api/chat/general/page-context", json={
        "page_kind": "guidelines", "page_key": "api-style.md", "content": markdown,
    })
    assert response.status_code == 200
    path = Path(response.json()["path"])
    assert path.read_text(encoding="utf-8") == markdown
    assert path.parent.name == "guidelines"
    assert path.parent.parent.name == "page-context"
    assert "/lead/.missioncrew/" in path.as_posix()

    same = client.post("/api/chat/general/page-context", json={
        "page_kind": "guidelines", "page_key": "api-style.md", "content": markdown,
    })
    assert same.json()["path"] == str(path)
    document = client.post("/api/chat/general/page-context", json={
        "page_kind": "docs", "page_key": "specs/design.md", "content": "# Draft\n",
    })
    document_path = Path(document.json()["path"])
    assert document_path.read_text(encoding="utf-8") == "# Draft\n"
    assert document_path.parent.name == "docs" and document_path.suffix == ".md"
    invalid = client.post("/api/chat/general/page-context", json={
        "page_kind": "skills", "page_key": "SKILL.md", "content": "secret",
    })
    assert invalid.status_code == 400


def test_background_refresh_preserves_scrollable_view_state(seeded):
    client = _client(seeded)
    ui = client.get("/assets/js/ui.js").text
    router = client.get("/assets/js/router.js").text
    configs = client.get("/assets/js/project-configs.js").text
    documents = client.get("/assets/js/documents.js").text
    boards = client.get("/assets/js/boards.js").text
    tasks = client.get("/assets/js/tasks.js").text

    for helper in ("captureScrollPositions", "restoreScrollPositions",
                   "captureKeyedScrollPositions", "restoreKeyedScrollPositions",
                   "isNearScrollBottom"):
        assert f"function {helper}" in ui

    assert "signature !== guidelineEditorSignature" in configs
    assert "signature !== skillEditorSignature" in configs
    assert 'root.dataset.itemKey === itemKey' in configs
    assert '"#skill-markdown-preview"' in configs
    assert '".skill-file-viewer-body"' in configs
    assert "appendMessagesToSurface(pending" in configs
    assert "syncRuns(thread.runs" in configs
    assert "root.dataset.contextKey !== context.key" in configs
    assert "thread.lastRenderedId" in configs

    assert "renderDocuments(true)" in router
    assert "signature !== docPaneRenderSignature" in documents
    viewer = client.get("/assets/js/viewer.js").text
    assert 'scrollSelectors: () => ["#doc-pane"]' in documents
    assert "captureScrollPositions(config.scrollSelectors())" in viewer
    assert "token !== V.renderToken" in viewer

    assert "signature === customBoardRenderSignature" in boards
    assert "boardHasLiveWidgets(board)" in boards
    assert 'data-scroll-key="widget:' in boards
    assert "restoreKeyedScrollPositions(preview, scrollState)" in boards
    assert 'captureScrollPositions(["#side-scroll"])' in router
    assert 'captureScrollPositions(["#board-view"])' in tasks


# ---- 文档库:恢复 / 软链可达性 / 二进制读取 / 审计 ----

def test_document_restore_creates_new_version(seeded):
    client = _client(seeded)
    url = "/api/projects/webshop/documents/file/notes/plan.md"
    client.put(url, json={"content": "v1", "actor": "alice"})
    client.put(url, json={"content": "v2", "actor": "bob"})
    history = client.get(
        "/api/projects/webshop/documents/history?path=notes%2Fplan.md").json()
    restored = client.post("/api/projects/webshop/documents/restore", json={
        "path": "notes/plan.md", "revision": history[1]["revision"]})
    assert restored.status_code == 200
    assert client.get(url).json()["content"] == "v1"     # 内容回到 v1
    new_history = client.get(
        "/api/projects/webshop/documents/history?path=notes%2Fplan.md").json()
    assert len(new_history) == 3                          # 历史完整保留
    assert "Restore" in new_history[0]["message"]
    # 不存在的版本 -> 404
    assert client.post("/api/projects/webshop/documents/restore", json={
        "path": "notes/plan.md", "revision": "deadbeef00"}).status_code == 404


def test_harness_workspace_contains_documents_without_polluting_source_workdir(seeded):
    """文档统一从独立 .missioncrew 访问，频道源码目录不产生平台文件。"""
    from missioncrew.core.config import mc_home
    chat = ChatEngine(seeded)
    msg = seeded.add_message("general", "human", "human", "@dev 干活", ["dev"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "dev"),
                         seeded.get_backend("std-1"), msg)
    workspace = Path(cfg.env["MISSIONCREW_WORKSPACE"])
    link = workspace / "documents"
    assert workspace.name == ".missioncrew"
    assert link.is_symlink()
    assert link.resolve() == library_for("webshop").root.resolve()
    assert Path(cfg.env["MISSIONCREW_DOCUMENTS_DIR"]) == workspace / "documents"
    assert not (workspace / "docs").exists()
    assert "MissionCrew 是本地多 Agent harness" in cfg.prompt
    assert "不会进入业务源码或业务代码提交" in cfg.prompt
    assert "协作草稿、报告和普通聊天产生的验证记录写入 `documents/`" in cfg.prompt
    assert "Task 包含标题、简介、正文、状态" in cfg.prompt
    assert "快照对当前执行只读" in cfg.prompt
    assert "包括 `/tmp`、`/var/tmp`" in cfg.prompt
    assert str(workspace / "temp") in cfg.prompt
    assert "MissionCrew 注入的项目 Skill 是额外能力" in cfg.prompt
    assert "不要把 `MISSIONCREW_SKILLS_DIR` 当作唯一 Skill 来源" in cfg.prompt
    assert "`.agent/skills`、`.agents/skills`、`.claude/skills`" in cfg.prompt
    assert "MissionCrew 是一个本地 Agent harness" in (
        workspace / "README.md").read_text(encoding="utf-8")
    assert "Shell 重定向、后台日志和工具自动生成" in (
        workspace / "README.md").read_text(encoding="utf-8")
    assert (workspace / "project.md").is_file()
    assert (workspace / "tasks").is_dir()
    assert (workspace / "guidelines").is_dir()
    assert (workspace / "skills" / "webshop-local-ci" / "SKILL.md").is_file()
    assert Path(cfg.env["MISSIONCREW_CHANNEL_HISTORY"]).parent == workspace
    assert Path(cfg.env["MISSIONCREW_TASKS_DIR"]) == workspace / "tasks"
    assert not (Path(cfg.workdir) / ".missioncrew").exists()
    # 指定外部代码仓时，平台入口仍只建在自己的数据根中。
    from missioncrew.core.models import Channel
    ext = mc_home() / "ext-repo"
    ext.mkdir(parents=True, exist_ok=True)
    seeded.put_channel(Channel(id="webshop:ext", name="ext", project_id="webshop",
                               workdir=str(ext)))
    msg2 = seeded.add_message("webshop:ext", "human", "human", "@dev 干活", ["dev"])
    ext_cfg = chat._assemble(seeded.get_channel("webshop:ext"),
                             seeded.get_role("webshop", "dev"),
                             seeded.get_backend("std-1"), msg2)
    assert not (ext / ".missioncrew").exists()
    assert Path(ext_cfg.env["MISSIONCREW_WORKSPACE"]).name == ".missioncrew"


def test_legacy_runtime_files_migrate_under_harness_directories(seeded):
    from missioncrew.core.config import mc_home

    home = mc_home()
    history = home / "channel-history" / "general" / "history"
    history.mkdir(parents=True)
    (history / "channel-history.json").write_text("{}", encoding="utf-8")
    channel_dir = home / "channels" / "general"
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "documents").symlink_to(library_for("webshop").root,
                                             target_is_directory=True)
    (channel_dir / ".mc_last_output_codex.log").write_text("old log")
    task_dir = home / "workspaces" / "legacy-task"
    evidence = task_dir / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "manifest.json").write_text(
        '[{"type":"plan","path":"evidence/plan.md"}]', encoding="utf-8")
    agent_harness = (home / "agent-workspaces" / "webshop" / "channels"
                     / "general" / "dev" / ".missioncrew")
    agent_harness.mkdir(parents=True)
    (agent_harness / "docs").symlink_to(library_for("webshop").root,
                                         target_is_directory=True)
    (agent_harness / "guidelines").mkdir()
    (agent_harness / "guidelines" / "stale.md").write_text("stale")
    (agent_harness / "skills").mkdir()
    (agent_harness / "skills" / "stale").mkdir()

    assert migrate_legacy_workspace_layout() == 5
    assert not (home / "channel-history").exists()
    assert not (channel_dir / "documents").exists()
    assert not (channel_dir / ".mc_last_output_codex.log").exists()
    assert (home / "agent-workspaces" / "_legacy" / "channels" / "general"
            / "_platform" / ".missioncrew" / "runtime"
            / "last-output-codex.log").read_text() == "old log"
    assert (task_dir / ".missioncrew" / "evidence" / "manifest.json").is_file()
    assert json.loads((task_dir / ".missioncrew" / "evidence" / "manifest.json")
                      .read_text())[0]["path"] == ".missioncrew/evidence/plan.md"
    assert not (task_dir / "evidence").exists()
    assert not (agent_harness / "docs").exists()
    assert (agent_harness / "documents").is_symlink()
    assert (agent_harness / "documents").resolve() \
        == library_for("webshop").root.resolve()
    assert migrate_legacy_workspace_layout() == 0
    assert migrate_resource_workspace_links(seeded) == 2
    assert (agent_harness / "guidelines").is_symlink()
    assert (agent_harness / "guidelines").resolve() \
        == guideline_context_dir(seeded.get_project("webshop"))
    assert not (agent_harness / "guidelines" / "stale.md").exists()
    assert (agent_harness / "skills").is_symlink()
    assert (agent_harness / "skills").resolve() \
        == skill_context_dir(seeded.get_project("webshop"))
    assert not (agent_harness / "skills" / "stale").exists()
    assert migrate_resource_workspace_links(seeded) == 0


def test_binary_document_read_returns_415(seeded):
    client = _client(seeded)
    library = library_for("webshop")
    (library.root / "image.bin").write_bytes(b"\x89PNG\x00\xff\xfe binary")
    library.commit_changes("human", "add binary")
    r = client.get("/api/projects/webshop/documents/file/image.bin")
    assert r.status_code == 415

    first = library.write_bytes(
        "control.bin", b"\x00first", actor="alice", overwrite=True)
    second = library.write_bytes(
        "control.bin", b"\x00second", actor="bob", overwrite=True)
    compared = client.post("/api/projects/webshop/documents/compare", json={
        "path": "control.bin", "from_revision": first, "to_revision": second,
    })
    assert compared.status_code == 415
    assert compared.json()["detail"] == "二进制或非 UTF-8 文件不能比较版本"


def test_document_upload_preserves_bytes_versions_and_conflict_safety(seeded):
    client = _client(seeded)
    upload_url = "/api/projects/webshop/documents/upload"
    first_content = b"\x89PNG\x00\xff\xfe first"
    first = client.post(
        upload_url, params={"path": "assets/sample.png"}, content=first_content)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["path"] == "assets/sample.png"
    assert first_body["size"] == len(first_content)
    assert first_body["resource_url"] == \
        "/resources/webshop/documents/assets/sample.png"
    assert library_for("webshop").read_bytes("assets/sample.png") == first_content

    conflict = client.post(
        upload_url, params={"path": "assets/sample.png"}, content=b"rejected")
    assert conflict.status_code == 409
    assert library_for("webshop").read_bytes("assets/sample.png") == first_content

    second_content = b"\x89PNG\x00 second"
    second = client.post(
        upload_url,
        params={"path": "assets/sample.png", "overwrite": "true"},
        content=second_content,
    )
    assert second.status_code == 200
    assert second.json()["revision"] != first_body["revision"]

    latest = client.get(
        "/api/projects/webshop/documents/download/assets/sample.png")
    assert latest.status_code == 200 and latest.content == second_content
    assert latest.headers["content-type"].startswith("image/png")
    assert "filename*=UTF-8''sample.png" in latest.headers["content-disposition"]
    assert latest.headers["x-content-type-options"] == "nosniff"
    old = client.get(
        "/api/projects/webshop/documents/download/assets/sample.png",
        params={"revision": first_body["revision"]},
    )
    assert old.status_code == 200 and old.content == first_content
    restored = client.post("/api/projects/webshop/documents/restore", json={
        "path": "assets/sample.png",
        "revision": first_body["revision"],
        "actor": "alice",
    })
    assert restored.status_code == 200
    assert library_for("webshop").read_bytes("assets/sample.png") == first_content

    text = client.post(
        upload_url, params={"path": "assets/readme.md"}, content=b"# Uploaded\n")
    assert text.status_code == 200
    read = client.get(
        "/api/projects/webshop/documents/file/assets/readme.md").json()
    assert read["content"] == "# Uploaded\n"
    assert client.post(
        upload_url, params={"path": "../escape.bin"}, content=b"no").status_code == 400
    assert client.post(
        upload_url, params={"path": "assets"}, content=b"no").status_code == 400
    audits = [row for row in seeded.list_audit(limit=50)
              if row["action"] == "document_uploaded"]
    assert len(audits) == 3
    assert all("project=webshop" in row["detail"] for row in audits)


def test_document_upload_enforces_size_limit(seeded, monkeypatch):
    monkeypatch.setattr("missioncrew.api.documents.MAX_DOCUMENT_UPLOAD_BYTES", 3)
    response = _client(seeded).post(
        "/api/projects/webshop/documents/upload",
        params={"path": "too-large.bin"}, content=b"1234",
    )
    assert response.status_code == 413
    assert not (library_for("webshop").root / "too-large.bin").exists()


def test_agent_document_writes_are_audited(seeded):
    """Agent 执行期间写文档库:执行后自动提交、归属该角色并进平台审计。"""
    chat = ChatEngine(seeded)
    chat.post("general", "human", "@[dev] [写文档] 记录一下")
    chat.wait_idle()
    library = library_for("webshop")
    assert (library.root / "mock-note.md").exists()
    assert library.history("mock-note.md")[0]["actor"] == "role:dev"
    audits = [a for a in seeded.list_audit(limit=50)
              if a["action"] == "documents_committed"]
    assert audits and audits[0]["actor"] == "role:dev"


def test_task_workspace_direct_edits_are_discarded_after_chat_run(
        seeded, monkeypatch):
    """Task 快照只读；只有显式 Agent Tool 调用可以修改事实源。"""
    from missioncrew.collab.tasks import create_task

    task = create_task(
        seeded, "webshop", title="原任务", body="原描述",
        channel_ids=["general"])
    edited_paths = []

    def _start(config):
        if config.role_id == "dev":
            tasks_dir = Path(config.env["MISSIONCREW_TASKS_DIR"])
            task_file = tasks_dir / f"{task.id}.md"
            task_file.write_text(
                task_file.read_text(encoding="utf-8").replace(
                    "title: 原任务", "title: 未授权修改", 1),
                encoding="utf-8",
            )
            (tasks_dir / "new-task.md").write_text(
                "---\ntitle: 未授权新建\n---\n", encoding="utf-8")
            edited_paths.append((task_file, tasks_dir / "new-task.md"))
        return RunResult(True, "ok", output="已完成检查。")

    monkeypatch.setattr(runtime_manager, "start", _start)
    chat = ChatEngine(seeded)
    chat.post("general", "human", "@[dev] 检查任务。")
    chat.wait_idle()
    assert edited_paths
    task_file, untracked_file = edited_paths[0]
    assert seeded.get_task(task.id).title == "原任务"
    assert "title: 原任务" in task_file.read_text(encoding="utf-8")
    assert untracked_file.exists()
    assert not any(
        item.title == "未授权新建" for item in seeded.list_tasks()
    )
    assert all(
        "任务文件同步失败" not in row["content"]
        for row in seeded.list_messages("general")
    )


def test_task_snapshot_refresh_prunes_deleted_task_but_keeps_untracked_files(
        seeded, tmp_path):
    from missioncrew.collab.tasks import create_task

    tasks_dir = tmp_path / "tasks"
    task = create_task(
        seeded, "webshop", title="待删除快照", channel_ids=["general"])
    write_task_files(seeded, "webshop", tasks_dir)
    snapshot = tasks_dir / f"{task.id}.md"
    untracked = tasks_dir / "untracked.md"
    untracked.write_text("# 不是平台 Task\n", encoding="utf-8")
    assert snapshot.is_file()

    seeded.delete_task(task.id)
    write_task_files(seeded, "webshop", tasks_dir)

    assert not snapshot.exists()
    assert untracked.exists()


# ---- 面板卡片:通用展示原语 + 平台数据源(AgentDesk 式) ----

def test_widget_data_resolves_tasks_source(seeded):
    client = _client(seeded)
    from missioncrew.collab.tasks import create_task
    create_task(seeded, "webshop", title="支付重构", labels=["pay"],
                channel_ids=["general"])
    create_task(seeded, "webshop", title="修购物车", labels=["bug"],
                channel_ids=["general"])
    resolved = client.post("/api/projects/webshop/widget_data", json={"widgets": [
        {"id": "w1", "type": "table",
         "content": {"source": {"from": "tasks", "labels": ["bug"]}}},
    ]}).json()
    rows = resolved["w1"]["rows"]
    assert len(rows) == 1 and rows[0]["标题"] == "修购物车"


def test_widget_data_resolves_document_and_messages(seeded):
    client = _client(seeded)
    client.put("/api/projects/webshop/documents/file/notes/status.md",
               json={"content": "# 状态\n一切正常\n"})
    seeded.add_message("general", "human", "human", "进展同步:一切顺利", [])
    resolved = client.post("/api/projects/webshop/widget_data", json={"widgets": [
        {"id": "doc", "type": "markdown",
         "content": {"source": {"from": "document", "path": "notes/status.md"}}},
        {"id": "msgs", "type": "list",
         "content": {"source": {"from": "messages", "channel": "general", "limit": 5}}},
        {"id": "bad", "type": "markdown",
         "content": {"source": {"from": "document", "path": "ghost.md"}}},
    ]}).json()
    assert "一切正常" in resolved["doc"]["markdown"]
    assert any("一切顺利" in item["text"] for item in resolved["msgs"]["items"])
    assert "error" in resolved["bad"]          # 坏源返回错误说明而不是 500


def test_widget_data_skips_static_widgets(seeded):
    client = _client(seeded)
    resolved = client.post("/api/projects/webshop/widget_data", json={"widgets": [
        {"id": "s", "type": "markdown", "content": {"markdown": "静态"}},
    ]}).json()
    assert resolved == {}


def test_widget_types_are_display_primitives(seeded):
    from missioncrew.core.models import BOARD_WIDGET_TYPES
    assert BOARD_WIDGET_TYPES == {"markdown", "table", "card", "chart",
                                  "list", "log", "code"}
    # 旧领域类型已彻底移除,未知类型在 API 与聊天动作两条链路都被拒绝
    client = _client(seeded)
    bad = client.post("/api/projects/webshop/boards", json={
        "id": "legacy", "name": "旧", "layout": [{
            "id": "w", "type": "requirements", "title": "x",
            "x": 0, "y": 0, "width": 6, "height": 4, "content": {}}]})
    assert bad.status_code == 400 and "未知组件类型" in bad.json()["detail"]
    chat = ChatEngine(seeded)
    reply = chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"create_board","id":"old","name":"O",'
        '"layout":[{"id":"w","type":"task_query","title":"t",'
        '"x":0,"y":0,"width":6,"height":4}]}</missioncrew-action>',
        root_id=1, depth=0)
    assert "未知组件类型" in reply and seeded.get_board("webshop:old") is None


# ---- 项目资源:本地路径 / git 仓自动绑远程 / 迁移 ----

def test_resource_local_git_repo_binds_remote(seeded, tmp_path):
    import subprocess
    client = _client(seeded)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://example.com/team/myrepo.git"], check=True)
    r = client.post("/api/projects/webshop/resources",
                    json={"target": str(repo)}).json()
    assert r["kind"] == "git"                      # 本地 git 仓自动识别
    assert r["remote"] == "https://example.com/team/myrepo.git"  # 自动绑定远程
    assert r["path"] == str(repo)


def test_resource_plain_path_url_and_dedup(seeded, tmp_path):
    client = _client(seeded)
    plain = tmp_path / "assets"
    plain.mkdir()
    r1 = client.post("/api/projects/webshop/resources",
                     json={"target": str(plain)}).json()
    assert r1["kind"] == "path" and r1["remote"] == ""
    r2 = client.post("/api/projects/webshop/resources",
                     json={"target": "https://github.com/acme/widget.git"}).json()
    assert r2["kind"] == "git" and r2["path"] == "" and r2["id"] == "widget"
    r3 = client.post("/api/projects/webshop/resources",
                     json={"target": str(plain)}).json()
    assert r3["id"] == "assets-2"                  # 同名资源自动加序号
    assert client.post("/api/projects/webshop/resources",
                       json={"target": "/nonexistent/dir"}).status_code == 400
    assert client.delete("/api/projects/webshop/resources/assets").status_code == 200
    assert client.delete("/api/projects/webshop/resources/ghost").status_code == 404


def test_dev_guidelines_migrated_into_guideline_doc(seeded):
    from missioncrew.core import seed as seed_mod
    p = seeded.get_project("webshop")
    p.dev_guidelines = "旧开发准则内容"
    seeded.put_project(p)
    assert seed_mod.migrate_project_fields(seeded) == 1
    p2 = seeded.get_project("webshop")
    assert p2.dev_guidelines == ""
    doc = next(g for g in p2.guidelines if g.name == "dev-guidelines")
    assert "旧开发准则内容" in doc.content
    assert doc.description == "项目开发中的架构、代码与变更约束。"
    assert seed_mod.migrate_project_fields(seeded) == 0   # 幂等


def test_legacy_default_chain_budget_migrates_to_one_hundred(seeded):
    from missioncrew.core import seed as seed_mod

    project = seeded.get_project("webshop")
    project.dev_guidelines = ""
    project.max_chain_runs = 20
    seeded.put_project(project)

    assert seed_mod.migrate_project_fields(seeded) == 1
    assert seeded.get_project("webshop").max_chain_runs == 100
    assert seed_mod.migrate_project_fields(seeded) == 0
    project = seeded.get_project("webshop")
    project.max_chain_runs = 50
    seeded.put_project(project)
    assert seed_mod.migrate_project_fields(seeded) == 0
    assert seeded.get_project("webshop").max_chain_runs == 50


def test_fs_dirs_endpoint(seeded, tmp_path):
    client = _client(seeded)
    root = tmp_path / "browse"
    (root / "sub-a").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("x")
    d = client.get(f"/api/fs/dirs?path={root}").json()
    assert d["dirs"] == ["sub-a"]            # 隐藏目录与文件默认不列出
    assert d["parent"] == str(root.parent)
    assert d["is_git"] is False
    shown = client.get(f"/api/fs/dirs?path={root}&hidden=true").json()
    assert shown["dirs"] == [".hidden", "sub-a"]   # 开启后包含隐藏目录
    # 缺省从用户主目录开始;非目录路径报 400
    assert client.get("/api/fs/dirs").status_code == 200
    assert client.get(f"/api/fs/dirs?path={root}/file.txt").status_code == 400


def test_fs_dirs_git_remotes_listed(seeded, tmp_path):
    import subprocess
    client = _client(seeded)
    repo = tmp_path / "multi-remote"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "upstream",
                    "https://example.com/up/multi.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://example.com/me/multi.git"], check=True)
    d = client.get(f"/api/fs/dirs?path={repo}").json()
    assert d["is_git"] is True
    remotes = {r["name"]: r["url"] for r in d["remotes"]}
    assert remotes == {"origin": "https://example.com/me/multi.git",
                       "upstream": "https://example.com/up/multi.git"}
    # 资源绑定:有 origin 时优先 origin
    r = client.post("/api/projects/webshop/resources",
                    json={"target": str(repo)}).json()
    assert r["remote"] == "https://example.com/me/multi.git"


def test_resource_binding_without_origin_uses_first_remote(seeded, tmp_path):
    import subprocess
    client = _client(seeded)
    repo = tmp_path / "no-origin"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "mirror",
                    "https://example.com/mirror/x.git"], check=True)
    r = client.post("/api/projects/webshop/resources",
                    json={"target": str(repo)}).json()
    assert r["kind"] == "git"
    assert r["remote"] == "https://example.com/mirror/x.git"


def test_resource_refresh_rebinds_git_remote(seeded, tmp_path):
    import subprocess
    client = _client(seeded)
    repo = tmp_path / "later-git"
    repo.mkdir()
    r = client.post("/api/projects/webshop/resources",
                    json={"target": str(repo)}).json()
    assert r["kind"] == "path"                      # 添加时还是普通目录
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://example.com/late/binding.git"], check=True)
    refreshed = client.post(
        f"/api/projects/webshop/resources/{r['id']}/refresh").json()
    assert refreshed["kind"] == "git"               # 刷新后识别为 git 仓
    assert refreshed["remote"] == "https://example.com/late/binding.git"
    # 纯远程资源无本地路径,刷新返回 400
    url_res = client.post("/api/projects/webshop/resources",
                          json={"target": "https://github.com/acme/pure.git"}).json()
    assert client.post(
        f"/api/projects/webshop/resources/{url_res['id']}/refresh").status_code == 400
