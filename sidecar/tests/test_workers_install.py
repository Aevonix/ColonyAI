"""Packaged workers: configuration, claims, dry runs and service entrypoints."""

from __future__ import annotations

import plistlib
import tomllib
import urllib.request
from pathlib import Path

import pytest

from apsimo.workers import colony_worker, queue_worker, skills_sync


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "COLONY_URL", "COLONY_API_KEY", "COLONY_JOBS_WEBHOOK_URL",
        "COLONY_WORKER_NODE_ID", "COLONY_AGENT_NAME", "COLONY_WORKER_MAX_JOBS",
        "HERMES_SKILLS_DIR", "COLONY_STATE_DIR", "COLONY_HOME",
        "COLONY_INIT_DEFAULTS",
        "COLONY_AGENT_JOB_CLAIMS_ENABLED", "COLONY_WORKER_JOB_TYPES",
    ):
        monkeypatch.delenv(var, raising=False)


def _no_network(monkeypatch):
    """Any urllib request in the test is a bug."""
    def boom(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(urllib.request, "urlopen", boom)


# ---------------------------------------------------------------------------
# queue_worker module
# ---------------------------------------------------------------------------

def test_queue_worker_load_config_defaults():
    cfg = queue_worker.load_config()
    assert cfg["colony_url"] == "http://127.0.0.1:7777"
    assert cfg["api_key"] == "dev-mode-no-key"
    assert cfg["webhook_url"] == "http://127.0.0.1:8644/webhooks/colony-jobs"
    assert cfg["node_id"] == "hermes-agent"
    assert cfg["max_jobs"] == 1


def test_queue_worker_node_id_derived_from_agent_name(monkeypatch):
    monkeypatch.setenv("COLONY_AGENT_NAME", "My Agent")
    assert queue_worker.load_config()["node_id"] == "my-agent-agent"
    monkeypatch.setenv("COLONY_WORKER_NODE_ID", "explicit-node")
    assert queue_worker.load_config()["node_id"] == "explicit-node"


def test_queue_worker_max_jobs_env(monkeypatch):
    monkeypatch.setenv("COLONY_WORKER_MAX_JOBS", "3")
    assert queue_worker.load_config()["max_jobs"] == 3


def test_queue_worker_webhook_payload_lifecycle_urls():
    cfg = queue_worker.load_config()
    job = {
        "job_id": "job-42",
        "claim_attempt_id": "attempt-42",
        "payload": {"action_hint": "agent_sync_github", "risk": "read_only",
                    "domain": "github", "description": "look around"},
    }
    payload = queue_worker.build_webhook_payload(cfg, job)
    inner = payload["payload"]
    assert payload["type"] == "agent_job"
    assert inner["job_id"] == "job-42"
    assert inner["claim_attempt_id"] == "attempt-42"
    assert inner["action_hint"] == "agent_sync_github"
    assert inner["observations_url"] == "http://127.0.0.1:7777/v1/host/observations"
    assert inner["heartbeat_url"] == "http://127.0.0.1:7777/v1/host/queue/jobs/job-42/heartbeat"
    assert inner["complete_url"] == "http://127.0.0.1:7777/v1/host/queue/jobs/job-42/complete"
    assert inner["fail_url"] == "http://127.0.0.1:7777/v1/host/queue/jobs/job-42/fail"
    assert inner["api_key_header"] == "X-API-Key"


def test_bundled_hermes_job_route_is_retired_and_empty():
    route = (
        Path(__file__).resolve().parents[2]
        / "plugins/hermes-plugin/examples/webhook-config.yaml"
    ).read_text()
    assert "routes: {}" in route
    assert "heartbeat_url" not in route
    assert "claim_attempt_id" not in route


def test_queue_worker_claim_empty_response_is_none(monkeypatch):
    monkeypatch.setattr(queue_worker, "_post", lambda cfg, url, body, timeout=15: {})
    assert queue_worker.claim_job(queue_worker.load_config()) is None


def test_queue_worker_fire_failure_releases_claim(monkeypatch, capsys):
    _no_network(monkeypatch)  # webhook fire raises -> failure path
    released = []
    monkeypatch.setattr(
        queue_worker, "_post",
        lambda cfg, url, body, timeout=15: released.append(url),
    )
    cfg = queue_worker.load_config()
    ok = queue_worker.fire_to_agent(cfg, {"job_id": "job-9", "payload": {}})
    assert ok is False
    assert released == ["http://127.0.0.1:7777/v1/host/queue/jobs/job-9/release"]
    assert "Webhook fire failed for job job-9" in capsys.readouterr().out


def test_queue_worker_starts_claim_before_handing_it_to_agent(monkeypatch):
    calls = []
    claims = [{"job_id": "job-10", "payload": {}}, {}]

    def fake_post(cfg, url, body, timeout=15):  # noqa: ARG001
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/jobs/claim"):
            return claims.pop(0)
        return {"success": True}

    monkeypatch.setattr(queue_worker, "_post", fake_post)
    monkeypatch.setattr(
        queue_worker,
        "fire_to_agent",
        lambda cfg, job: calls.append("webhook") or True,
    )
    cfg = queue_worker.load_config()
    assert queue_worker.run(cfg) == 1
    assert calls.index("start") < calls.index("webhook")


def test_global_claim_kill_switch_stops_standalone_queue_worker(
        monkeypatch):
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "false")
    monkeypatch.setattr(
        queue_worker, "register_worker",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("registration must stay dark")
        ),
    )
    monkeypatch.setattr(
        queue_worker, "claim_job",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("claim must stay dark")
        ),
    )
    assert queue_worker.run(queue_worker.load_config()) == 0


