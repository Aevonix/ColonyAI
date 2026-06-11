#!/usr/bin/env python3
"""Aeva activity monitor — external observer (NO Hermes code changes).

Tails Hermes' unified activity log (~/.hermes/logs/agent.log), reconstructs
each agent turn per session, and mirrors activity to the WhatsApp home channel:

  - INTERACTIVE turns (incl. the owner's own DMs) stream LIVE, per tool:
    a header when work starts, one line per tool as it completes (friendly
    labels), then a footer when the turn ends.
  - AUTONOMOUS (cron) turns post a single deferred summary, and are SUPPRESSED
    entirely when the job returns [SILENT] (routine "nothing to do" cycles) so
    the home channel isn't spammed every cycle.
  - A fuller, unthrottled line-by-line stream is always appended to
    ~/.hermes/logs/aeva-activity.log (the "deep view").

Sends run on a background thread fed by a queue, so log-tailing never blocks on
WhatsApp. Read-only mirror: it only reacts to log lines, never touches Hermes.
Runs as a launchd KeepAlive service. Handles log rotation/truncation.
"""
import json
import os
import re
import subprocess
import threading
import time
import queue as _queue
from collections import OrderedDict, deque

HOME = os.path.expanduser("~")
LOG = os.path.join(HOME, ".hermes/logs/agent.log")
ACTIVITY = os.path.join(HOME, ".hermes/logs/aeva-activity.log")
ENV = os.path.join(HOME, ".hermes/.env")
JOBS = os.path.join(HOME, ".hermes/cron/jobs.json")
HERMES = os.path.join(HOME, ".hermes/hermes-agent/venv/bin/hermes")

MIN_SEND_GAP = 1.0     # seconds between WhatsApp sends (the sender thread paces ticks)


# --- home channel target ---
def _home_channel():
    try:
        for ln in open(ENV):
            if ln.startswith("WHATSAPP_HOME_CHANNEL="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""

HOME_CH = _home_channel()

# The home channel is the full activity view: EVERY interactive turn — including
# the owner's own DMs — is mirrored here, tool by tool. The owner's *direct chat*
# stays clean because tool-progress is off there in config; this mirror is where
# that activity is meant to be visible instead.

# --- friendly labels for tools (icon + short verb) ---
TOOL_LABELS = {
    "terminal": "⚡ shell",
    "process": "⚙️ process",
    "execute_code": "\U0001f9ea run code",
    "read_file": "\U0001f4d6 read",
    "write_file": "✏️ write",
    "patch": "✏️ edit",
    "search_files": "\U0001f50e search",
    "session_search": "\U0001f50e sessions",
    "memory": "\U0001f9e0 memory",
    "colony_search_memory": "\U0001f9e0 memory",
    "colony_get_facts": "\U0001f9e0 facts",
    "send_message": "\U0001f4e4 message",
    "skill_view": "\U0001f4da skill",
    "skill_manage": "\U0001f4da skill",
    "skills_list": "\U0001f4da skills",
    "todo": "\U0001f4dd todo",
    "clarify": "❓ clarify",
}

def label(tool):
    return TOOL_LABELS.get(tool, "\U0001f527 " + tool)


# --- cron job id -> friendly name ---
def _cron_names():
    out = {}
    try:
        j = json.load(open(JOBS))
        items = j if isinstance(j, list) else j.get("jobs", [])
        for x in items:
            out[x.get("id", "")] = x.get("name", "cron job")
    except Exception:
        pass
    return out

CRON_NAMES = _cron_names()

# --- log line patterns ---
RE_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,\d]*\s+\w+\s+(?:\[([^\]]+)\]\s+)?(.*)$")
RE_INBOUND = re.compile(r"inbound message: platform=(\S+) user=(\S+) chat=(\S+) msg='(.*)'")
RE_TURN_START = re.compile(r"conversation turn: session=(\S+?)[, ]")
RE_TOOL = re.compile(r"agent\.tool_executor: tool (\S+) completed \(([\d.]+)s, (\d+) chars\)")
RE_TURN_END = re.compile(r"Turn ended: reason=(\S+?)[\( ]")
RE_RESP = re.compile(r"response ready: platform=(\S+) chat=(\S+) time=([\d.]+)s api_calls=(\d+) response=(\d+)")
RE_SILENT = re.compile(r"Job '([^']+)': agent returned \[SILENT\]")
RE_MEMORY = re.compile(r"Colony memory provider initialized \(session=(\S+?)\)")

