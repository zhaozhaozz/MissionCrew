"""文档/准则版本比较（diff + ops）与文档下载 inline 参数。"""
from fastapi.testclient import TestClient

from missioncrew.api import create_app


def _client():
    return TestClient(create_app())


def test_document_compare_returns_diff_and_ops(seeded):
    client = _client()
    url = "/api/projects/webshop/documents/file/specs/diff.md"
    first = client.put(url, json={"content": "alpha\nbeta\ngamma\n"})
    second = client.put(url, json={"content": "alpha\nBETA\ngamma\ndelta\n"})
    assert first.status_code == second.status_code == 200

    compared = client.post("/api/projects/webshop/documents/compare", json={
        "path": "specs/diff.md",
        "from_revision": first.json()["revision"],
        "to_revision": second.json()["revision"],
    })
    assert compared.status_code == 200
    comparison = compared.json()
    # unified diff 文本行为不变
    assert comparison["additions"] == 2
    assert comparison["deletions"] == 1
    assert comparison["identical"] is False
    assert "-beta" in comparison["diff"]
    assert "+BETA" in comparison["diff"]
    assert "+delta" in comparison["diff"]
    # ops 覆盖 equal/replace/insert
    ops = comparison["ops"]
    assert {"tag": "equal", "a": ["alpha"], "b": ["alpha"]} in ops
    assert {"tag": "replace", "a": ["beta"], "b": ["BETA"]} in ops
    assert {"tag": "equal", "a": ["gamma"], "b": ["gamma"]} in ops
    assert {"tag": "insert", "a": [], "b": ["delta"]} in ops

    # 反向比较时 insert 变为 delete
    reversed_comparison = client.post(
        "/api/projects/webshop/documents/compare", json={
            "path": "specs/diff.md",
            "from_revision": second.json()["revision"],
            "to_revision": first.json()["revision"],
        }).json()
    assert {"tag": "delete", "a": ["delta"], "b": []} in reversed_comparison["ops"]

    identical = client.post("/api/projects/webshop/documents/compare", json={
        "path": "specs/diff.md",
        "from_revision": first.json()["revision"],
        "to_revision": first.json()["revision"],
    })
    assert identical.status_code == 200
    assert identical.json()["identical"] is True
    assert identical.json()["ops"] == [
        {"tag": "equal", "a": ["alpha", "beta", "gamma"],
         "b": ["alpha", "beta", "gamma"]}]


def _save_guideline(client, markdown):
    return client.post("/api/projects/webshop/guidelines", json={
        "markdown": markdown, "actor_role_id": "lead",
    })


def test_guideline_compare_returns_diff_and_ops(seeded):
    client = _client()
    base_url = "/api/projects/webshop/guidelines/diff-guide"
    first = _save_guideline(client,
                            "---\nname: diff-guide\ndescription: 差异测试\n---\n\n"
                            "# 第一版\n\n保持这一行\n")
    second = client.post("/api/projects/webshop/guidelines", json={
        "original_name": "diff-guide",
        "markdown": ("---\nname: diff-guide\ndescription: 差异测试\n---\n\n"
                     "# 第二版\n\n保持这一行\n\n新增一行\n"),
        "actor_role_id": "lead",
    })
    assert first.status_code == second.status_code == 200

    compared = client.post(base_url + "/compare", json={
        "from_revision": first.json()["revision"],
        "to_revision": second.json()["revision"],
    })
    assert compared.status_code == 200
    comparison = compared.json()
    assert comparison["guideline"] == "diff-guide"
    assert comparison["from_revision"] == first.json()["revision"]
    assert comparison["to_revision"] == second.json()["revision"]
    assert comparison["identical"] is False
    assert comparison["additions"] >= 2
    assert comparison["deletions"] >= 1
    assert "-# 第一版" in comparison["diff"]
    assert "+# 第二版" in comparison["diff"]
    tags = {op["tag"] for op in comparison["ops"]}
    assert {"equal", "replace", "insert"} <= tags
    assert all(set(op) == {"tag", "a", "b"} for op in comparison["ops"])
    assert any(op["tag"] == "insert" and op["a"] == [] and "新增一行" in op["b"]
               for op in comparison["ops"])

    identical = client.post(base_url + "/compare", json={
        "from_revision": first.json()["revision"],
        "to_revision": first.json()["revision"],
    })
    assert identical.status_code == 200
    assert identical.json()["identical"] is True
    assert identical.json()["additions"] == identical.json()["deletions"] == 0
    assert all(op["tag"] == "equal" for op in identical.json()["ops"])

    missing_revision = client.post(base_url + "/compare", json={
        "from_revision": "deadbeef00",
        "to_revision": second.json()["revision"],
    })
    assert missing_revision.status_code == 404

    missing_guideline = client.post(
        "/api/projects/webshop/guidelines/no-such-guide/compare", json={
            "from_revision": first.json()["revision"],
            "to_revision": second.json()["revision"],
        })
    assert missing_guideline.status_code == 404


def test_document_download_inline_disposition(seeded):
    client = _client()
    client.put("/api/projects/webshop/documents/file/specs/preview.md",
               json={"content": "# 预览\n"})
    base = "/api/projects/webshop/documents/download/specs/preview.md"

    attachment = client.get(base)
    assert attachment.status_code == 200
    assert attachment.headers["Content-Disposition"].startswith("attachment")

    inline = client.get(base, params={"inline": 1})
    assert inline.status_code == 200
    assert inline.headers["Content-Disposition"].startswith("inline")
    assert inline.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in inline.headers


def test_document_download_inline_html_is_sandboxed(seeded):
    """HTML 内联预览在沙箱 iframe 中渲染；响应头同样声明 CSP sandbox 作为兜底。"""
    client = _client()
    client.put("/api/projects/webshop/documents/file/reports/index.html",
               json={"content": "<!doctype html><p>report</p>"})
    base = "/api/projects/webshop/documents/download/reports/index.html"

    inline = client.get(base, params={"inline": 1})
    assert inline.status_code == 200
    assert inline.headers["Content-Type"].startswith("text/html")
    assert inline.headers["Content-Security-Policy"].startswith("sandbox")
    assert "allow-same-origin" not in inline.headers["Content-Security-Policy"]

    attachment = client.get(base)
    assert attachment.headers["Content-Disposition"].startswith("attachment")
    assert "Content-Security-Policy" not in attachment.headers
