"""Post-action verification: what actually happened vs the granted scope.

Two evidence sources, best-first:
  1. Repo inspection (when a read-only local mirror of the target exists):
     fetch the work branch and diff it against its merge-base -- files touched,
     commit count, branch name are verified MECHANICALLY.
  2. The delegate's structured report, cross-checked field by field.

Any out-of-scope observation is a VIOLATION: flagged loudly, recorded on the
task, and fed to the outcome feedback store by the service.
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from typing import Any, Dict, List, Optional

from colony_sidecar.directed.models import ScopedTask, MUTATE_OPS

logger = logging.getLogger(__name__)


def _match_globs(path: str, globs: List[str]) -> bool:
    p = (path or "").lstrip("./")
    for g in globs or ["**"]:
        if g in ("**", "*") or fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.rstrip("/") + "/**"):
            return True
    return False


def _git(mirror_path: str, *args: str, timeout: int = 30) -> str:
    out = subprocess.run(
        ["git", "-C", mirror_path, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return out.stdout


def audit_via_mirror(task: ScopedTask, mirror_path: str, branch: str,
                     base: str = "HEAD") -> Dict[str, Any]:
    """Mechanical audit from a read-only mirror: fetch the branch, diff vs base."""
    findings: List[str] = []
    ok = True
    try:
        _git(mirror_path, "fetch", "origin", branch, timeout=60)
        merge_base = _git(mirror_path, "merge-base", base, "FETCH_HEAD").strip()
        commits = _git(mirror_path, "rev-list", "--count", f"{merge_base}..FETCH_HEAD").strip()
        n_commits = int(commits or 0)
        files = [f for f in _git(
            mirror_path, "diff", "--name-only", merge_base, "FETCH_HEAD",
        ).splitlines() if f.strip()]
    except Exception as exc:
        return {"method": "mirror", "ok": None,
                "findings": [f"mirror inspection unavailable: {exc}"]}

    if not branch.startswith(task.limits.branch_prefix):
        ok = False
        findings.append(f"branch {branch!r} outside required prefix {task.limits.branch_prefix!r}")
    if n_commits > task.limits.max_commits:
        ok = False
        findings.append(f"{n_commits} commits exceeds cap {task.limits.max_commits}")
    off = [f for f in files if not _match_globs(f, task.limits.path_globs)]
    if off:
        ok = False
        findings.append(f"files outside allowed paths: {off[:10]}")
    return {"method": "mirror", "ok": ok, "branch": branch,
            "commits": n_commits, "files_touched": files[:100], "findings": findings}


def audit_via_report(task: ScopedTask, report: Dict[str, Any]) -> Dict[str, Any]:
    """Cross-check the delegate's structured report against the scope."""
    findings: List[str] = []
    ok = True
    report = report or {}

    ops = [str(o).lower() for o in (report.get("operations") or [])]
    illegal = [o for o in ops if o not in set(task.allowed_ops)]
    if illegal:
        ok = False
        findings.append(f"operations outside scope: {illegal}")

    branch = str(report.get("branch") or "")
    did_mutate = bool(set(ops) & MUTATE_OPS) or bool(report.get("commits"))
    if did_mutate and not task.mutating:
        ok = False
        findings.append("delegate mutated on a read-only scope")
    if did_mutate and branch and not branch.startswith(task.limits.branch_prefix):
        ok = False
        findings.append(f"branch {branch!r} outside required prefix {task.limits.branch_prefix!r}")

    n_commits = int(report.get("commits") or 0)
    if n_commits > task.limits.max_commits:
        ok = False
        findings.append(f"{n_commits} commits exceeds cap {task.limits.max_commits}")

    if report.get("force_push"):
        ok = False
        findings.append("force push reported (never allowed)")
    if report.get("deletions") and not task.limits.delete_allowed:
        deld = report.get("deletions")
        off = [f for f in deld if not _match_globs(str(f), task.limits.path_globs)] if isinstance(deld, list) else [str(deld)]
        if off:
            ok = False
            findings.append(f"deletes outside scope: {off[:10]}")

    files = [str(f) for f in (report.get("files_touched") or [])]
    off = [f for f in files if not _match_globs(f, task.limits.path_globs)]
    if off:
        ok = False
        findings.append(f"files outside allowed paths: {off[:10]}")

    missing = [k for k in task.reporting if k not in report]
    if missing:
        findings.append(f"report missing expected fields: {missing}")

    return {"method": "report", "ok": ok, "branch": branch, "commits": n_commits,
            "files_touched": files[:100], "findings": findings}


def audit_completion(task: ScopedTask, report: Dict[str, Any],
                     mirror_path: Optional[str] = None) -> Dict[str, Any]:
    """Full audit: mirror inspection when reachable, report cross-check always.

    Verdict: 'clean' | 'violation' | 'unverified'. A violation from EITHER
    source wins (fail loud).
    """
    report_audit = audit_via_report(task, report)
    mirror_audit = None
    branch = report_audit.get("branch") or ""
    if mirror_path and branch:
        mirror_audit = audit_via_mirror(task, mirror_path, branch)

    oks = [a["ok"] for a in (report_audit, mirror_audit) if a is not None]
    if any(o is False for o in oks):
        verdict = "violation"
    elif any(o is True for o in oks):
        verdict = "clean"
    else:
        verdict = "unverified"

    result = {
        "verdict": verdict,
        "report_audit": report_audit,
        "mirror_audit": mirror_audit,
    }
    if verdict == "violation":
        all_findings = report_audit.get("findings", []) + (
            (mirror_audit or {}).get("findings", []) if mirror_audit else [])
        logger.warning("DIRECTED-ACTION SCOPE VIOLATION on %s: %s",
                       task.id, "; ".join(all_findings)[:400])
    return result
