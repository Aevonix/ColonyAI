"""Colony worker daemon -- claims typed jobs and executes them with an LLM
(cognition program, Phase B item 5).

This is the installable, capability-typed worker half of the Colony queue.
It authenticates to the sidecar, advertises its capabilities, claims eligible
jobs, reasons over each with an OpenAI-compatible LLM using a READ/ANALYSE
tool posture ONLY, and posts a structured report the sidecar audits.

Trust model: the worker is UNTRUSTED. It never performs mutations itself and
never reports one -- the sidecar's WorkerGovernor re-checks capability
coverage and owner boundaries at claim time and audits the report at
completion (a mutation on a read-only job is a server-side violation). Keeping
the worker read/analyse-only is what makes "never trust the client" cheap:
there is nothing dangerous for it to do.

Deliberately stdlib-only (like the other workers/ modules) so it runs on hosts
that have the agent but not the full sidecar dependency stack. The report
schema (summary, operations, files_touched, commits, branch, confidence)
matches what governor.audit_report expects.

Native alternative (preferred where available): rather than this bespoke
executor, a deployment can drive work through Hermes kanban (dispatcher +
worker subprocess) or the gateway's structured-runs API (/v1/runs + SSE +
approval), and let this governor enforce/audit the same way. This daemon is
the dependency-light reference path; see the private deployment layer for the
host/LLM/capability placement.

Environment:
  COLONY_URL                 sidecar base URL     (default http://127.0.0.1:7777)
  COLONY_API_KEY             sidecar API key      (default dev-mode-no-key)
  COLONY_WORKER_NODE_ID      worker identity      (default <agent>-worker)
  COLONY_WORKER_CAPABILITIES csv capabilities     (default research,analyst,read)
  COLONY_WORKER_JOB_TYPES    csv job types        (default research)
  COLONY_WORKER_MAX_JOBS     jobs per cycle       (default 1)
  COLONY_WORKER_POLL_SECS    seconds between polls (default 30; daemon mode)
  COLONY_WORKER_LLM_BASE_URL OpenAI-compatible base (default OPENAI_BASE_URL)
  COLONY_WORKER_LLM_MODEL    model name           (default gpt-4o-mini)
  COLONY_WORKER_LLM_API_KEY  LLM key              (default OPENAI_API_KEY)
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional

# Compact worker doctrine (mirrors cognition/charter.py role="worker"; inlined
# to keep this daemon stdlib-only and runnable without the sidecar package).
_WORKER_SYSTEM = (
    "You are a Colony worker agent executing ONE claimed job with a read and "
    "analysis posture only. You never modify files, commit, push, deploy, or "
    "send anything -- you observe, reason, and report. Rules: stay strictly "
    "inside the job's stated scope; verify before asserting; if the job cannot "
    "be done, say so plainly with the reason; never fabricate a result. "
    "Respond with ONLY a JSON object (no prose, no markdown fences):\n"
    '{"summary": str (outcome first, then evidence), '
    '"operations": [str] (only from: analyze, read, search), '
    '"files_touched": [] (always empty -- you do not touch files), '
    '"commits": 0, "branch": "", '
    '"confidence": float 0.0-1.0 (calibrated: your honesty here trains earned '
    'autonomy), "remaining_work": str}'
)


def load_config() -> Dict[str, Any]:
    """Resolve worker config from the environment (at call time)."""
    node_id = os.environ.get("COLONY_WORKER_NODE_ID") or (
        os.environ.get("COLONY_AGENT_NAME", "colony").strip().lower().replace(" ", "-")
        + "-worker"
    )
    caps = [c.strip() for c in os.environ.get(
        "COLONY_WORKER_CAPABILITIES", "research,analyst,read").split(",") if c.strip()]
    job_types = [t.strip() for t in os.environ.get(
        "COLONY_WORKER_JOB_TYPES", "research").split(",") if t.strip()]
    return {
        "colony_url": os.environ.get("COLONY_URL", "http://127.0.0.1:7777").rstrip("/"),
        "api_key": os.environ.get("COLONY_API_KEY", "dev-mode-no-key"),
        "node_id": node_id,
        "capabilities": caps,
        "job_types": job_types,
        "max_jobs": int(os.environ.get("COLONY_WORKER_MAX_JOBS", "1")),
        "poll_secs": float(os.environ.get("COLONY_WORKER_POLL_SECS", "30")),
        "llm_base_url": (os.environ.get("COLONY_WORKER_LLM_BASE_URL")
                         or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/"),
        "llm_model": os.environ.get("COLONY_WORKER_LLM_MODEL", "gpt-4o-mini"),
        "llm_api_key": (os.environ.get("COLONY_WORKER_LLM_API_KEY")
                        or os.environ.get("OPENAI_API_KEY", "")),
    }


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {"X-API-Key": cfg["api_key"], "Content-Type": "application/json"}


def _post(cfg: Dict[str, Any], url: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=_headers(cfg), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def register_worker(cfg: Dict[str, Any]) -> None:
    """Registration is ephemeral (lost on sidecar restart) -- re-register."""
    _post(cfg, f"{cfg['colony_url']}/v1/host/queue/workers/register", {
        "node_id": cfg["node_id"],
        "capabilities": cfg["capabilities"],
        "job_types": cfg["job_types"],
        "max_concurrent": 1,
        "available": True,
        "load": 0.0,
    })


def claim_job(cfg: Dict[str, Any]) -> Optional[dict]:
    return _post(cfg, f"{cfg['colony_url']}/v1/host/queue/jobs/claim", {
        "node_id": cfg["node_id"],
        "capabilities": cfg["capabilities"],
        "job_types": cfg["job_types"],
    }) or None


def build_llm_messages(job: dict) -> List[Dict[str, str]]:
    """Compose the worker LLM messages for a claimed job (pure -- testable)."""
    payload = job.get("payload") or {}
    parts = []
    for k in ("description", "action_hint", "domain"):
        v = payload.get(k)
        if v:
            parts.append(f"{k}: {v}")
    ctx = payload.get("context")
    if ctx:
        parts.append("context: " + json.dumps(ctx, default=str)[:2000])
    user = "JOB:\n" + "\n".join(parts) if parts else "JOB: (no description)"
    return [{"role": "system", "content": _WORKER_SYSTEM},
            {"role": "user", "content": user}]


def _parse_report(content: str) -> Dict[str, Any]:
    """Parse the LLM's JSON report; sanitise to the read/analyse contract."""
    text = (content or "").strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    if not text.startswith("{"):
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        text = m.group(0) if m else "{}"
    try:
        data = json.loads(text)
    except Exception:
        data = {"summary": (content or "")[:600]}
    allowed_ops = {"analyze", "read", "search"}
    ops = [str(o).lower() for o in (data.get("operations") or [])
           if str(o).lower() in allowed_ops] or ["analyze"]
    conf = data.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.5
    # Read/analyse posture is enforced client-side too: never report a mutation.
    return {
        "summary": str(data.get("summary", ""))[:2000],
        "operations": ops,
        "files_touched": [],
        "commits": 0,
        "branch": "",
        "confidence": conf,
        "remaining_work": str(data.get("remaining_work", ""))[:600],
    }


