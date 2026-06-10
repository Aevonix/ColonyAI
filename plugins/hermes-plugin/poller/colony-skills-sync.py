#!/usr/bin/env python3
"""Report the Hermes skill index to Colony (v0.18.0).

Scans the Hermes skills directory for SKILL.md files, parses the
frontmatter (name / description / tags), and POSTs the index to
Colony's push-only "skills" observation domain. Colony's self-directed
thinking and capability-gap machinery read from there, so Colony
proposes work the agent can actually do.

Run from cron daily (or after installing new skills), alongside
colony-initiative-poller.py. The TypeScript OpenClaw plugin ships the
same sync built-in (src/hermes-skills.ts); this script is for
deployments using the Python Hermes plugin.
"""

import json
import os
import pathlib
import re
import urllib.request

COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "dev-mode-no-key")
SKILLS_DIR = pathlib.Path(
    os.environ.get("HERMES_SKILLS_DIR", "~/.hermes/skills")).expanduser()
MAX_DEPTH = 4

HEADERS = {"X-API-Key": COLONY_API_KEY, "Content-Type": "application/json"}


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


def scan() -> list:
    observations = []
    if not SKILLS_DIR.is_dir():
        return observations
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        rel = skill_md.relative_to(SKILLS_DIR)
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


def main() -> None:
    observations = scan()
    if not observations:
        print(f"No skills found under {SKILLS_DIR}")
        return
    body = {"domain": "skills", "reported_by": "hermes-skills-sync",
            "observations": observations}
    req = urllib.request.Request(
        f"{COLONY_URL}/v1/host/observations",
        data=json.dumps(body).encode("utf-8"),
        headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"Reported {len(observations)} skills "
              f"(HTTP {resp.status}) from {SKILLS_DIR}")


if __name__ == "__main__":
    main()
