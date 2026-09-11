"""Report the agent's skill index to Colony.

Installed as the ``colony-skills-sync`` console script (v0.20.0); the
logic moved here unchanged from
``plugins/hermes-plugin/poller/colony-skills-sync.py`` (v0.18.0),
which remains as a thin back-compat wrapper.

Scans the Hermes skills directory for SKILL.md files, parses the
frontmatter (name / description / tags), and POSTs the index to
Colony's push-only "skills" observation domain. Colony's self-directed
thinking and capability-gap machinery read from there, so Colony
proposes work the agent can actually do.

Run from cron daily (the wizard installs ``0 9 * * *``) or after
installing new skills. The TypeScript OpenClaw plugin ships the same
sync built-in (src/hermes-skills.ts); this worker is for deployments
using the Python Hermes plugin.

Environment (unchanged from the v0.18 script):
  COLONY_URL          sidecar URL  (default http://127.0.0.1:7777)
  COLONY_API_KEY      API key      (default dev-mode-no-key)
  HERMES_SKILLS_DIR   skills tree  (default ~/.hermes/skills)

Stdlib-only on purpose — must run from cron without the sidecar deps.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import urllib.request
from typing import Optional

MAX_DEPTH = 4


def skills_dir() -> pathlib.Path:
    """Resolve the skills directory from the environment at call time."""
    return pathlib.Path(
        os.environ.get("HERMES_SKILLS_DIR", "~/.hermes/skills")
    ).expanduser()


def _parse_frontmatter(text: str) -> dict:
    """Minimal SKILL.md frontmatter parse — name/description scalars and
    tags (inline ``[a, b]`` or block list), no YAML dependency."""
    match = re.match(r"\A---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    fm = match.group(1)
    out: dict = {}
    name = re.search(r"^name:\s*[\"']?([^\"'\n]+)", fm, re.MULTILINE)
    desc = re.search(r"^description:\s*[\"']?([^\"\n]+?)[\"']?\s*$",
                     fm, re.MULTILINE)
    if name:
        out["name"] = name.group(1).strip()
    if desc:
        out["description"] = desc.group(1).strip()
    inline = re.search(r"^\s*tags:\s*\[([^\]]*)\]", fm, re.MULTILINE)
    if inline:
        out["tags"] = [t.strip().strip("\"'")
                       for t in inline.group(1).split(",") if t.strip()]
    else:
        block = re.search(r"^\s*tags:\s*\n((?:\s+-\s+.*\n?)+)", fm,
                          re.MULTILINE)
        if block:
            out["tags"] = [ln.split("-", 1)[1].strip().strip("\"'")
                           for ln in block.group(1).splitlines()
                           if "-" in ln]
    return out


def scan(base: Optional[pathlib.Path] = None) -> list:
    """Scan the skills tree, returning observation dicts for each SKILL.md."""
    base = base if base is not None else skills_dir()
    observations = []
    if not base.is_dir():
        return observations
    for skill_md in sorted(base.rglob("SKILL.md")):
        rel = skill_md.relative_to(base)
        if len(rel.parts) > MAX_DEPTH:
            continue
        try:
            meta = _parse_frontmatter(
                skill_md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = meta.get("name") or skill_md.parent.name
        observations.append({
            "entity_id": name,
            "payload": {
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "path": str(skill_md),
                "source": "hermes",
            },
        })
    return observations


def report(observations: list) -> int:
    """POST the skill index to Colony's skills observation domain.

    Returns the HTTP status code.
    """
    colony_url = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
    api_key = os.environ.get("COLONY_API_KEY", "dev-mode-no-key")
    body = {"domain": "skills", "reported_by": "hermes-skills-sync",
            "observations": observations}
    req = urllib.request.Request(
        f"{colony_url}/v1/host/observations",
        data=json.dumps(body).encode("utf-8"),
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="colony-skills-sync",
        description="Report the agent's installed skill index to Colony.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and print what would be reported without POSTing to Colony",
    )
    args = parser.parse_args(argv)

    base = skills_dir()
    observations = scan(base)
    if not observations:
        print(f"No skills found under {base}")
        return 0
    if args.dry_run:
        print("colony-skills-sync (dry run — nothing reported):")
        for obs in observations:
            print(f"  - {obs['entity_id']}: {obs['payload'].get('description', '')}")
        print(f"Would report {len(observations)} skills from {base}")
        return 0

    status = report(observations)
    print(f"Reported {len(observations)} skills (HTTP {status}) from {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
