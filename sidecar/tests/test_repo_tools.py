"""Tests for read-only repo mirrors + tools (best-of-B), boundary-gated."""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from colony_sidecar.repos import RepoMirrorManager, parse_mirror_config
from colony_sidecar.directives import DirectiveManager, DirectiveStore


def _make_source_repo(base: str) -> str:
    """A tiny real git repo to mirror via file:// (no network)."""
    src = os.path.join(base, "src-repo")
    os.makedirs(os.path.join(src, "src"))
    with open(os.path.join(src, "README.md"), "w") as f:
        f.write("# widget-api\nA sample service.\n")
    with open(os.path.join(src, "src", "main.py"), "w") as f:
        f.write("def handle_retry():\n    return 'retry logic here'\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=src, env=env, check=True, capture_output=True)
    return src


def test_parse_mirror_config():
    cfg = parse_mirror_config("widget-api=https://x/y.git|the widget repo,bill=git@z:b.git")
    assert cfg["widget-api"]["url"] == "https://x/y.git"
    assert cfg["widget-api"]["aliases"] == "the widget repo"
    assert cfg["bill"]["url"] == "git@z:b.git"
    assert parse_mirror_config("") == {}


@pytest.fixture()
def mirror_env(tmp_path):
    src = _make_source_repo(str(tmp_path))
    mgr = RepoMirrorManager(
        mirror_dir=str(tmp_path / "mirrors"),
        config={"widget-api": {"url": f"file://{src}", "aliases": "the widget repo"}},
    )
    r = mgr.refresh("widget-api")
    assert r["ok"], r
    return mgr


def test_mirror_clone_list_read_search(mirror_env):
    mgr = mirror_env
    ls = mgr.list_files("widget-api")
    assert "README.md" in ls["files"] and "src/main.py" in ls["files"]
    rd = mgr.read_file("widget-api", "src/main.py")
    assert "handle_retry" in rd["content"]
    sr = mgr.search("widget-api", "retry logic")
    assert sr["count"] >= 1 and "main.py" in sr["matches"][0]


def test_mirror_unknown_repo_and_path_escape(mirror_env):
    mgr = mirror_env
    assert mgr.list_files("nope").get("status") == "unavailable"
    assert mgr.read_file("widget-api", "../../etc/passwd").get("status") in ("error", "not_found")


def test_act_boundary_allows_reads(tmp_path):
    """Tiered semantics: 'leave X alone' (ACT) binds ACTION, not perception --
    read-only mirror tools stay open."""
    src = _make_source_repo(str(tmp_path))
    dm = DirectiveManager(DirectiveStore(db_path=None))
    dm.capture_from_message("leave the widget-api repo alone")
    mgr = RepoMirrorManager(
        mirror_dir=str(tmp_path / "m2"),
        config={"widget-api": {"url": f"file://{src}", "aliases": ""}},
        directive_manager=dm,
    )
    r = mgr.refresh("widget-api")
    assert r["ok"] is True                       # reads open under ACT
    assert "README.md" in mgr.list_files("widget-api")["files"]
    assert "handle_retry" in mgr.read_file("widget-api", "src/main.py")["content"]


def test_observe_boundary_blocks_reads(tmp_path):
    """Explicit perception language -> OBSERVE blackout blocks reads too."""
    src = _make_source_repo(str(tmp_path))
    dm = DirectiveManager(DirectiveStore(db_path=None))
    dm.capture_from_message("don't even look at the widget-api repo")
    mgr = RepoMirrorManager(
        mirror_dir=str(tmp_path / "m3"),
        config={"widget-api": {"url": f"file://{src}", "aliases": ""}},
        directive_manager=dm,
    )
    r = mgr.refresh("widget-api")
    assert r["ok"] is False and "boundary" in r["reason"]
    assert mgr.list_files("widget-api").get("status") == "boundary_refused"
    assert mgr.read_file("widget-api", "README.md").get("status") == "boundary_refused"
    assert mgr.search("widget-api", "retry").get("status") == "boundary_refused"
