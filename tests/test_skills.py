"""完整 Skill 包导入、直接投放与 Runtime 适配。"""
from __future__ import annotations

import os
import io
import shutil
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.skills import (project_skill_library_dir,
                                       skill_context_dir, write_skill_context)


def _skill_markdown(name: str, description: str, body: str = "") -> str:
    suffix = f"\n{body.rstrip()}\n" if body else ""
    return f"---\nname: {name}\ndescription: {description}\n---\n{suffix}"


def _zip(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_skill_context_refresh_tolerates_concurrently_removed_stale_directory(
        seeded, monkeypatch):
    project = seeded.get_project("webshop")
    context = skill_context_dir(project)
    stale = context / ".agents"
    stale.mkdir(parents=True)
    original_is_dir = Path.is_dir

    def remove_after_type_check(path: Path) -> bool:
        is_directory = original_is_dir(path)
        if path == stale and is_directory:
            path.rmdir()
        return is_directory

    monkeypatch.setattr(Path, "is_dir", remove_after_type_check)

    assert write_skill_context(project) == context
    assert not stale.exists()


def test_direct_drop_skill_is_discovered_with_complete_files_and_runtime_access(seeded):
    client = TestClient(create_app())
    root = project_skill_library_dir("webshop")
    skill_dir = root / "browser-check"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "SKILL.md").write_text(_skill_markdown(
        "browser-check", "浏览器交互或视觉验证时使用", "运行 scripts/check.sh。"))
    (skill_dir / "scripts" / "check.sh").write_text("echo first\n")
    (skill_dir / "references" / "selectors.md").write_text("# Selectors\n")

    project = next(item for item in client.get("/api/overview").json()["projects"]
                   if item["id"] == "webshop")
    assert any(skill["id"] == "browser-check" for skill in project["skills"])

    chat = ChatEngine(seeded)
    message = seeded.add_message("general", "human", "human", "@dev 验证页面", ["dev"])
    cfg = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
        seeded.get_backend("std-1"), message)
    exposed = Path(cfg.env["MISSIONCREW_SKILLS_DIR"]) / "browser-check"
    assert exposed.is_symlink() and exposed.resolve() == skill_dir.resolve()
    assert not os.path.isabs(os.readlink(exposed))   # 相对链接,数据目录可搬迁
    assert (exposed / "scripts" / "check.sh").read_text() == "echo first\n"
    assert (exposed / "references" / "selectors.md").is_file()
    assert str(exposed.parent) in cfg.prompt          # 目录一次说明,入口按 id 推导
    assert "浏览器交互或视觉验证时使用" in cfg.prompt
    assert "运行 scripts/check.sh" not in cfg.prompt
    assert str(root.resolve()) in cfg.allowed_dirs

    # Skill 文件改动不改公共上下文版本,已有会话下一轮只收到「资源更新」提示
    from missioncrew.runtime import adapters
    first_version = cfg.context_version
    adapters.get_adapter("mock").run(cfg)
    (skill_dir / "scripts" / "check.sh").write_text("echo second\n")
    updated = chat._assemble(
        seeded.get_channel("general"), seeded.get_role("webshop", "dev"),
        seeded.get_backend("std-1"), message)
    assert updated.context_version == first_version
    assert "Skill `browser-check`" in updated.turn_prompt

    shutil.rmtree(skill_dir)
    project = next(item for item in client.get("/api/overview").json()["projects"]
                   if item["id"] == "webshop")
    assert all(skill["id"] != "browser-check" for skill in project["skills"])


