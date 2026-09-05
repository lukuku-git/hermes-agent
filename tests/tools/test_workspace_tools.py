import json
import os
import subprocess
import sys

import pytest

from tools import workspace_tools as ws
from toolsets import resolve_toolset


def result(value):
    return json.loads(value)


@pytest.fixture
def root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "docs").mkdir()
    (workspace / "docs" / "hello.txt").write_text("hello world\n", encoding="utf-8")
    monkeypatch.setenv(ws.ROOT_ENV, str(workspace))
    return workspace


def test_normal_read_search_write_patch(root):
    assert result(ws.workspace_read({"path": "docs/hello.txt"}))["content"] == "hello world\n"
    found = result(ws.workspace_search({"pattern": "WORLD"}))
    assert found["results"][0]["path"] == "docs/hello.txt"
    assert result(ws.workspace_write({"path": "docs/new.txt", "content": "alpha"}))["success"]
    assert result(ws.workspace_patch({"path": "docs/new.txt", "old_string": "alpha", "new_string": "beta"}))["success"]
    assert (root / "docs" / "new.txt").read_text() == "beta"


@pytest.mark.parametrize("path", ["/Users/alice/private.txt", "../private.txt", ".git/config", "auth.json", "config.yaml", ".hermes/state"])
def test_denied_paths_do_not_leak_input(root, path):
    response = ws.workspace_read({"path": path})
    assert "error" in result(response)
    assert path not in response and str(root) not in response


def test_nested_symlink_and_symlink_destination_denied(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "destination.txt").symlink_to(outside / "secret.txt")
    assert "error" in result(ws.workspace_read({"path": "link/secret.txt"}))
    assert "error" in result(ws.workspace_write({"path": "destination.txt", "content": "changed"}))
    assert (outside / "secret.txt").read_text() == "secret"


def test_hard_link_denied(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    os.link(outside, root / "linked.txt")
    assert "error" in result(ws.workspace_read({"path": "linked.txt"}))
    assert "error" in result(ws.workspace_write({"path": "linked.txt", "content": "changed"}))


def test_missing_relative_and_symlink_roots_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv(ws.ROOT_ENV, raising=False)
    assert "error" in result(ws.workspace_read({"path": "x"}))
    monkeypatch.setenv(ws.ROOT_ENV, "relative")
    assert "error" in result(ws.workspace_read({"path": "x"}))
    missing = tmp_path / "missing"
    monkeypatch.setenv(ws.ROOT_ENV, str(missing))
    assert "error" in result(ws.workspace_read({"path": "x"}))
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "root-link"; link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv(ws.ROOT_ENV, str(link))
    assert "error" in result(ws.workspace_read({"path": "x"}))


def test_paginated_read_first_middle_and_final_pages(root):
    (root / "pages.txt").write_text("one\ntwo\nthree\nfour", encoding="utf-8")

    first = result(ws.workspace_read({"path": "pages.txt", "limit": 2}))
    assert first == {
        "path": "pages.txt", "content": "one\ntwo\n",
        "total_lines": 4, "next_offset": 3,
    }
    middle = result(ws.workspace_read({"path": "pages.txt", "offset": 2, "limit": 2}))
    assert middle["content"] == "two\nthree\n"
    assert middle["total_lines"] == 4
    assert middle["next_offset"] == 4
    final = result(ws.workspace_read({"path": "pages.txt", "offset": 4, "limit": 2}))
    assert final["content"] == "four"
    assert final["total_lines"] == 4
    assert final["next_offset"] is None


@pytest.mark.parametrize("key,value", [
    ("offset", 0), ("offset", -1), ("offset", True), ("offset", "1"),
    ("limit", 0), ("limit", -1), ("limit", True), ("limit", "1"),
    ("limit", ws.MAX_READ_LINES + 1),
])
def test_paginated_read_rejects_invalid_offset_and_limit(root, key, value):
    assert "error" in result(ws.workspace_read({"path": "docs/hello.txt", key: value}))


def test_paginated_read_rejects_oversized_line_and_escaped_output(root):
    (root / "long-line.txt").write_text("x" * (ws.MAX_READ + 1), encoding="utf-8")
    assert "error" in result(ws.workspace_read({"path": "long-line.txt", "limit": 1}))

    # JSON escaping can make output exceed the cap even when content does not.
    (root / "escaped.txt").write_text("\\" * (ws.MAX_OUTPUT // 2), encoding="utf-8")
    assert "limit" in result(ws.workspace_read({"path": "escaped.txt", "limit": 1}))["error"]


def test_oversized_read_and_search_result_limit(root):
    (root / "large.txt").write_text("x" * (ws.MAX_READ + 1))
    assert "error" in result(ws.workspace_read({"path": "large.txt"}))
    (root / "many.txt").write_text("match\n" * (ws.MAX_RESULTS + 10))
    searched = result(ws.workspace_search({"pattern": "match"}))
    assert searched["truncated"] is True
    assert len(searched["results"]) == ws.MAX_RESULTS


def test_workspace_alias_contains_only_scoped_tools():
    assert set(resolve_toolset("workspace")) == {
        "workspace_read", "workspace_search", "workspace_write", "workspace_patch"
    }
    assert not ({"read_file", "terminal", "process", "execute_code"} & set(resolve_toolset("workspace")))


def test_workspace_readonly_contains_only_review_tools():
    assert resolve_toolset("workspace_readonly") == [
        "workspace_read", "workspace_search",
    ]
    assert not ({
        "workspace_write", "workspace_patch", "write_file", "patch",
        "terminal", "process", "execute_code",
    } & set(resolve_toolset("workspace_readonly")))


def test_builtin_discovery_registers_and_exposes_workspace_tools(root):
    """Exercise discovery in a fresh process, not the already-imported test module."""
    expected = {"workspace_read", "workspace_search", "workspace_write", "workspace_patch"}
    script = """
import json
from tools.registry import discover_builtin_tools, registry

imported = discover_builtin_tools()
registered = registry.get_tool_names_for_toolset("workspace")
definitions = registry.get_definitions(set(registered), quiet=True)
print(json.dumps({
    "imported": imported,
    "registered": registered,
    "available": [item["function"]["name"] for item in definitions],
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.fspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        env={**os.environ, ws.ROOT_ENV: str(root)},
        check=True,
        capture_output=True,
        text=True,
    )
    discovered = json.loads(completed.stdout)
    assert "tools.workspace_tools" in discovered["imported"]
    assert set(discovered["registered"]) == expected
    assert set(discovered["available"]) == expected