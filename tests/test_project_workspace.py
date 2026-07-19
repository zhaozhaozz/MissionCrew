"""项目主控、频道、文档库、自定义面板和结构化上下文的集成测试。"""
import pytest
from fastapi.testclient import TestClient

from missioncrew import adapters, assembler
from missioncrew.chat import ChatEngine
from missioncrew.documents import library_for
from missioncrew.models import ProjectSkill, Task, TaskStage
from missioncrew.server import create_app


def _client(seeded):
    return TestClient(create_app())


def test_project_has_one_configurable_orchestrator_and_protects_it(seeded):
    client = _client(seeded)
    project = next(p for p in client.get("/api/overview").json()["projects"]
                   if p["id"] == "webshop")
    assert project["orchestrator_role_id"] == "lead"

    project.update({"orchestrator_role_id": "expert", "rules_yaml": ""})
    project.pop("rules", None)
    response = client.post("/api/projects", json=project)
    assert response.status_code == 200
    assert response.json()["orchestrator_role_id"] == "expert"
    assert client.delete("/api/roles/expert?project_id=webshop").status_code == 409

    chat = ChatEngine(seeded)
    msg_id = seeded.add_message("general", "human", "human", "@expert 调度任务", ["expert"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "expert"),
                         seeded.get_backend("exp-1"), msg_id)
    assert "项目主控权限" in cfg.prompt
    dev_cfg = chat._assemble(seeded.get_channel("general"),
                             seeded.get_role("webshop", "dev"),
                             seeded.get_backend("std-1"), msg_id)
    assert "项目主控权限" not in dev_cfg.prompt


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


def test_document_library_versions_and_is_injected_into_all_execution_modes(seeded):
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
        id="checkout-std", name="结算开发", instructions="遵循结算步骤",
        file_refs=["specs/checkout.md"], runtime_ids=["std-1"],
    ))
    project.skills.append(ProjectSkill(
        id="expert-only", instructions="只给专家", runtime_ids=["exp-1"],
    ))
    seeded.put_project(project)
    chat = ChatEngine(seeded)
    msg_id = seeded.add_message("general", "human", "human", "@dev 开发", ["dev"])
    chat_cfg = chat._assemble(seeded.get_channel("general"),
                              seeded.get_role("webshop", "dev"),
                              seeded.get_backend("std-1"), msg_id)
    assert "checkout-std" in chat_cfg.prompt and "expert-only" not in chat_cfg.prompt
    assert "# Checkout v2" in chat_cfg.prompt
    assert chat_cfg.env["MISSIONCREW_DOCUMENTS_DIR"] in chat_cfg.prompt

    task = Task(id="t_context", project_id="webshop", title="context")
    task_cfg = assembler.assemble(
        task, TaskStage(name="develop"), project, seeded.get_backend("exp-1"),
        {}, [], [],
    )
    assert "expert-only" in task_cfg.prompt and "checkout-std" not in task_cfg.prompt
    assert task_cfg.env["MISSIONCREW_DOCUMENTS_DIR"] in task_cfg.prompt


def test_guideline_and_skill_management_validate_runtime_scope(seeded):
    client = _client(seeded)
    assert client.post("/api/projects/webshop/guidelines", json={
        "id": "testing", "title": "测试规范", "content": "所有修复必须回归。",
        "file_refs": ["specs/testing.md"], "actor_role_id": "lead",
    }).status_code == 200
    assert client.post("/api/projects/webshop/guidelines", json={
        "id": "forbidden", "actor_role_id": "tester",
    }).status_code == 403

    skill = client.post("/api/projects/webshop/skills", json={
        "id": "local-ci", "name": "本地 CI", "instructions": "运行完整测试",
        "runtime_ids": ["std-1"], "runtime_instructions": {"std-1": "使用 uv"},
        "actor_role_id": "lead",
    })
    assert skill.status_code == 200
    assert skill.json()["runtime_ids"] == ["std-1"]
    unknown = client.post("/api/projects/webshop/skills", json={
        "id": "bad-runtime", "runtime_ids": ["missing"],
    })
    assert unknown.status_code == 400
    bad_ref = client.post("/api/projects/webshop/skills", json={
        "id": "bad-ref", "file_refs": ["../outside.md"],
    })
    assert bad_ref.status_code == 400


def test_sandboxed_cli_commands_allow_the_document_library():
    docs = "/mc/projects/demo/documents"
    codex = adapters.render_command(
        adapters.DEFAULT_COMMANDS["codex"], "work", "gpt-test", docs)
    claude = adapters.render_command(
        adapters.DEFAULT_COMMANDS["claude_code"], "work", "sonnet", docs)
    assert codex[codex.index("--add-dir") + 1] == docs
    assert claude[claude.index("--add-dir") + 1] == docs


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
    from missioncrew.models import ProjectResource
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
    assert "调度预算" in cfg.prompt and "post_message" in cfg.prompt


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


def test_document_library_linked_into_platform_workdirs(seeded):
    """平台自有工作区内 documents/ 软链指向文档库;真实代码仓不被污染。"""
    from missioncrew.config import mc_home
    chat = ChatEngine(seeded)
    msg = seeded.add_message("general", "human", "human", "@dev 干活", ["dev"])
    cfg = chat._assemble(seeded.get_channel("general"),
                         seeded.get_role("webshop", "dev"),
                         seeded.get_backend("std-1"), msg)
    link = __import__("pathlib").Path(cfg.workdir) / "documents"
    assert link.is_symlink()
    assert link.resolve() == library_for("webshop").root.resolve()
    # 指定了外部 workdir 的频道:不建软链
    from missioncrew.models import Channel
    ext = mc_home() / "ext-repo"
    ext.mkdir(parents=True, exist_ok=True)
    seeded.put_channel(Channel(id="webshop:ext", name="ext", project_id="webshop",
                               workdir=str(ext)))
    msg2 = seeded.add_message("webshop:ext", "human", "human", "@dev 干活", ["dev"])
    chat._assemble(seeded.get_channel("webshop:ext"),
                   seeded.get_role("webshop", "dev"),
                   seeded.get_backend("std-1"), msg2)
    assert not (ext / "documents").exists()


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


# ---- 面板卡片:通用展示原语 + 平台数据源(AgentDesk 式) ----

def test_widget_data_resolves_tasks_source(seeded):
    client = _client(seeded)
    from missioncrew.engine import Engine
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
    from missioncrew.models import BOARD_WIDGET_TYPES
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
    from missioncrew import seed as seed_mod
    p = seeded.get_project("webshop")
    p.dev_guidelines = "旧开发准则内容"
    seeded.put_project(p)
    assert seed_mod.migrate_project_fields(seeded) == 1
    p2 = seeded.get_project("webshop")
    assert p2.dev_guidelines == ""
    doc = next(g for g in p2.guidelines if g.id == "dev-guidelines")
    assert "旧开发准则内容" in doc.content and doc.title == "开发准则"
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