def test_zip_import_detects_skill_packages_preserves_files_and_confirms_overwrite(seeded):
    client = TestClient(create_app())
    payload = _zip({
        "bundle/release/SKILL.md": (
            "---\nname: release\ndescription: 准备版本发布时使用\n"
            "allowed-tools:\n  - Bash\n---\n\n参见 references/checklist.md。\n"),
        "bundle/release/references/checklist.md": "# Checklist\n",
        "bundle/release/scripts/release.sh": "#!/bin/sh\necho release\n",
        "bundle/audit/skill.md": _skill_markdown("audit", "进行审计时使用"),
        "bundle/audit/assets/template.txt": "report\n",
    })
    url = "/api/projects/webshop/skills/import-zip"
    imported = client.post(url, content=payload, headers={"Content-Type": "application/zip"})
    assert imported.status_code == 200
    assert imported.json()["imported"] == ["audit", "release"]
    root = project_skill_library_dir("webshop")
    assert (root / "release" / "scripts" / "release.sh").is_file()
    assert (root / "release" / "references" / "checklist.md").is_file()
    assert (root / "audit" / "SKILL.md").is_file()
    assert not (root / "audit" / "skill.md").exists()

    edited = client.post("/api/projects/webshop/skills", json={
        "id": "release", "name": "release", "description": "发布检查",
        "instructions": "运行 scripts/release.sh", "enabled": True,
    })
    assert edited.status_code == 200
    release_markdown = (root / "release" / "SKILL.md").read_text()
    assert "allowed-tools:\n- Bash" in release_markdown
    assert "运行 scripts/release.sh" in release_markdown

    conflict = client.post(url, content=payload, headers={"Content-Type": "application/zip"})
    assert conflict.status_code == 200
    assert conflict.json()["needs_confirmation"] is True
    assert conflict.json()["conflicts"] == ["audit", "release"]
    overwritten = client.post(
        url + "?overwrite=true", content=payload,
        headers={"Content-Type": "application/zip"})
    assert overwritten.status_code == 200
    assert overwritten.json()["needs_confirmation"] is False
    recycled = client.get("/api/projects/webshop/recycle-bin").json()["items"]
    assert any(item["resource_type"] == "skill"
               and item["resource_id"] == "release" for item in recycled)
    assert not (root.parent / ".skill-trash").exists()

    unsafe = _zip({"../escape/SKILL.md": _skill_markdown("escape", "no")})
    rejected = client.post(url, content=unsafe, headers={"Content-Type": "application/zip"})
    assert rejected.status_code == 400
    assert "不安全路径" in rejected.json()["detail"]

    single = _zip({
        "SKILL.md": _skill_markdown("root-skill", "ZIP 根目录中的单个 Skill"),
        "scripts/run.sh": "echo root\n",
    })
    root_import = client.post(url, content=single, headers={"Content-Type": "application/zip"})
    assert root_import.status_code == 200
    assert root_import.json()["imported"] == ["root-skill"]
    assert (root / "root-skill" / "scripts" / "run.sh").is_file()


def test_local_folder_import_and_new_project_skill_directory(seeded, tmp_path):
    client = TestClient(create_app())
    source = tmp_path / "skill-collection"
    tool = source / "api-helper"
    (tool / "scripts").mkdir(parents=True)
    (tool / "SKILL.md").write_text(
        _skill_markdown("api-helper", "开发或检查 API 时使用"))
    (tool / "scripts" / "inspect.py").write_text("print('ok')\n")

    imported = client.post("/api/projects/webshop/skills/import-folder", json={
        "path": str(source), "overwrite": False,
    })
    assert imported.status_code == 200
    assert imported.json()["imported"] == ["api-helper"]
    assert (project_skill_library_dir("webshop") / "api-helper" / "scripts"
            / "inspect.py").is_file()

    created = client.post("/api/projects", json={"id": "new-project", "name": "New"})
    assert created.status_code == 200
    new_root = project_skill_library_dir("new-project")
    assert new_root.is_dir() and list(new_root.iterdir()) == []

    unsafe_source = tmp_path / "unsafe-skills" / "linked"
    unsafe_source.mkdir(parents=True)
    (unsafe_source / "SKILL.md").write_text(
        _skill_markdown("linked", "包含越界链接的 Skill"))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (unsafe_source / "reference.txt").symlink_to(outside)
    unsafe = client.post("/api/projects/webshop/skills/import-folder", json={
        "path": str(unsafe_source.parent), "overwrite": False,
    })
    assert unsafe.status_code == 400
    assert "不允许符号链接" in unsafe.json()["detail"]

    managed_root = project_skill_library_dir("webshop")
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (managed_root / "linked-save").symlink_to(escaped, target_is_directory=True)
    save_through_link = client.post("/api/projects/webshop/skills", json={
        "id": "linked-save", "name": "linked-save", "description": "unsafe",
        "instructions": "must not be written", "enabled": True,
    })
    assert save_through_link.status_code == 400
    assert not (escaped / "SKILL.md").exists()