def test_global_claim_kill_switch_removes_agent_action_from_colony_worker(
        monkeypatch):
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("COLONY_WORKER_JOB_TYPES", "agent_action")
    cfg = colony_worker.load_config()
    assert cfg["job_types"] == []
    monkeypatch.setattr(
        colony_worker, "register_worker",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("registration must stay dark")
        ),
    )
    assert colony_worker.run_cycle(cfg) == 0


def test_queue_worker_main_dry_run_no_network(monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setenv("COLONY_WORKER_NODE_ID", "dry-node")
    assert queue_worker.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "dry-node" in out


# ---------------------------------------------------------------------------
# skills_sync module
# ---------------------------------------------------------------------------

def _write_skill(base: Path, rel: str, name: str, description: str, tags: str = ""):
    skill_dir = base / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n{tags}---\n\n# {name}\n"
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")


def test_skills_sync_scan_parses_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SKILLS_DIR", str(tmp_path))
    _write_skill(tmp_path, "git-helper", "git-helper", "Work with git",
                 tags="tags: [git, vcs]\n")
    _write_skill(tmp_path, "deep/web-search", "web-search", "Search the web",
                 tags="tags:\n  - web\n  - search\n")
    obs = skills_sync.scan()
    by_id = {o["entity_id"]: o["payload"] for o in obs}
    assert set(by_id) == {"git-helper", "web-search"}
    assert by_id["git-helper"]["tags"] == ["git", "vcs"]
    assert by_id["web-search"]["tags"] == ["web", "search"]
    assert by_id["git-helper"]["description"] == "Work with git"
    assert by_id["git-helper"]["source"] == "hermes"


def test_skills_sync_scan_respects_depth_limit(tmp_path):
    _write_skill(tmp_path, "a/b/c/d/too-deep", "too-deep", "buried")
    _write_skill(tmp_path, "ok", "ok", "fine")
    assert [o["entity_id"] for o in skills_sync.scan(tmp_path)] == ["ok"]


def test_skills_sync_scan_missing_dir_is_empty(tmp_path):
    assert skills_sync.scan(tmp_path / "nope") == []


def test_skills_sync_frontmatter_fallback_to_dirname(tmp_path):
    skill_dir = tmp_path / "anon-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    obs = skills_sync.scan(tmp_path)
    assert obs[0]["entity_id"] == "anon-skill"


def test_skills_sync_main_dry_run_no_network(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setenv("HERMES_SKILLS_DIR", str(tmp_path))
    _write_skill(tmp_path, "git-helper", "git-helper", "Work with git")
    assert skills_sync.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would report 1 skills" in out
    assert "git-helper" in out


def test_skills_sync_main_no_skills_no_network(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setenv("HERMES_SKILLS_DIR", str(tmp_path / "empty"))
    assert skills_sync.main([]) == 0
    assert "No skills found" in capsys.readouterr().out


# ---------------------------------------------------------------------------

def test_worker_install_commands_are_published():
    sidecar = Path(__file__).resolve().parents[1]
    scripts = tomllib.loads((sidecar / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["apsimo-queue-worker"] == "apsimo.workers.queue_worker:main"
    assert scripts["apsimo-skills-sync"] == "apsimo.workers.skills_sync:main"
    deploy = sidecar / "apsimo/workers/deploy"
    plist = plistlib.loads((deploy / "colony-worker.plist").read_bytes())
    executable = plist["ProgramArguments"][0].split("/")[-1]
    assert scripts[executable] == "apsimo.workers.colony_worker:main"
    service = (deploy / "colony-worker.service").read_text()
    command = next(line.split("=", 1)[1] for line in service.splitlines()
                   if line.startswith("ExecStart="))
    assert scripts[command.split("/")[-1]] == "apsimo.workers.colony_worker:main"
