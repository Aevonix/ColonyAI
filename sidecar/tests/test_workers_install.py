"""Scheduled agent workers (v0.20.0).

Covers:
- the packaged worker modules (colony_sidecar.workers.*): import, config
  resolution from env, --dry-run main() paths (no network), and the pure
  scan/claim/payload helpers
- the wizard's cron helpers: command construction (console script vs
  ``python -m`` fallback), cron line construction (env-file prefix, log
  redirection), crontab merge idempotency, and the install path with an
  injected ``run``
- the back-compat wrapper scripts under plugins/hermes-plugin/poller/
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from colony_sidecar import setup as wizard
from colony_sidecar.setup import (
    WORKER_SPECS,
    build_cron_lines,
    build_worker_command,
    install_cron_jobs,
    merge_crontab,
    run_workers_step,
)
from colony_sidecar.workers import queue_worker, skills_sync


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "COLONY_URL", "COLONY_API_KEY", "COLONY_JOBS_WEBHOOK_URL",
        "COLONY_WORKER_NODE_ID", "COLONY_AGENT_NAME", "COLONY_WORKER_MAX_JOBS",
        "HERMES_SKILLS_DIR", "COLONY_STATE_DIR", "COLONY_HOME",
        "COLONY_INIT_DEFAULTS",
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
        "payload": {"action_hint": "agent_sync_github", "risk": "read_only",
                    "domain": "github", "description": "look around"},
    }
    payload = queue_worker.build_webhook_payload(cfg, job)
    inner = payload["payload"]
    assert payload["type"] == "agent_job"
    assert inner["job_id"] == "job-42"
    assert inner["action_hint"] == "agent_sync_github"
    assert inner["observations_url"] == "http://127.0.0.1:7777/v1/host/observations"
    assert inner["complete_url"] == "http://127.0.0.1:7777/v1/host/queue/jobs/job-42/complete"
    assert inner["fail_url"] == "http://127.0.0.1:7777/v1/host/queue/jobs/job-42/fail"
    assert inner["api_key_header"] == "X-API-Key"


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
# Cron command / line construction
# ---------------------------------------------------------------------------

def _which_none(name):
    return None


def _which_console(name):
    return f"/usr/local/bin/{name}" if name.startswith("colony-") else None


def test_build_worker_command_prefers_console_script():
    cmd = build_worker_command(
        "colony-queue-worker", "colony_sidecar.workers.queue_worker",
        which=_which_console,
    )
    assert cmd == "/usr/local/bin/colony-queue-worker"


def test_build_worker_command_falls_back_to_module():
    cmd = build_worker_command(
        "colony-queue-worker", "colony_sidecar.workers.queue_worker",
        which=_which_none, python="/opt/venv/bin/python",
    )
    assert cmd == "/opt/venv/bin/python -m colony_sidecar.workers.queue_worker"


def test_build_worker_command_default_python_is_current_interpreter():
    cmd = build_worker_command("colony-skills-sync",
                               "colony_sidecar.workers.skills_sync",
                               which=_which_none)
    assert cmd.startswith(sys.executable + " -m ")


def test_build_cron_lines_console_script(tmp_path):
    lines = build_cron_lines(
        env_file="/home/me/colony/.env",
        log_dir="/home/me/.colony/logs",
        workdir="/home/me/.colony",
        which=_which_console,
    )
    assert len(lines) == len(WORKER_SPECS) == 2
    qw, sync = lines
    assert qw.startswith("*/5 * * * * ")
    assert sync.startswith("0 9 * * * ")
    # env-file prefix: cd + exported source of the wizard's .env
    for line in lines:
        assert "cd /home/me/.colony && set -a; . /home/me/colony/.env; set +a;" in line
    # commands + per-worker log redirection
    assert "/usr/local/bin/colony-queue-worker" in qw
    assert qw.endswith(">> /home/me/.colony/logs/cron-colony-queue-worker.log 2>&1")
    assert "/usr/local/bin/colony-skills-sync" in sync
    assert sync.endswith(">> /home/me/.colony/logs/cron-colony-skills-sync.log 2>&1")


def test_build_cron_lines_module_fallback():
    lines = build_cron_lines(
        env_file="/e/.env", log_dir="/l", workdir="/w",
        which=_which_none, python="/opt/venv/bin/python",
    )
    assert "/opt/venv/bin/python -m colony_sidecar.workers.queue_worker" in lines[0]
    assert "/opt/venv/bin/python -m colony_sidecar.workers.skills_sync" in lines[1]


# ---------------------------------------------------------------------------
# Crontab merge (idempotency)
# ---------------------------------------------------------------------------

def _lines(which=_which_console):
    return build_cron_lines(env_file="/e/.env", log_dir="/l", workdir="/w",
                            which=which, python="/opt/venv/bin/python")


def test_merge_into_empty_crontab_adds_both():
    merged, added = merge_crontab("", _lines())
    assert added == _lines()
    assert merged == "\n".join(_lines()) + "\n"


def test_merge_preserves_existing_entries():
    existing = "MAILTO=root\n0 3 * * * /usr/local/bin/backup.sh\n"
    merged, added = merge_crontab(existing, _lines())
    assert merged.startswith(existing)
    assert len(added) == 2
    assert merged.endswith("\n")


def test_merge_is_idempotent():
    merged, added = merge_crontab("", _lines())
    assert added
    merged2, added2 = merge_crontab(merged, _lines())
    assert added2 == []
    assert merged2 == merged


def test_merge_skips_worker_already_referenced_in_other_form():
    # Hand-installed `python -m` entry must block the console-script line
    # for the same worker (and vice versa) — never schedule a worker twice.
    existing = "*/2 * * * * /opt/venv/bin/python -m colony_sidecar.workers.queue_worker\n"
    merged, added = merge_crontab(existing, _lines(which=_which_console))
    assert len(added) == 1
    assert "colony-skills-sync" in added[0]
    assert merged.count("queue_worker") + merged.count("colony-queue-worker") == 1


def test_merge_skips_each_worker_independently():
    existing = "0 9 * * * /usr/local/bin/colony-skills-sync >> /l/x.log 2>&1\n"
    merged, added = merge_crontab(existing, _lines())
    assert len(added) == 1
    assert "colony-queue-worker" in added[0]


# ---------------------------------------------------------------------------
# install_cron_jobs (injected subprocess runner)
# ---------------------------------------------------------------------------

class FakeRun:
    """Record crontab invocations; emulate read (-l) and write (-)."""

    def __init__(self, existing="", read_rc=0, write_rc=0):
        self.existing = existing
        self.read_rc = read_rc
        self.write_rc = write_rc
        self.written = None
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd == ["crontab", "-l"]:
            return SimpleNamespace(returncode=self.read_rc,
                                   stdout=self.existing, stderr="")
        if cmd == ["crontab", "-"]:
            self.written = kwargs.get("input")
            return SimpleNamespace(returncode=self.write_rc, stdout="",
                                   stderr="permission denied" if self.write_rc else "")
        raise AssertionError(f"unexpected command {cmd}")


def test_install_cron_jobs_writes_merged_crontab():
    fake = FakeRun(existing="0 3 * * * /usr/local/bin/backup.sh\n")
    added = install_cron_jobs(_lines(), run=fake)
    assert len(added) == 2
    assert fake.written.startswith("0 3 * * * /usr/local/bin/backup.sh\n")
    assert "colony-queue-worker" in fake.written
    assert "colony-skills-sync" in fake.written


def test_install_cron_jobs_no_crontab_yet_treated_as_empty():
    fake = FakeRun(existing="no crontab for user\n", read_rc=1)
    added = install_cron_jobs(_lines(), run=fake)
    assert len(added) == 2
    assert "no crontab for user" not in fake.written


def test_install_cron_jobs_already_installed_skips_write():
    first = FakeRun()
    install_cron_jobs(_lines(), run=first)
    rerun = FakeRun(existing=first.written)
    added = install_cron_jobs(_lines(), run=rerun)
    assert added == []
    assert rerun.written is None
    assert ["crontab", "-"] not in rerun.calls


def test_install_cron_jobs_write_failure_raises():
    fake = FakeRun(write_rc=1)
    with pytest.raises(RuntimeError, match="permission denied"):
        install_cron_jobs(_lines(), run=fake)


# ---------------------------------------------------------------------------
# run_workers_step (wizard UX, scripted answers)
# ---------------------------------------------------------------------------

def make_ask(answers):
    queue = list(answers)
    return lambda prompt_text: queue.pop(0) if queue else ""


@pytest.fixture
def step_env(monkeypatch, tmp_path):
    """Linux platform, crontab present, console scripts absent, tmp HOME dirs."""
    monkeypatch.setenv("COLONY_HOME", str(tmp_path / "colony-home"))
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "colony-home" / "data"))
    monkeypatch.setattr(wizard.platform, "system", lambda: "Linux")
    env_path = tmp_path / "colony" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("COLONY_API_KEY=k\n")
    return env_path


def _step_which(name):
    return "/usr/bin/crontab" if name == "crontab" else None


def test_workers_step_installs_cron_on_yes(step_env, tmp_path, capsys):
    fake = FakeRun()
    run_workers_step(step_env, ask=make_ask(["y", "y"]), run=fake, which=_step_which)
    out = capsys.readouterr().out
    assert "Installed 2 crontab entries" in out
    assert "colony_sidecar.workers.queue_worker" in fake.written
    assert "colony_sidecar.workers.skills_sync" in fake.written
    # env file is sourced with export, from the state-dir parent
    assert f". {step_env.resolve()}; set +a;" in fake.written
    assert f"cd {tmp_path / 'colony-home'} && set -a" in fake.written
    # logs land under $COLONY_HOME/logs and the dir was created
    assert f"{tmp_path}/colony-home/logs/cron-colony-queue-worker.log" in fake.written
    assert (tmp_path / "colony-home" / "logs").is_dir()


def test_workers_step_rerun_is_idempotent(step_env, capsys):
    first = FakeRun()
    run_workers_step(step_env, ask=make_ask(["y", "y"]), run=first, which=_step_which)
    rerun = FakeRun(existing=first.written)
    run_workers_step(step_env, ask=make_ask(["y", "y"]), run=rerun, which=_step_which)
    assert rerun.written is None
    assert "already installed" in capsys.readouterr().out


def test_workers_step_agent_elsewhere_prints_manual_lines(step_env, capsys):
    fake = FakeRun()
    run_workers_step(step_env, ask=make_ask(["n"]), run=fake, which=_step_which)
    out = capsys.readouterr().out
    assert fake.calls == []  # no crontab interaction at all
    assert "crontab -e" in out
    assert "*/5 * * * *" in out and "0 9 * * *" in out
    assert "colony_sidecar.workers.queue_worker" in out


def test_workers_step_no_crontab_prints_manual_lines(step_env, capsys):
    fake = FakeRun()
    run_workers_step(step_env, ask=make_ask(["y"]), run=fake, which=lambda name: None)
    out = capsys.readouterr().out
    assert fake.calls == []
    assert "crontab not found" in out
    assert "*/5 * * * *" in out


def test_workers_step_unsupported_platform_prints_manual_lines(
    step_env, monkeypatch, capsys
):
    monkeypatch.setattr(wizard.platform, "system", lambda: "Windows")
    fake = FakeRun()
    run_workers_step(step_env, ask=make_ask(["y"]), run=fake, which=_step_which)
    out = capsys.readouterr().out
    assert fake.calls == []
    assert "unsupported platform" in out


def test_workers_step_install_failure_degrades_to_manual(step_env, capsys):
    fake = FakeRun(write_rc=1)
    run_workers_step(step_env, ask=make_ask(["y", "y"]), run=fake, which=_step_which)
    out = capsys.readouterr().out
    assert "Crontab install failed" in out
    assert "crontab -e" in out  # manual fallback printed


def test_workers_step_non_interactive_defaults_to_install(step_env, capsys):
    fake = FakeRun()
    run_workers_step(step_env, non_interactive=True, run=fake, which=_step_which)
    assert fake.written is not None
    assert "colony-queue-worker" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Back-compat wrapper scripts
# ---------------------------------------------------------------------------

_POLLER_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin" / "poller"


@pytest.mark.skipif(not _POLLER_DIR.is_dir(), reason="repo poller dir not present")
@pytest.mark.parametrize("script,needle", [
    ("colony-queue-worker.py", "dry run"),
    ("colony-skills-sync.py", "No skills found"),
])
def test_wrapper_scripts_delegate_to_package(script, needle, tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "HERMES_SKILLS_DIR": str(tmp_path / "no-skills"),
    }
    args = [sys.executable, str(_POLLER_DIR / script)]
    if script == "colony-queue-worker.py":
        args.append("--dry-run")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
    assert proc.returncode == 0, proc.stderr
    assert needle in proc.stdout