def test_save_skill_with_full_markdown_preserves_frontmatter(seeded):
    client = TestClient(create_app())
    root = project_skill_library_dir("webshop")
    markdown = (
        "---\nname: 发布\ndescription: 准备版本发布时使用\n"
        "allowed-tools:\n  - Bash\n---\n\n参见 references/checklist.md。\n")
    saved = client.post("/api/projects/webshop/skills", json={
        "id": "release-md", "markdown": markdown, "enabled": False,
    })
    assert saved.status_code == 200
    assert saved.json()["name"] == "发布" and saved.json()["enabled"] is False
    assert (root / "release-md" / "SKILL.md").read_text() == markdown

    info = client.get("/api/projects/webshop/skills/library").json()
    entry = next(item for item in info["skills"] if item["id"] == "release-md")
    assert entry["markdown"] == markdown

    updated = client.post("/api/projects/webshop/skills", json={
        "id": "release-md",
        "markdown": markdown.replace("准备版本发布时使用", "发布前检查时使用"),
        "enabled": True,
    })
    assert updated.status_code == 200
    assert "allowed-tools:\n  - Bash" in (root / "release-md" / "SKILL.md").read_text()

    invalid = client.post("/api/projects/webshop/skills", json={
        "id": "broken", "markdown": "# 没有 frontmatter\n", "enabled": True,
    })
    assert invalid.status_code == 400
    assert not (root / "broken").exists()


def test_skill_full_package_history_compare_and_restore(seeded):
    client = TestClient(create_app())
    root = project_skill_library_dir("webshop")
    directory = root / "versioned-skill"
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        _skill_markdown("versioned-skill", "第一版", "使用 scripts/run.sh。"))
    script = directory / "scripts" / "run.sh"
    script.write_text("echo first\n")
    script.chmod(0o755)

    scanned = client.post("/api/projects/webshop/skills/rescan")
    assert scanned.status_code == 200
    first_revision = next(
        item for item in scanned.json()["skills"]
        if item["id"] == "versioned-skill")["revision"]
    assert len(first_revision) == 40

    (directory / "SKILL.md").write_text(
        _skill_markdown("versioned-skill", "第二版", "使用 references/check.md。"))
    script.write_text("echo second\n")
    (directory / "references").mkdir()
    (directory / "references" / "check.md").write_text("# Check\n")
    rescanned = client.post("/api/projects/webshop/skills/rescan")
    second_revision = next(
        item for item in rescanned.json()["skills"]
        if item["id"] == "versioned-skill")["revision"]
    assert second_revision != first_revision

    enabled_only = client.post("/api/projects/webshop/skills", json={
        "id": "versioned-skill",
        "markdown": (directory / "SKILL.md").read_text(),
        "enabled": False,
    })
    assert enabled_only.status_code == 200
    assert enabled_only.json()["revision"] == second_revision

    history_url = "/api/projects/webshop/skills/versioned-skill/history"
    history = client.get(history_url)
    assert history.status_code == 200
    assert [row["revision"] for row in history.json()[:2]] == [
        second_revision, first_revision]

    old = client.get(f"{history_url}/{first_revision}")
    assert old.status_code == 200
    assert "第一版" in old.json()["markdown"]
    assert old.json()["files"] == ["SKILL.md", "scripts/run.sh"]
    old_script = client.get(
        "/api/projects/webshop/skills/versioned-skill/file",
        params={"path": "scripts/run.sh", "revision": first_revision})
    assert old_script.json()["content"] == "echo first\n"

    compared = client.post(
        "/api/projects/webshop/skills/versioned-skill/compare", json={
            "from_revision": first_revision,
            "to_revision": second_revision,
        })
    assert compared.status_code == 200
    changes = {item["path"]: item["status"]
               for item in compared.json()["file_changes"]}
    assert changes == {
        "SKILL.md": "modified",
        "references/check.md": "added",
        "scripts/run.sh": "modified",
    }
    assert compared.json()["identical"] is False

    script.chmod(0o644)
    mode_revision = next(
        item for item in client.post(
            "/api/projects/webshop/skills/rescan").json()["skills"]
        if item["id"] == "versioned-skill")["revision"]
    mode_compared = client.post(
        "/api/projects/webshop/skills/versioned-skill/compare", json={
            "from_revision": second_revision,
            "to_revision": mode_revision,
        }).json()
    assert mode_compared["file_changes"] == [
        {"path": "scripts/run.sh", "status": "modified"}]
    assert mode_compared["additions"] == mode_compared["deletions"] == 0
    assert mode_compared["identical"] is False

    restored = client.post(
        "/api/projects/webshop/skills/versioned-skill/restore",
        json={"revision": first_revision})
    assert restored.status_code == 200
    restored_revision = restored.json()["revision"]
    assert restored_revision not in {first_revision, second_revision}
    assert "第一版" in (directory / "SKILL.md").read_text()
    assert script.read_text() == "echo first\n"
    assert script.stat().st_mode & 0o111
    assert not (directory / "references").exists()
    assert client.get(history_url).json()[0]["revision"] == restored_revision