def call_llm(cfg: Dict[str, Any], messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """Call the OpenAI-compatible endpoint; return a parsed report."""
    if not cfg["llm_base_url"]:
        raise RuntimeError("no COLONY_WORKER_LLM_BASE_URL configured")
    body = {"model": cfg["llm_model"], "messages": messages, "temperature": 0.2}
    req = urllib.request.Request(
        f"{cfg['llm_base_url']}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['llm_api_key']}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _parse_report(content)


def execute_job(cfg: Dict[str, Any], job: dict) -> bool:
    """Run one claimed job to completion (report) or failure."""
    job_id = job.get("job_id") or job.get("id")
    try:
        report = call_llm(cfg, build_llm_messages(job))
    except Exception as exc:
        try:
            _post(cfg, f"{cfg['colony_url']}/v1/host/queue/jobs/{job_id}/fail",
                  {"error": f"worker execution failed: {exc}"[:400]})
        except Exception:
            pass
        return False
    try:
        resp = _post(cfg, f"{cfg['colony_url']}/v1/host/queue/jobs/{job_id}/complete",
                     {"output": report})
        verdict = resp.get("verdict", "?")
        print(f"Completed job {job_id} (verdict={verdict}, "
              f"confidence={report['confidence']})")
        return True
    except Exception as exc:
        print(f"Complete post failed for {job_id}: {exc}")
        return False


def run_cycle(cfg: Dict[str, Any]) -> int:
    """One poll cycle: register, claim up to max_jobs, execute each."""
    try:
        register_worker(cfg)
    except Exception as exc:
        print(f"Worker registration failed (sidecar down?): {exc}")
        return 0
    done = 0
    for _ in range(cfg["max_jobs"]):
        try:
            job = claim_job(cfg)
        except Exception as exc:
            print(f"Claim failed: {exc}")
            break
        if not job:
            break
        if execute_job(cfg, job):
            done += 1
    if not done:
        print("No eligible jobs.")
    return done


def run_forever(cfg: Dict[str, Any]) -> None:
    print(f"colony-worker {cfg['node_id']} polling {cfg['colony_url']} "
          f"every {cfg['poll_secs']}s (caps={cfg['capabilities']}, "
          f"types={cfg['job_types']})")
    while True:
        try:
            run_cycle(cfg)
        except Exception as exc:
            print(f"cycle error: {exc}")
        time.sleep(cfg["poll_secs"])


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="colony-worker",
        description="Claim typed Colony jobs and execute them read/analyse-only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print resolved config and exit (no network)")
    parser.add_argument("--once", action="store_true",
                        help="run a single poll cycle and exit")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.dry_run:
        safe = dict(cfg)
        safe["api_key"] = "***" if cfg["api_key"] else ""
        safe["llm_api_key"] = "***" if cfg["llm_api_key"] else ""
        print("colony-worker (dry run -- no network calls):")
        for k, v in safe.items():
            print(f"  {k}: {v}")
        return 0
    if args.once:
        run_cycle(cfg)
        return 0
    run_forever(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
