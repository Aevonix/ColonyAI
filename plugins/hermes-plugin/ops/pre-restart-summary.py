#!/usr/bin/env python3
"""Pre-restart activity summary (generic — any Hermes+Colony agent).

Run by the gateway restart runner BEFORE the gateway is stopped, while the log
and Colony are still current. Captures what the agent was just doing and writes
a concise summary to ~/.hermes/.post_restart_resume, which the restart runner
folds into the wake message (and the worker reads to resume) so the agent knows,
on wake, what it was doing right before the restart.

No LLM call — pulls from the unified agent.log + Colony's /v1/host/timeline.
"""
import json
import os
import re
import urllib.request

HOME = os.path.expanduser("~")
LOG = os.path.join(HOME, ".hermes/logs/agent.log")
MARK = os.path.join(HOME, ".hermes/.post_restart_resume")
COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")

RE_INBOUND = re.compile(r"inbound message: platform=(\S+) user=(\S+) chat=(\S+) msg='(.*)'")
RE_RESP = re.compile(r"response ready: platform=\S+ chat=\S+ time=[\d.]+s api_calls=\d+ response=(\d+)")
RE_TOOL = re.compile(r"agent\.tool_executor: tool (\S+) completed")
RE_TURN = re.compile(r"conversation turn: session=(\S+?)[, ]")


def _colony_key():
    for path in (os.path.join(HOME, ".colony/.env"), os.path.join(HOME, ".hermes/.env")):
        try:
            for ln in open(path):
                if ln.startswith("COLONY_API_KEY="):
                    return ln.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    return os.environ.get("COLONY_API_KEY", "")


def _last_turn():
    last_in = None
    last_resp = None
    tools = []
    try:
        lines = open(LOG, errors="ignore").read().splitlines()[-500:]
    except OSError:
        lines = []
    for ln in lines:
        m = RE_INBOUND.search(ln)
        if m:
            last_in = (m.group(2), m.group(4))
            tools = []
            last_resp = None
        m = RE_TOOL.search(ln)
        if m:
            tools.append(m.group(1))
        m = RE_RESP.search(ln)
        if m:
            last_resp = m.group(1)
    return last_in, tools[-8:], last_resp


def _timeline_digest():
    try:
        req = urllib.request.Request(
            f"{COLONY_URL}/v1/host/timeline?since=1h&limit=8",
            headers={"Authorization": "Bearer " + _colony_key()})
        d = json.load(urllib.request.urlopen(req, timeout=8))
        return d.get("digest", "")
    except Exception:
        return ""


def build():
    last_in, tools, last_resp = _last_turn()
    parts = []
    if last_in:
        who, msg = last_in
        tail = (f"you replied (~{last_resp} chars)" if last_resp
                else "you had NOT replied yet — they may still be waiting")
        parts.append(f'Last exchange: {who} said "{msg[:160]}" — {tail}.')
    if tools:
        from collections import Counter
        counts = Counter(tools)
        pretty = ", ".join((f"{t} x{n}" if n > 1 else t) for t, n in counts.items())
        parts.append("Recent tools you ran: " + pretty + ".")
    dg = _timeline_digest()
    if dg:
        parts.append("Recent activity (Colony timeline):\n"
                     + "\n".join("  " + l for l in dg.splitlines()[:6]))
    return "\n".join(parts) if parts else "No recent activity was captured before the restart."


if __name__ == "__main__":
    summary = build()
    try:
        with open(MARK, "w") as f:
            f.write(summary)
        print(f"pre-restart summary written ({len(summary)} chars) -> {MARK}")
    except OSError as e:
        print("pre-restart summary write failed:", e)
