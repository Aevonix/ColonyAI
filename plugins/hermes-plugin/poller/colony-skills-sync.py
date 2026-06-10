#!/usr/bin/env python3
"""Back-compat wrapper (v0.20.0) — logic lives in the installed package.

The skills-sync logic moved to ``colony_sidecar.workers.skills_sync``
so pip installs ship it as the ``colony-skills-sync`` console script
(see sidecar/pyproject.toml [project.scripts]). This file remains so
existing cron / hermes-cron entries that invoke it by path keep working.

Resolution order:
  1. import colony_sidecar (installed in this interpreter's environment)
  2. sys.path fallback to the repo-relative ``sidecar/`` tree, for the
     common case where this script still runs from a ColonyAI checkout
     without the package installed

If both fail (e.g. this file was copied standalone to ~/.hermes/scripts/
on a machine without the package), install it: ``pip install colonyai``
— the worker module is stdlib-only, so no heavy deps are pulled at run
time. The pre-v0.20 standalone logic is preserved in git history
(tag/branch v0.19) if you truly need a single-file copy.

Same env vars and behavior as before: COLONY_URL, COLONY_API_KEY,
HERMES_SKILLS_DIR.
"""

import sys
from pathlib import Path


def _resolve_main():
    try:
        from colony_sidecar.workers.skills_sync import main
        return main
    except ImportError:
        # Repo-relative fallback: this file lives at
        # <repo>/plugins/hermes-plugin/poller/, the package at <repo>/sidecar/.
        sidecar = Path(__file__).resolve().parents[3] / "sidecar"
        if sidecar.is_dir():
            sys.path.insert(0, str(sidecar))
            try:
                from colony_sidecar.workers.skills_sync import main
                return main
            except ImportError:
                pass
        sys.stderr.write(
            "colony-skills-sync: colony_sidecar is not importable from this "
            "interpreter and no repo-relative sidecar/ tree was found.\n"
            "Install the package (pip install colonyai) and either re-run this "
            "script or switch your cron entry to the `colony-skills-sync` "
            "console command.\n"
        )
        raise


if __name__ == "__main__":
    raise SystemExit(_resolve_main()())
