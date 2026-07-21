"""项目主控、频道、文档库、自定义面板和结构化上下文的集成测试。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.runtime import adapters
from missioncrew.taskflow import assembler
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.documents import library_for
from missioncrew.collab.project_context import guideline_context_dir
from missioncrew.collab.skills import (materialize_project_skills,
                                       project_skill_library_dir)
from missioncrew.collab.workspace import (migrate_legacy_workspace_layout,
                                          sync_task_files)
from missioncrew.core.models import (DEFAULT_MAX_CHAIN_RUNS, Backend,
                                     ExecutionConfig, GuidelineDocument, ProjectResource,
                                     ProjectSkill, Task, TaskStage)
from missioncrew.api import create_app


def _client(seeded):
    return TestClient(create_app())


def test_project_has_one_configurable_orchestrator_and_protects_it(seeded):
    client = _client(seeded)
    project = next(p for p in client.get("/api/overview").json()["projects"]
                   if p["id"] == "webshop")
    assert project["orchestrator_role_id"] == "lead"
    assert project["max_chain_runs"] == DEFAULT_MAX_CHAIN_RUNS == 20

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
    old = client.get(url + f"?revision={history[1]['revision']}").json()
    assert old["content"] == "# Checkout v1\n"
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
    guideline_dir = Path(chat_cfg.env["MISSIONCREW_GUIDELINES_DIR"])
    dev_guideline = guideline_dir / "dev-guide.md"
    tester_guideline = guideline_dir / "tester-guide.md"
    assert str(guideline_dir) in chat_cfg.prompt
    assert str(dev_guideline) in chat_cfg.prompt
    assert str(guideline_dir.parent) in chat_cfg.allowed_dirs
    assert not (guideline_dir / "disabled-guide.md").exists()
    assert dev_guideline.read_text() == (
        "---\nname: dev-guide\ndescription: 开发代码或 API 时使用\n---\n\n"
        "开发相关任务准则\n")
    assert tester_guideline.read_text().endswith("\n测试相关任务准则\n")
    (guideline_dir / "stale.md").write_text("stale")
    (guideline_dir.parent / "guidelines.json").write_text("{}")

    task = Task(id="t_context", project_id="webshop", title="context")
    task_cfg = assembler.assemble(
        task, TaskStage(name="develop"), project, seeded.get_backend("exp-1"),
        {}, [], [], seeded,
    )
    assert "common" in task_cfg.prompt
    assert "expert-only" in task_cfg.prompt and "checkout-dev" in task_cfg.prompt
    assert "开发代码或 API 时使用" in task_cfg.prompt
    assert "开发相关任务准则" not in task_cfg.prompt
    assert "结合当前任务自行判断哪些条目适用" in task_cfg.prompt
    assert task_cfg.env["MISSIONCREW_DOCUMENTS_DIR"] in task_cfg.prompt
    assert task_cfg.env["MISSIONCREW_GUIDELINES_DIR"] in task_cfg.prompt
    task_guideline_dir = Path(task_cfg.env["MISSIONCREW_GUIDELINES_DIR"])
    assert task_guideline_dir != guideline_dir
    assert not (task_guideline_dir / "stale.md").exists()

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


def test_sandboxed_cli_commands_allow_the_document_library():
    docs = "/mc/projects/demo/documents"
    codex = adapters.render_command(
        adapters.DEFAULT_COMMANDS["codex"], "work", "gpt-test", docs)
    claude = adapters.render_command(
        adapters.DEFAULT_COMMANDS["claude_code"], "work", "sonnet", docs)
    assert codex[codex.index("--add-dir") + 1] == docs
    assert claude[claude.index("--add-dir") + 1] == docs


def test_all_project_directories_are_assembled_for_chat_and_tasks(seeded, tmp_path):
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
    task_cfg = assembler.assemble(
        Task(id="multi_repo", project_id="webshop", title="multi"),
        TaskStage(name="develop"), project, seeded.get_backend("std-1"), {}, [], [],
        seeded,
    )

    shared = [str(repo_a.resolve()), str(repo_b.resolve()), str(library.root.resolve())]
    skill_root = str(project_skill_library_dir("webshop").resolve())
    chat_workspace = str(Path(chat_cfg.env["MISSIONCREW_WORKSPACE"]).resolve())
    task_workspace = str(Path(task_cfg.env["MISSIONCREW_WORKSPACE"]).resolve())
    assert task_cfg.allowed_dirs == [*shared, task_workspace, skill_root]
    assert chat_cfg.allowed_dirs == [*shared, chat_workspace, skill_root]
    assert all(path in chat_cfg.prompt and path in task_cfg.prompt for path in shared)
    assert chat_workspace in chat_cfg.prompt and task_workspace in task_cfg.prompt


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
    assert rendered["grok_build"][rendered["grok_build"].index("--cwd") + 1] == workdir
    assert rendered["opencode"][rendered["opencode"].index("--dir") + 1] == workdir
    assert "--force" in rendered["cursor"]
    assert rendered["pi"][:2] == ["pi", "-p"]

    acp_commands = {
        name: adapters.render_command(template, "", "", allowed_dirs=dirs)
        for name, template in adapters.ACP_SERVE_COMMANDS.items()
    }
    for name in ("kimi", "qoder", "trae"):
        assert acp_commands[name].count("--add-dir") == len(dirs)
        assert all(path in acp_commands[name] for path in dirs)
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
    """主控建频道 + post_message 派工:被 @ 的角色在新频道真实执行。"""
    chat = ChatEngine(seeded)
    root = seeded.add_message("general", "human", "human", "@lead 开新任务", ["lead"])
    chat._apply_orchestrator_actions(
        seeded.get_project("webshop"), "lead",
        '<missioncrew-action>{"action":"create_channel","id":"pay",'
        '"name":"支付任务","purpose":"支付重构"}</missioncrew-action>'
        '<missioncrew-action>{"action":"post_message","channel":"pay",'
        '"content":"@dev 请实现支付重构,验收标准见频道用途。"}</missioncrew-action>',
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
              '@dev 试试越权 <missioncrew-action>{"action":"create_board",'
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
    client.post("/api/projects/webshop/boards",
                json={"id": "quality", "name": "质量面板", "layout": []})
    msg = seeded.add_message("general", "human", "human", "@lead 看看", ["lead"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "lead"),
                         seeded.get_backend("std-1"), msg)
    assert "## 现有频道" in cfg.prompt and "general" in cfg.prompt
    assert "## 现有面板" in cfg.prompt and "quality" in cfg.prompt
    assert "协作链预算" in cfg.prompt and "post_message" in cfg.prompt


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
    assert "已保存准则文档" in reply and "已保存文档 specs/generated.md" in reply

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
        "save_guideline", "save_skill", "write_document"))
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
    assert html.index("/assets/js/markdown.js") < html.index("/assets/js/project-configs.js")
    assert '"guidelines", "skills"' in router and '"rules"' not in router
    assert "projPanels()" in router and "builtin-badge" in router
    assert 'tab === "board" || tab === "custom"' in router
    assert "openFormDialog" not in js
    assert "uiPrompt" not in documents
    assert 'id="doc-new-path"' in documents
    assert "documentSidebarHtml" in documents
    assert "版本历史" in documents
    assert "documentSidebarHtml()" in router
    assert "sendConfigChat" in js and "pollConfigChat" in js
    assert "currentConfigDraft" in js and "captureConfigChatSelection" in js
    assert "startConfigChatResize" in js and "toggleConfigChatCollapsed" in js
    assert "CONFIG_CHAT_HEIGHT_KEY" in js and "CONFIG_CHAT_COLLAPSED_KEY" in js
    assert "guideline-markdown-preview markdown-body" in js
    assert "setGuidelineMarkdownMode" in js
    assert 'GUIDELINE_MARKDOWN_PLACEHOLDER = "---\\nname: \\ndescription: \\n---' in js
    assert "frontmatter_contract" in js and "current_draft.markdown" in js
    assert "original_name: selectedGuidelineName" in js
    assert all(old not in js for old in ('id="gf-id"', 'id="gf-title"', 'id="gf-summary"'))
    assert "roleBindingPicker" not in js and "role_ids" not in js
    assert "fileRefPicker" not in js and "runtime_instructions" not in js
    assert "line_start" in js and "selected_text" in js
    assert 'replace(/@/g, "\\\\u0040")' in js
    assert "只需回答，不要写入" in js
    assert "setInterval(pollConfigChat, 2000)" in main
    assert "openMarkdownDocumentLink" in documents
    assert all(markup in markdown for markup in (
        "markdownInline", "<blockquote>", "<pre><code", "markdown-table-wrap"))
    assert "markdownPreviewHtml(markdown)" in js
    assert "markdownPreviewHtml(d.content)" in documents
    assert "markdownPreviewHtml(c.markdown || c.text" in boards
    assert js.count("markdownPreviewHtml(") >= 3
    assert "markdownContentWithoutFrontmatter" not in js
    assert "skillMarkdownPreviewHtml" not in js
    assert "miniMarkdown(" not in js and "miniMarkdown(" not in documents
    assert "miniMarkdown(" not in boards
    assert all(action in js for action in (
        "save_guideline", "save_skill", "write_document"))
    assert "save_rule" not in js and "验证规则" not in html


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
    assert "isNearScrollBottom(root)" in configs
    assert "root.dataset.renderKey === renderKey" in configs

    assert "renderDocuments(true)" in router
    assert "signature !== docPaneRenderSignature" in documents
    assert 'captureScrollPositions(["#doc-pane"])' in documents
    assert "renderToken !== docPaneRenderToken" in documents

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
    assert "MissionCrew 是一个本地 Agent harness" in (
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
    seeded.add_evidence("legacy-task", "develop", "plan", "evidence/plan.md")
    agent_harness = (home / "agent-workspaces" / "webshop" / "channels"
                     / "general" / "dev" / ".missioncrew")
    agent_harness.mkdir(parents=True)
    (agent_harness / "docs").symlink_to(library_for("webshop").root,
                                         target_is_directory=True)

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
    assert seeded._migrate_harness_paths() == 1
    assert seeded.list_evidence("legacy-task")[0]["path"] \
        == ".missioncrew/evidence/plan.md"
    assert not (agent_harness / "docs").exists()
    assert (agent_harness / "documents").is_symlink()
    assert (agent_harness / "documents").resolve() \
        == library_for("webshop").root.resolve()
    assert migrate_legacy_workspace_layout() == 0


def test_binary_document_read_returns_415(seeded):
    client = _client(seeded)
    library = library_for("webshop")
    (library.root / "image.bin").write_bytes(b"\x89PNG\x00\xff\xfe binary")
    library.commit_changes("human", "add binary")
    r = client.get("/api/projects/webshop/documents/file/image.bin")
    assert r.status_code == 415


def test_agent_document_writes_are_audited(seeded):
    """Agent 执行期间写文档库:执行后自动提交、归属该角色并进平台审计。"""
    chat = ChatEngine(seeded)
    chat.post("general", "human", "@dev [写文档] 记录一下")
    chat.wait_idle()
    library = library_for("webshop")
    assert (library.root / "mock-note.md").exists()
    assert library.history("mock-note.md")[0]["actor"] == "role:dev"
    audits = [a for a in seeded.list_audit(limit=50)
              if a["action"] == "documents_committed"]
    assert audits and audits[0]["actor"] == "role:dev"


def test_agents_can_create_and_edit_tasks_through_harness_workspace(seeded):
    """任务 Markdown 不是只读副本：聊天执行后会创建/更新数据库任务。"""
    from missioncrew.taskflow.engine import Engine

    chat = ChatEngine(seeded)
    chat.post("general", "human", "@dev [写任务] 新增后续工作")
    chat.wait_idle()
    created = next(task for task in seeded.list_tasks()
                   if task.title == "Agent 创建的任务")
    assert created.project_id == "webshop"
    assert created.task_type == "chore" and created.labels == ["workspace"]
    assert any(row["action"] == "task_workspace_created"
               and row["task_id"] == created.id
               for row in seeded.list_audit(limit=50))

    existing = Engine(seeded).create_task("webshop", "原任务", "原描述")
    message = seeded.add_message("general", "human", "human", "@dev 编辑任务", ["dev"])
    cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
        seeded.get_backend("std-1"), message)
    tasks_dir = Path(cfg.env["MISSIONCREW_TASKS_DIR"])
    task_file = tasks_dir / f"{existing.id}.md"
    markdown = task_file.read_text(encoding="utf-8")
    markdown = markdown.replace("title: 原任务", "title: 更新后的任务", 1)
    markdown = markdown.replace("\n原描述\n", "\n更新后的描述\n", 1)
    task_file.write_text(markdown, encoding="utf-8")
    assert sync_task_files(seeded, "webshop", tasks_dir, "role:dev") == []
    updated = seeded.get_task(existing.id)
    assert updated.title == "更新后的任务"
    assert updated.description == "更新后的描述"


# ---- 面板卡片:通用展示原语 + 平台数据源(AgentDesk 式) ----

def test_widget_data_resolves_tasks_source(seeded):
    client = _client(seeded)
    from missioncrew.taskflow.engine import Engine
    engine = Engine(seeded)
    engine.create_task("webshop", "支付重构", task_type="feature", labels=["pay"])
    engine.create_task("webshop", "修购物车", task_type="bug")
    resolved = client.post("/api/projects/webshop/widget_data", json={"widgets": [
        {"id": "w1", "type": "table",
         "content": {"source": {"from": "tasks", "task_type": ["bug"]}}},
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