# --- per-session turn state ---
turns = OrderedDict()          # session -> dict(start_ts, who, stream, text, tools[], memory, chat, _header)
pending_inbound = None         # (platform, user, chat, text) buffered before turn start
pending_cron = {}              # cron session -> (finalize_ts, summary); deferred so SILENT can suppress

# --- outbound: queue + background sender so parsing never blocks on WhatsApp ---
_send_q = _queue.Queue()


def append_local(line):
    try:
        with open(ACTIVITY, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _do_send(text):
    try:
        r = subprocess.run([HERMES, "send", "-t", f"whatsapp:{HOME_CH}", text],
                           capture_output=True, timeout=20,
                           env={**os.environ, "PATH": os.path.dirname(HERMES) + ":" + os.environ.get("PATH", "")})
        return r.returncode == 0
    except Exception:
        return False


def _sender_loop():
    last = 0.0
    while True:
        try:
            text = _send_q.get()
            gap = MIN_SEND_GAP - (time.time() - last)
            if gap > 0:
                time.sleep(gap)
            ok = _do_send(text)
            append_local(f"{time.strftime('%H:%M:%S')} >>> HOME {'sent' if ok else 'FAIL'}: {text.splitlines()[0][:80]}")
            last = time.time()
        except Exception:
            time.sleep(0.5)


def send_home(text):
    if not HOME_CH:
        return
    _send_q.put(text)


def tool_summary(tools):
    if not tools:
        return ""
    counts = OrderedDict()
    for t in tools:
        counts[t] = counts.get(t, 0) + 1
    parts = [f"{label(n)} ×{k}" if k > 1 else label(n) for n, k in counts.items()]
    return ", ".join(parts)


def stream_label(session):
    if session.startswith("cron_"):
        parts = session.split("_")
        jid = parts[1] if len(parts) > 1 else ""
        return CRON_NAMES.get(jid, "Autonomous job"), "cron"
    if session.startswith("subagent"):
        return "Subagent", "subagent"
    return None, "interactive"


def _who(t):
    return t.get("who") or t.get("stream_name") or "session"


# --- live emit helpers (interactive / subagent turns) ---
def emit_header(t):
    if t.get("_header"):
        return
    t["_header"] = True
    who = _who(t)
    if t["kind"] == "subagent":
        send_home(f"\U0001f9e9 {who} working…")
        return
    txt = (t.get("text") or "").replace("\n", " ").strip()
    head = f"\U0001f4ac {who} → Aeva"
    if txt:
        head += f"\n  \"{txt[:140]}\""
    send_home(head)


# --- tool-intent enrichment: a friendly "what is it doing" per tool call,
# written by the colony plugin's pre_tool_call hook to ~/.hermes/.tool_activity.jsonl ---
ACTIVITY_FILE = os.path.expanduser("~/.hermes/.tool_activity.jsonl")
tool_intents = {}      # (session, tool) -> deque[str] pending summaries (FIFO)
try:
    _intent_pos = [os.path.getsize(ACTIVITY_FILE)]   # only read intents from now on
except OSError:
    _intent_pos = [0]

def _drain_intents():
    try:
        size = os.path.getsize(ACTIVITY_FILE)
    except OSError:
        return
    if size < _intent_pos[0]:
        _intent_pos[0] = 0          # file rotated/truncated
    try:
        with open(ACTIVITY_FILE) as f:
            f.seek(_intent_pos[0])
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                key = (r.get("session", ""), r.get("tool", ""))
                tool_intents.setdefault(key, deque(maxlen=16)).append(r.get("summary", ""))
            _intent_pos[0] = f.tell()
    except OSError:
        pass


def emit_tool(t, tool, secs, chars, session=""):
    parts = [label(tool)]
    dq = tool_intents.get((session, tool))
    if dq:
        summ = dq.popleft()
        if summ:
            parts.append(summ)
    if secs and float(secs) > 0:
        parts.append(f"{secs}s")
    if chars and int(chars) > 0:
        parts.append(f"{chars} chars")
    send_home(f"↳ {_who(t)} · " + " · ".join(parts))


def emit_footer(t, reason, resp_chars):
    who = _who(t)
    if resp_chars:
        send_home(f"✅ {who} · replied ({resp_chars} chars)")
    elif reason == "text_response":
        send_home(f"✅ {who} · replied")
    else:
        send_home(f"✅ {who} · done ({reason})")


def finalize(session, reason=None, resp_chars=None, resp_secs=None, silent=False):
    t = turns.pop(session, None)
    if t is None:
        return
    kind = t["kind"]

    # local deep line (always)
    ts = t.get("start_ts", "")
    who = _who(t)
    txt = (t.get("text") or "").replace("\n", " ")[:200]
    tl = ", ".join(t["tools"]) or "-"
    res = "[SILENT]" if silent else (f"replied {resp_chars} chars" if resp_chars else f"ended ({reason})")
    append_local(f"{ts} [{who}] in='{txt}' tools=[{tl}] -> {res}")

    # AUTONOMOUS cron: single deferred summary; suppressed if the job went silent.
    if kind == "cron":
        if silent:
            return
        name = t["stream_name"]
        body = "\n".join(x for x in [tool_summary(t["tools"]), "✅ acted"] if x)
        pending_cron[session] = (time.time(), f"\U0001f916 {name}" + ("\n" + body if body else ""))
        return

    # INTERACTIVE / SUBAGENT: tools already streamed live. Skip contentless
    # gateway-resume turns (empty inbound + no work). Otherwise emit a footer
    # (and a header first, for pure-text turns where no tool ever fired).
    if not txt and not t["tools"]:
        return
    emit_header(t)
    emit_footer(t, reason, resp_chars)


def handle(ts, session, body):
    global pending_inbound
    _drain_intents()

    m = RE_INBOUND.search(body)
    if m:
        platform, user, chat, text = m.groups()
        pending_inbound = (platform, user, chat, text)
        append_local(f"{ts} [{user}] INBOUND: '{text[:200]}'")
        return

    m = RE_TURN_START.search(body)
    if m:
        sess = m.group(1)
        name, kind = stream_label(sess)
        rec = {"start_ts": ts, "tools": [], "memory": False, "kind": kind,
               "stream_name": name, "who": name, "text": "", "chat": None, "_header": False}
        if kind == "interactive" and pending_inbound:
            _, user, chat, text = pending_inbound
            rec["who"], rec["text"], rec["chat"] = user, text, chat
            pending_inbound = None
        turns[sess] = rec
        return

    if session:
        m = RE_TOOL.search(body)
        if m and session in turns:
            rec = turns[session]
            tool, secs, chars = m.group(1), m.group(2), m.group(3)
            rec["tools"].append(tool)
            append_local(f"{ts} [{_who(rec)}] tool: {tool} ({secs}s, {chars} chars)")
            if rec["kind"] in ("interactive", "subagent"):
                emit_header(rec)            # lazy: first tool opens the turn
                emit_tool(rec, tool, secs, chars, session)
            return
        if RE_TURN_END.search(body) and session in turns:
            reason = RE_TURN_END.search(body).group(1)
            finalize(session, reason=reason)
            return

    m = RE_RESP.search(body)
    if m:
        platform, chat, secs, calls, chars = m.groups()
        append_local(f"{ts} [{chat}] RESPONSE: {chars} chars in {secs}s ({calls} api calls)")
        return

    m = RE_SILENT.search(body)
    if m:
        jid = m.group(1)
        for sess in list(turns):
            if sess.startswith(f"cron_{jid}_"):
                finalize(sess, silent=True)
        for sess in [s for s in pending_cron if s.startswith(f"cron_{jid}_")]:
            pending_cron.pop(sess, None)   # suppress deferred summary: it stayed silent
        append_local(f"{ts} [{CRON_NAMES.get(jid, jid)}] -> [SILENT]")
        return

    m = RE_MEMORY.search(body)
    if m and m.group(1) in turns:
        turns[m.group(1)]["memory"] = True


def _flush_pending_cron():
    now = time.time()
    for sess in [s for s, (ts0, _) in pending_cron.items() if now - ts0 >= 8.0]:
        _, msg = pending_cron.pop(sess)
        send_home(msg)


def follow():
    append_local(f"--- aeva-activity-monitor started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    f = None
    inode = None
    pos = 0
    while True:
        try:
            st = os.stat(LOG)
            if f is None or st.st_ino != inode or st.st_size < pos:
                if f:
                    f.close()
                f = open(LOG, "r", errors="ignore")
                f.seek(0, os.SEEK_END)
                inode = st.st_ino
                pos = f.tell()
            line = f.readline()
            if not line:
                _flush_pending_cron()
                time.sleep(0.4)
                pos = f.tell()
                continue
            pos = f.tell()
            mm = RE_LINE.match(line.rstrip("\n"))
            if not mm:
                continue
            ts, session, rest = mm.group(1), mm.group(2), mm.group(3)
            handle(ts, session, rest)
        except FileNotFoundError:
            time.sleep(2)
        except Exception:
            time.sleep(1)


if __name__ == "__main__":
    threading.Thread(target=_sender_loop, daemon=True).start()
    follow()
