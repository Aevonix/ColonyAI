"""Harness (Hermes) cron integration for feed instances.

All scheduling goes through the ``hermes cron`` CLI; the one thing the CLI
cannot do — pinning a job to an explicit provider/model so global inference
config drift can never silently skip a feed job — is done by editing the
scheduler's jobs.json directly (with a timestamped backup first).

Paths/binaries are overridable via env for non-standard installs:
  COLONY_HERMES_BIN   hermes CLI (default: ``hermes`` on PATH, then the
                      conventional venv location)
  COLONY_HERMES_HOME  harness home (default ~/.hermes)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time


def hermes_home() -> str:
    return os.path.expanduser(os.environ.get("COLONY_HERMES_HOME", "~/.hermes"))


def hermes_bin() -> str:
    env = os.environ.get("COLONY_HERMES_BIN")
    if env:
        return os.path.expanduser(env)
    on_path = shutil.which("hermes")
    if on_path:
        return on_path
    conventional = os.path.join(hermes_home(), "hermes-agent/venv/bin/hermes")
    return conventional if os.path.exists(conventional) else "hermes"


def jobs_json_path() -> str:
    return os.path.join(hermes_home(), "cron/jobs.json")


def scripts_dir() -> str:
    return os.path.join(hermes_home(), "scripts")


def _run(args: list[str], timeout: int = 60) -> str:
    proc = subprocess.run([hermes_bin(), *args], capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"hermes {' '.join(args[:2])} failed: "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _load_jobs() -> list[dict]:
    try:
        with open(jobs_json_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    return data.get("jobs", data) if isinstance(data, dict) else data


def find_jobs(name_prefix: str) -> list[dict]:
    return [j for j in _load_jobs()
            if isinstance(j, dict) and (j.get("name") or "").startswith(name_prefix)]


def create_job(name: str, schedule: str, *, prompt: str | None = None,
               script: str | None = None, no_agent: bool = False,
               deliver: str = "local") -> str:
    """Create a job and return its id (resolved by unique name lookup)."""
    if find_jobs(name):
        raise RuntimeError(f"a cron job named {name!r} already exists")
    args = ["cron", "create", schedule, "--name", name, "--deliver", deliver]
    if script:
        args += ["--script", script]
    if no_agent:
        args += ["--no-agent"]
    if prompt is not None:
        args.insert(3, prompt)  # positional prompt right after the schedule
    _run(args)
    jobs = find_jobs(name)
    if len(jobs) != 1:
        raise RuntimeError(f"created job {name!r} but found {len(jobs)} matches in jobs.json")
    return jobs[0]["id"]


def pin_model(job_ids: list[str], provider: str, model: str) -> None:
    """Pin jobs to an explicit provider/model in jobs.json (backup first)."""
    if not (provider and model):
        return
    path = jobs_json_path()
    shutil.copy2(path, f"{path}.bak-feeds-{int(time.time())}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    hit = 0
    for j in jobs:
        if isinstance(j, dict) and j.get("id") in job_ids:
            j["provider"] = provider
            j["model"] = model
            hit += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if hit != len(job_ids):
        raise RuntimeError(f"pinned {hit}/{len(job_ids)} jobs — jobs.json out of sync")


def remove_job(job_id: str) -> None:
    _run(["cron", "remove", job_id])


def pause_job(job_id: str) -> None:
    _run(["cron", "pause", job_id])


def resume_job(job_id: str) -> None:
    _run(["cron", "resume", job_id])


def run_job_detached(job_id: str) -> None:
    """Trigger a run without blocking: agent jobs can run for many minutes."""
    subprocess.Popen([hermes_bin(), "cron", "run", job_id],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