def test_skill_file_read_endpoint_serves_text_and_rejects_escape(seeded):
    client = TestClient(create_app())
    root = project_skill_library_dir("webshop")
    skill_dir = root / "browser-check"
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_skill_markdown("browser-check", "查看文件"))
    (skill_dir / "scripts" / "check.sh").write_text("echo first\n")
    (skill_dir / "blob.bin").write_bytes(b"\x00\x01\x02")

    shown = client.get("/api/projects/webshop/skills/browser-check/file",
                       params={"path": "scripts/check.sh"})
    assert shown.status_code == 200
    assert shown.json() == {
        "path": "scripts/check.sh", "content": "echo first\n", "truncated": False,
        "resource_url": "/resources/webshop/skills/browser-check/scripts/check.sh",
    }

    assert client.get("/api/projects/webshop/skills/browser-check/file",
                      params={"path": "../escape"}).status_code == 404
    assert client.get("/api/projects/webshop/skills/browser-check/file",
                      params={"path": "blob.bin"}).status_code == 400
    assert client.get("/api/projects/webshop/skills/missing/file",
                      params={"path": "SKILL.md"}).status_code == 404
    assert client.get("/api/projects/webshop/skills/bad%2Fid/file",
                      params={"path": "SKILL.md"}).status_code in (400, 404)


def test_skill_page_exposes_all_import_modes(seeded):
    client = TestClient(create_app())
    html = client.get("/").text
    js = client.get("/assets/js/project-configs.js").text
    markdown = client.get("/assets/js/markdown.js").text
    sidebar = client.get("/assets/js/sidebar.js").text

    assert 'id="skill-zip-input"' in html
    assert "上传 ZIP" in html and "导入本地目录" in html and "重新扫描" in html
    assert 'id="skill-library-info"' in html
    for function in ("importSkillZip", "openSkillFolderImport",
                     "importSkillFolder", "rescanSkillLibrary"):
        assert f"function {function}" in js
    assert "直接投放目录" in js and "skillFolderImportOpen" in js
    assert "function isSkillMarkdownFile(path)" in js
    assert "/\\.(?:md|markdown)$/i.test(path)" in js
    assert "function markdownPreviewHtml(markdown" in markdown
    assert "function markdownFrontmatterTableHtml(source)" in markdown
    assert 'class="markdown-frontmatter-table"' in markdown
    assert "<th>属性</th><th>值</th>" in markdown
    assert 'class="markdown-frontmatter-value"' in markdown
    assert "markdownPreviewHtml(data.content)" in js
    assert 'class="skill-file-viewer-body markdown-body"' in js
    assert 'class="skill-file-viewer-body">${esc(data.content)}' in js
    assert "openFormDialog" not in js
    assert 'targetInputId = "res-target"' in sidebar
