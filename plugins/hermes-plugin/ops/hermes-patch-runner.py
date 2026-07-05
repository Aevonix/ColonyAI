#!/usr/bin/env python3
"""Hermes patch runner: a registry of guarded, idempotent host-side patches.

Some integration behavior can only be achieved by patching the Hermes agent
framework's source (no hook/config seam exists yet). Hand-editing the framework
checkout is forbidden practice: edits are silently lost on every update and
nobody can tell what was changed. This runner makes such patches REPEATABLE,
INSPECTABLE and SAFE:

  - every patch is a small standalone script with its own anchor checks
  - the registry is a directory of those scripts (one file = one patch)
  - `status` verifies without touching anything, `apply` is safe to re-run
  - a patch whose anchors no longer match the installed framework version
    reports FUNDAMENTAL_CHANGE and is never blind-applied
  - after a framework update, one `apply` run re-applies everything that the
    update reverted, and clearly names anything that must be re-authored

Registry
--------
The registry directory (default ``~/.hermes/patches``, override with
``HERMES_PATCH_DIR`` or ``--dir``) holds executable patch scripts named
``*_patch.py`` (or any executable file ending in ``_patch``). Disable a patch
without deleting it by renaming it to ``<name>.disabled``.

Patch contract (each script must implement)
-------------------------------------------
Invocation:
  ``<script>``            check-only: NEVER modifies the target
  ``<script> --apply``    apply if missing (and only if anchors match exactly)

Exit codes / first line of output:
  0  "OK: ..."       patch already present (marker found)
  0  "APPLIED: ..."  (apply mode) patch applied now; a service restart is
                     usually required to load it
  1  "MISSING: ..."  (check mode) patch absent but anchors intact: re-appliable
  2  "ERROR: FUNDAMENTAL_CHANGE ..."  the target no longer matches the known
                     original; the patch must be re-authored for this version.
                     The target was NOT modified.
  3  "ERROR: ..."    apply was attempted, post-apply validation (py_compile,
                     node --check, ...) failed, and the target was ROLLED BACK.

A patch script should:
  - define a MARKER string that exists in the target iff the patch is applied
  - define exact ANCHOR block(s) of the original code, and require each to
    match exactly once before replacing
  - back up the target next to it before writing, validate after writing,
    and restore the backup on validation failure

Runner usage
------------
  hermes-patch-runner.py status  [--dir DIR] [--only NAME] [--json]
  hermes-patch-runner.py apply   [--dir DIR] [--only NAME] [--json]
  hermes-patch-runner.py list    [--dir DIR] [--json]

Runner exit codes:
  status: 0 all applied | 1 some missing (re-appliable) | 2 drift/failure
  apply:  0 all applied (restart may be needed; see output) | 2 drift | 3 rollback
  list:   0

Everything is stdlib-only and framework-version agnostic: the runner knows
nothing about individual patches beyond the contract above.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEFAULT_DIR = os.environ.get(
    "HERMES_PATCH_DIR", os.path.expanduser("~/.hermes/patches")
)
PATCH_TIMEOUT = int(os.environ.get("HERMES_PATCH_TIMEOUT", "60"))

# state name -> (runner exit contribution, human tag)
_STATES = {
    "ok": (0, "OK"),
    "applied": (0, "APPLIED"),
    "missing": (1, "MISSING"),
    "fundamental-change": (2, "FUNDAMENTAL_CHANGE"),
    "rollback": (3, "ROLLED_BACK"),
    "error": (2, "ERROR"),
}


def discover(directory: str) -> list[str]:
    """Return sorted paths of enabled patch scripts in the registry dir."""
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".disabled") or name.startswith("."):
            continue
        if not (name.endswith("_patch.py") or name.endswith("_patch")):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            out.append(path)
    return out


def describe(path: str) -> str:
    """First docstring/comment line of a patch script (best-effort)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            in_doc = False
            for line in f:
                s = line.strip()
                if not in_doc and (s.startswith('"""') or s.startswith("'''")):
                    s = s[3:].strip()
                    if s.endswith('"""') or s.endswith("'''"):
                        s = s[:-3].strip()
                    if s:
                        return s
                    in_doc = True
                    continue
                if in_doc and s:
                    return s.rstrip("\"'")
                if s.startswith("#") and "!" not in s[:2]:
                    return s.lstrip("# ").strip()
    except Exception:
        pass
    return ""


def run_patch(path: str, apply: bool) -> dict:
    """Execute one patch script per the contract; classify the result."""
    cmd = [sys.executable, path] if path.endswith(".py") else [path]
    if apply:
        cmd.append("--apply")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PATCH_TIMEOUT
        )
        rc = proc.returncode
        out = (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        rc, out = 124, f"timeout after {PATCH_TIMEOUT}s"
    except Exception as exc:  # unrunnable script counts as an error, not a crash
        rc, out = 125, f"could not execute: {exc}"

    last = out.splitlines()[-1] if out else f"exit {rc}"
    if rc == 0:
        # Contract: a fresh apply announces itself with an "APPLIED" prefix on
        # the last output line. Prefix-match (not substring) so loose wordings
        # like "already applied" from contract-adjacent scripts read as OK
        # instead of triggering a false re-applied/restart-needed signal on
        # every doctor run.
        state = "applied" if last.upper().startswith("APPLIED") else "ok"
    elif rc == 1 and not apply:
        state = "missing"
    elif rc == 2 or "FUNDAMENTAL_CHANGE" in out.upper():
        state = "fundamental-change"
    elif rc == 3:
        state = "rollback"
    else:
        state = "error"
    return {
        "name": os.path.basename(path),
        "path": path,
        "state": state,
        "exit_code": rc,
        "detail": last,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Registry runner for guarded, idempotent Hermes host patches."
    )
    ap.add_argument("command", choices=["status", "apply", "list"])
    ap.add_argument("--dir", default=DEFAULT_DIR, help="patch registry directory")
    ap.add_argument("--only", action="append", default=[],
                    help="run only patches whose filename contains this substring "
                         "(repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    patches = discover(args.dir)
    if args.only:
        patches = [p for p in patches
                   if any(f in os.path.basename(p) for f in args.only)]

    if args.command == "list":
        rows = [{"name": os.path.basename(p), "path": p, "description": describe(p)}
                for p in patches]
        if args.json:
            print(json.dumps({"dir": args.dir, "patches": rows}, indent=2))
        else:
            if not rows:
                print(f"(no patches in {args.dir})")
            for r in rows:
                print(f"{r['name']}: {r['description']}")
        return 0

    apply = args.command == "apply"
    results = [run_patch(p, apply) for p in patches]
    worst = 0
    restart_needed = False
    for r in results:
        contribution, tag = _STATES[r["state"]]
        worst = max(worst, contribution)
        if r["state"] == "applied":
            restart_needed = True
        if not args.json:
            print(f"[{tag:>18}] {r['name']}: {r['detail']}")

    if args.json:
        print(json.dumps({
            "dir": args.dir,
            "mode": args.command,
            "results": results,
            "restart_needed": restart_needed,
            "exit_code": worst if results else 0,
        }, indent=2))
    else:
        if not results:
            print(f"(no patches in {args.dir})")
        elif restart_needed:
            print("NOTE: one or more patches were just applied; restart the "
                  "patched service to load them.")
    # apply mode maps rollback (3) above drift (2); status mode maps drift (2)
    # above missing (1). worst already reflects that via _STATES.
    return worst if results else 0


if __name__ == "__main__":
    sys.exit(main())
