"""频道附件上传:落盘位置、回读、越界防护与执行授权目录。"""
from pathlib import Path

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.workspace import channel_uploads_dir


def test_upload_saves_file_and_reports_local_path(seeded):
    client = TestClient(create_app())
    r = client.post("/api/chat/general/uploads",
                    params={"filename": "notes.txt"}, content=b"hello")
    assert r.status_code == 200
    data = r.json()
    assert data["name"].endswith("-notes.txt")
    assert data["size"] == 5
    assert data["is_image"] is False
    saved = Path(data["path"])
    assert saved.read_bytes() == b"hello"
    assert saved.parent == channel_uploads_dir("webshop", "general")
    # 回读接口供消息内预览与人工下载
    fetched = client.get(data["url"])
    assert fetched.status_code == 200
    assert fetched.content == b"hello"


def test_upload_sanitizes_filename_and_flags_images(seeded):
    client = TestClient(create_app())
    r = client.post("/api/chat/general/uploads",
                    params={"filename": "../../evil path.png"}, content=b"\x89PNG")
    assert r.status_code == 200
    data = r.json()
    assert data["is_image"] is True
    assert "/" not in data["name"] and ".." not in data["name"]
    assert Path(data["path"]).parent == channel_uploads_dir("webshop", "general")


def test_upload_rejects_unknown_channel_and_empty_body(seeded):
    client = TestClient(create_app())
    assert client.post("/api/chat/nope/uploads", params={"filename": "a.txt"},
                       content=b"x").status_code == 404
    assert client.post("/api/chat/general/uploads", params={"filename": "a.txt"},
                       content=b"").status_code == 400


def test_download_rejects_missing_or_out_of_dir_names(seeded):
    client = TestClient(create_app())
    assert client.get("/api/chat/general/uploads/missing.txt").status_code == 404
    assert client.get(
        "/api/chat/general/uploads/%2e%2e%2fdb.sqlite3").status_code == 404


def test_uploads_dir_is_authorized_after_first_upload(seeded):
    client = TestClient(create_app())
    chat = ChatEngine(seeded)
    message = seeded.add_message("general", "human", "human", "@dev 看附件", ["dev"])
    before = chat._assemble(seeded.get_channel("general"),
                            seeded.get_role("webshop", "dev"),
                            seeded.get_backend("std-1"), message)
    uploads_path = str(channel_uploads_dir("webshop", "general"))
    assert uploads_path not in before.allowed_dirs  # 无附件时不授权空目录

    client.post("/api/chat/general/uploads",
                params={"filename": "spec.md"}, content=b"# spec")
    after = chat._assemble(seeded.get_channel("general"),
                           seeded.get_role("webshop", "dev"),
                           seeded.get_backend("std-1"), message)
    assert uploads_path in after.allowed_dirs
    assert uploads_path in after.runtime_policy.readable_paths
