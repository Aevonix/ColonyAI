"""Colony doctor — configuration and health diagnostics (v0.19.0).

A check engine for every misconfiguration class the sidecar can have,
including the exact footguns that bit the production deployment:

- persisted LLM config ``baseUrl`` missing the ``/v1`` suffix (LiteLLM
  then 404s against vllm and the entire internal cognition stack dies
  silently with "all tiers exhausted")
- empty ``apiKey`` in that config (``OPENAI_API_KEY`` never exported,
  LiteLLM refuses every call)
- contact store running ``:memory:`` (owner record lost on restart)
- ``COLONY_OWNER_CONTACT_ID`` unset or unresolvable (relationship +
  thinking degraded, CRITICAL at autonomy loop start)
- launchd plist env changes applied with ``kickstart`` instead of
  ``bootout``/``bootstrap`` (process restarts with the stale env)

Local checks read the filesystem/environment only; server checks talk
HTTP (stdlib urllib — zero extra deps) to a running sidecar and degrade
to ``skip`` when it is down. Every check runs defensively: an exception
inside a check becomes a ``fail`` result, never a crashed run.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# Check statuses
PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

#: Providers that route through LiteLLM's openai/* path — for these the
#: persisted baseUrl becomes OPENAI_API_BASE and MUST end with /v1
#: (LiteLLM appends /chat/completions to it).
OPENAI_COMPAT_PROVIDERS = frozenset({"zai", "local", "custom", "lmstudio", "vllm", "openai"})

#: Providers that need no API key at all.
KEYLESS_PROVIDERS = frozenset({"ollama"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_HOME_CHANNEL_RE = re.compile(r"^(\w+)_HOME_CHANNEL$")

#: Names of the server-side checks, in run order — used to emit skips
#: when the sidecar is unreachable.
SERVER_CHECK_NAMES = (
    "server-health",
    "server-auth",
    "server-owner-contact",
    "server-llm-router",
    "server-embedder",
    "server-blocked-approvals",
    "server-worker-liveness",
    "server-skills-observations",
)

#: A QUEUED agent_action job older than this means no queue worker is
#: claiming — auto-approved jobs would sit QUEUED forever.
WORKER_LIVENESS_THRESHOLD_MINUTES = 15

#: How to get the queue worker scheduled (v0.20.0).
WORKER_CRON_REMEDY = (
    "install the cron: re-run 'colony init' (Step 10e installs it), or add "
    "'*/5 * * * * colony-queue-worker' to your crontab — the console script "
    "ships with the pip package (or use "
    "'python -m colony_sidecar.workers.queue_worker')"
)

#: The launchd footgun: `launchctl kickstart` restarts the process with
#: the OLD environment, so .plist env edits silently do not apply.
PLIST_ENV_REMEDY = (
    "If the sidecar runs under launchd and you changed plist env vars, re-apply them with "
    "'launchctl bootout gui/$(id -u)/ai.aevonix.colony-sidecar && "
    "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist' — "
    "'launchctl kickstart' restarts the process with the stale environment."
)


@dataclass
class CheckResult:
    """One diagnostic verdict."""

    name: str
    status: str  # "pass" | "warn" | "fail" | "skip"
    detail: str = ""
    remedy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    """Resolve the state dir WITHOUT creating it (get_state_dir mkdirs)."""
    explicit = os.environ.get("COLONY_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".colony" / "data"


def _run(name: str, fn: Callable[..., Any], *args: Any) -> List[CheckResult]:
    """Run one check defensively — an exception becomes a fail result."""
    try:
        result = fn(*args)
    except Exception as exc:  # noqa: BLE001 — the whole point
        return [CheckResult(name=name, status=FAIL,
                            detail=f"check crashed: {type(exc).__name__}: {exc}")]
    if isinstance(result, CheckResult):
        return [result]
    return list(result)


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _http_get(url: str, api_key: str = "", timeout: float = 10.0) -> Tuple[int, Any]:
    """GET a sidecar endpoint with X-API-Key auth.

    Returns ``(status_code, parsed_body)``. HTTP error statuses are
    returned, not raised; connection-level failures (server down)
    propagate as ``urllib.error.URLError``/``OSError`` for the caller's
    reachability handling.
    """
    headers = {"X-API-Key": api_key} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        return exc.code, _maybe_json(raw)


# ---------------------------------------------------------------------------
# Local checks (filesystem/env — no server needed)
# ---------------------------------------------------------------------------

def check_state_dir() -> CheckResult:
    """1. COLONY_STATE_DIR exists and is writable."""
    path = _state_dir()
    if not path.exists():
        return CheckResult(
            "state-dir", FAIL,
            detail=f"state dir {path} does not exist",
            remedy=f"mkdir -p {path} (or run 'colony init'); set COLONY_STATE_DIR if it should live elsewhere",
        )
    if not path.is_dir():
        return CheckResult(
            "state-dir", FAIL,
            detail=f"{path} exists but is not a directory",
            remedy="point COLONY_STATE_DIR at a writable directory",
        )
    if not os.access(path, os.W_OK):
        return CheckResult(
            "state-dir", FAIL,
            detail=f"state dir {path} is not writable by uid {os.getuid()}",
            remedy=f"chown/chmod {path} so the sidecar user can write to it",
        )
    return CheckResult("state-dir", PASS, detail=f"{path} exists and is writable")


def check_llm_config() -> List[CheckResult]:
    """2. Persisted LLM config: exists, baseUrl /v1 suffix, apiKey, models."""
    path = _state_dir() / ".colony-llm-config.json"
    sub_names = ("llm-config-baseurl", "llm-config-apikey", "llm-config-models")

    if not path.exists():
        results = [CheckResult(
            "llm-config", WARN,
            detail=f"{path} not found — the router falls back to default Anthropic tiers "
                   "(needs ANTHROPIC_API_KEY in the sidecar env)",
            remedy="run 'colony init' or POST /v1/host/configure from the host to persist an LLM config",
        )]
        results += [CheckResult(n, SKIP, detail="no persisted LLM config") for n in sub_names]
        return results

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("top-level JSON value is not an object")
    except (OSError, ValueError) as exc:
        results = [CheckResult(
            "llm-config", FAIL,
            detail=f"{path} is unreadable/corrupt: {exc}",
            remedy="fix or delete the file, then re-run 'colony init' / POST /v1/host/configure",
        )]
        results += [CheckResult(n, SKIP, detail="LLM config unparseable") for n in sub_names]
        return results

    provider = str(cfg.get("provider", "anthropic") or "").strip().lower()
    base_url = str(cfg.get("baseUrl", "") or "").strip()
    api_key = str(cfg.get("apiKey", "") or "")
    models = cfg.get("models") or {}

    results = [CheckResult("llm-config", PASS, detail=f"{path} parsed (provider={provider})")]

    # --- baseUrl /v1 suffix (the production vllm footgun) ---
    if provider in OPENAI_COMPAT_PROVIDERS and base_url:
        if base_url.rstrip("/").endswith("/v1"):
            results.append(CheckResult(
                "llm-config-baseurl", PASS,
                detail=f"baseUrl {base_url} carries the /v1 suffix",
            ))
        else:
            fixed = base_url.rstrip("/") + "/v1"
            results.append(CheckResult(
                "llm-config-baseurl", WARN,
                detail=f"baseUrl {base_url!r} (provider={provider}) does not end with /v1 — "
                       "LiteLLM posts to <baseUrl>/chat/completions, so vllm/OpenAI-compatible "
                       "servers return 404 on every call and the whole cognition stack dies "
                       'silently with "all tiers exhausted"',
                remedy=f'edit {path}: set "baseUrl" to "{fixed}", then restart the sidecar. '
                       + PLIST_ENV_REMEDY,
            ))
    elif provider in OPENAI_COMPAT_PROVIDERS and not base_url and provider not in ("openai", "zai"):
        results.append(CheckResult(
            "llm-config-baseurl", WARN,
            detail=f"provider={provider} but baseUrl is empty — LiteLLM will target the real "
                   "OpenAI API instead of your local server",
            remedy=f'set "baseUrl" in {path} to your server\'s OpenAI-compatible endpoint '
                   '(ending in /v1, e.g. "http://127.0.0.1:8000/v1")',
        ))
    else:
        results.append(CheckResult(
            "llm-config-baseurl", PASS,
            detail=f"provider={provider} — no /v1 suffix requirement"
                   + (f" (baseUrl={base_url})" if base_url else ""),
        ))

    # --- apiKey non-empty (the OPENAI_API_KEY footgun) ---
    if provider in KEYLESS_PROVIDERS:
        results.append(CheckResult(
            "llm-config-apikey", PASS, detail=f"provider={provider} needs no apiKey",
        ))
    elif api_key.strip():
        results.append(CheckResult("llm-config-apikey", PASS, detail="apiKey is set"))
    else:
        results.append(CheckResult(
            "llm-config-apikey", FAIL,
            detail=f"apiKey in {path} is empty — the router only exports OPENAI_API_KEY / "
                   "ANTHROPIC_API_KEY when apiKey is non-empty, so LiteLLM refuses every call",
            remedy=f'set "apiKey" in {path} (for a local vllm any non-empty placeholder works, '
                   'e.g. "local"), then restart the sidecar. Alternatively export the provider '
                   "key directly in the sidecar environment.",
        ))

    # --- models map ---
    if isinstance(models, dict) and models:
        results.append(CheckResult(
            "llm-config-models", PASS,
            detail="models: " + ", ".join(f"{k}={v}" for k, v in sorted(models.items())),
        ))
    else:
        results.append(CheckResult(
            "llm-config-models", WARN,
            detail="models map is empty — the router falls back to provider presets or "
                   "auto-discovery; for local providers the placeholder model IDs likely "
                   "do not exist on your server",
            remedy=f'add "models" to {path}, e.g. '
                   '{"small": "llama3.2", "medium": "mistral", "large": "deepseek-r1"}',
        ))
    return results


def check_contacts_db() -> CheckResult:
    """3. Contact store must be persistent and (when present) a real DB."""
    from colony_sidecar.contacts.config import ContactsConfig

    sqlite_path = ContactsConfig.from_env().sqlite_path
    if sqlite_path == ":memory:":
        return CheckResult(
            "contacts-db", FAIL,
            detail="contact store is configured as :memory: — every contact (including the "
                   "owner record the IdentityResolver depends on) is lost on restart",
            remedy="set COLONY_CONTACTS_DB to a file path, or set COLONY_STATE_DIR so the "
                   "default $COLONY_STATE_DIR/colony-contacts.db applies",
        )

    path = Path(sqlite_path).expanduser()
    if not path.parent.exists():
        return CheckResult(
            "contacts-db", FAIL,
            detail=f"parent directory {path.parent} of contacts DB does not exist",
            remedy=f"mkdir -p {path.parent} (or fix COLONY_CONTACTS_DB / COLONY_STATE_DIR)",
        )
    if not path.exists():
        return CheckResult(
            "contacts-db", PASS,
            detail=f"{path} not created yet — the sidecar creates it on first start",
        )

    # Non-trivially sized (the DB itself or its -wal sibling) OR openable.
    wal = path.with_name(path.name + "-wal")
    size = path.stat().st_size
    wal_size = wal.stat().st_size if wal.exists() else 0
    if size >= 1024 or wal_size >= 1024:
        return CheckResult(
            "contacts-db", PASS,
            detail=f"{path} present ({size} bytes, wal {wal_size} bytes)",
        )
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA schema_version").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult(
            "contacts-db", FAIL,
            detail=f"{path} exists but is not a readable SQLite database: {exc}",
            remedy=f"move the corrupt file aside (mv {path} {path}.bad) and restart the "
                   "sidecar to recreate it; re-import contacts afterwards",
        )
    return CheckResult(
        "contacts-db", PASS,
        detail=f"{path} present and openable ({size} bytes)",
    )


def check_owner_contact_id() -> CheckResult:
    """4. COLONY_OWNER_CONTACT_ID must be set for owner-aware subsystems."""
    canonical = os.environ.get("COLONY_OWNER_CONTACT_ID", "")
    legacy = os.environ.get("COLONY_HOST_CONTACT_ID", "")
    if canonical:
        return CheckResult(
            "owner-contact-id", PASS, detail=f"COLONY_OWNER_CONTACT_ID={canonical}",
        )
    if legacy:
        return CheckResult(
            "owner-contact-id", WARN,
            detail=f"owner only set via deprecated COLONY_HOST_CONTACT_ID={legacy}",
            remedy="rename the variable to COLONY_OWNER_CONTACT_ID (same value)",
        )
    return CheckResult(
        "owner-contact-id", WARN,
        detail="COLONY_OWNER_CONTACT_ID is not set — owner-exclusion filters fail closed, so "
               "relationship inference and self-directed thinking run degraded and the "
               "autonomy loop logs CRITICAL at start (no owner-directed initiatives generated)",
        remedy="set COLONY_OWNER_CONTACT_ID to the owner's contact CID (create one via "
               "POST /v1/host/contacts if needed), then restart the sidecar",
    )


def check_approval_policy() -> CheckResult:
    """5. COLONY_APPROVAL_POLICY must be strict|graduated|unset."""
    raw = os.environ.get("COLONY_APPROVAL_POLICY")
    if raw is None or not raw.strip():
        return CheckResult(
            "approval-policy", PASS, detail="COLONY_APPROVAL_POLICY unset — defaults to strict",
        )
    value = raw.strip().lower()
    if value in ("strict", "graduated"):
        return CheckResult("approval-policy", PASS, detail=f"COLONY_APPROVAL_POLICY={value}")
    return CheckResult(
        "approval-policy", FAIL,
        detail=f"COLONY_APPROVAL_POLICY={raw!r} is not a valid mode — the gate fails closed "
               "to strict, so the policy you intended is silently NOT active",
        remedy="set COLONY_APPROVAL_POLICY to 'strict' or 'graduated' (or unset it for strict)",
    )


def check_standing_approvals() -> CheckResult:
    """6. standing_approvals.json must be parseable when present."""
    from colony_sidecar.initiatives import standing_approvals

    path = _state_dir() / standing_approvals._FILENAME
    if not path.exists():
        return CheckResult(
            "standing-approvals", PASS, detail=f"{path.name} not present — no standing grants",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return CheckResult(
            "standing-approvals", FAIL,
            detail=f"{path} is corrupt ({exc}) — the gate treats it as empty (fail closed), "
                   "so every previously granted 'always allow' is silently inactive",
            remedy=f"fix or delete {path}; re-grant via POST /v1/host/queue/jobs/{{id}}/approve "
                   'with {"always": true}',
        )
    return CheckResult(
        "standing-approvals", PASS, detail=f"{len(data)} standing approval(s) parseable",
    )


def check_feature_gates() -> CheckResult:
    """7. Gate env values are true/false-ish; thinking needs the LLM router."""
    problems: List[str] = []
    notes: List[str] = []
    for var in ("COLONY_ENABLE_INTERNAL_THINKING", "COLONY_ENABLE_SKILL_SYNTHESIS"):
        raw = os.environ.get(var)
        if raw is None or not raw.strip():
            continue
        value = raw.strip().lower()
        if value == "true":
            notes.append(f"{var}=true")
        elif value == "false":
            notes.append(f"{var}=false")
        else:
            problems.append(
                f"{var}={raw!r} — only the literal 'true' enables it, so this value is "
                "silently treated as false"
            )
    thinking_on = (
        os.environ.get("COLONY_ENABLE_INTERNAL_THINKING", "false").strip().lower() == "true"
    )
    if thinking_on:
        notes.append(
            "internal thinking is enabled — it requires a working LLM router "
            "(see the llm-config and server-llm-router checks)"
        )
    if problems:
        return CheckResult(
            "feature-gates", WARN,
            detail="; ".join(problems),
            remedy="set the variable to exactly 'true' or 'false'",
        )
    return CheckResult(
        "feature-gates", PASS, detail="; ".join(notes) or "gates unset (features off)",
    )


def check_home_channel() -> CheckResult:
    """8. At least one *_HOME_CHANNEL so initiatives can be delivered."""
    found = sorted(
        key for key, value in os.environ.items()
        if _HOME_CHANNEL_RE.match(key) and value.strip()
    )
    if found:
        return CheckResult("home-channel", PASS, detail="configured: " + ", ".join(found))
    return CheckResult(
        "home-channel", WARN,
        detail="no *_HOME_CHANNEL configured — initiatives will queue but never deliver",
        remedy="set one of TELEGRAM_HOME_CHANNEL / WHATSAPP_HOME_CHANNEL / DISCORD_HOME_CHANNEL "
               "/ SLACK_HOME_CHANNEL / SIGNAL_HOME_CHANNEL to the owner's chat id",
    )


def check_hermes_skills_dir() -> CheckResult:
    """9. When skill export is on, the export base's parent must exist."""
    from colony_sidecar.skills.hermes_export import hermes_base_dir, hermes_export_enabled

    if not hermes_export_enabled():
        return CheckResult(
            "hermes-skills-dir", SKIP, detail="COLONY_EMIT_HERMES_SKILLS not enabled",
        )
    base = hermes_base_dir()
    parent = base.parent
    if parent.exists():
        return CheckResult(
            "hermes-skills-dir", PASS, detail=f"export base {base} (parent {parent} exists)",
        )
    return CheckResult(
        "hermes-skills-dir", WARN,
        detail=f"COLONY_EMIT_HERMES_SKILLS is on but {parent} does not exist — skill exports "
               "will fail",
        remedy=f"mkdir -p {parent} (or point COLONY_HERMES_SKILLS_DIR at an existing skills tree)",
    )


def run_local_checks() -> List[CheckResult]:
    results: List[CheckResult] = []
    results += _run("state-dir", check_state_dir)
    results += _run("llm-config", check_llm_config)
    results += _run("contacts-db", check_contacts_db)
    results += _run("owner-contact-id", check_owner_contact_id)
    results += _run("approval-policy", check_approval_policy)
    results += _run("standing-approvals", check_standing_approvals)
    results += _run("feature-gates", check_feature_gates)
    results += _run("home-channel", check_home_channel)
    results += _run("hermes-skills-dir", check_hermes_skills_dir)
    return results


# ---------------------------------------------------------------------------
# Server checks (HTTP against the running sidecar)
# ---------------------------------------------------------------------------

def check_server_auth(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """11. Auth round-trip: an authed endpoint must not 401."""
    status, _ = _http_get(f"{base_url}/v1/host/queue/stats", api_key, timeout)
    if status == 401:
        return CheckResult(
            "server-auth", FAIL,
            detail="API key rejected (401) by /v1/host/queue/stats",
            remedy="set COLONY_API_KEY (CLI side) to the key in the sidecar's environment "
                   "(~/.colony/.env), or pass --api-key. " + PLIST_ENV_REMEDY,
        )
    note = "" if api_key else " (no API key configured — sidecar is in dev mode)"
    return CheckResult(
        "server-auth", PASS,
        detail=f"authenticated request accepted (HTTP {status}){note}",
    )


def check_server_owner_contact(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """12. The configured owner CID must resolve end-to-end."""
    from colony_sidecar.identity.resolver import get_owner_contact_id

    owner = get_owner_contact_id()
    if not owner:
        return CheckResult(
            "server-owner-contact", SKIP,
            detail="COLONY_OWNER_CONTACT_ID not set (see owner-contact-id)",
        )
    if not owner.startswith("cid-"):
        return CheckResult(
            "server-owner-contact", SKIP,
            detail=f"owner id {owner!r} is not a cid- — name/UUID resolution happens "
                   "in-process and cannot be verified over HTTP",
        )
    status, _ = _http_get(f"{base_url}/v1/host/contacts/{owner}", api_key, timeout)
    if status == 200:
        return CheckResult("server-owner-contact", PASS, detail=f"{owner} resolves (HTTP 200)")
    if status == 401:
        return CheckResult(
            "server-owner-contact", SKIP, detail="auth failed — see server-auth",
        )
    if status == 404:
        return CheckResult(
            "server-owner-contact", FAIL,
            detail=f"COLONY_OWNER_CONTACT_ID={owner} does not resolve to any contact (404) — "
                   "owner-aware subsystems fail closed (relationship + thinking degraded, "
                   "CRITICAL at autonomy loop start)",
            remedy="create the owner contact via POST /v1/host/contacts and set "
                   "COLONY_OWNER_CONTACT_ID to the returned contact_id. " + PLIST_ENV_REMEDY,
        )
    return CheckResult(
        "server-owner-contact", FAIL,
        detail=f"unexpected HTTP {status} looking up {owner}",
    )


def check_server_llm(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """13. Live-fire the LLM router with one tiny completion."""
    status, body = _http_get(f"{base_url}/v1/host/health/llm", api_key, timeout)
    if status == 404:
        return CheckResult(
            "server-llm-router", SKIP,
            detail="/v1/host/health/llm not available (sidecar predates v0.19)",
        )
    if status != 200 or not isinstance(body, dict):
        return CheckResult(
            "server-llm-router", FAIL, detail=f"HTTP {status}: {body}",
        )
    if body.get("ok"):
        return CheckResult(
            "server-llm-router", PASS,
            detail=f"router answered (tier={body.get('tier')}, "
                   f"latency={body.get('latency_ms')}ms)",
        )
    return CheckResult(
        "server-llm-router", FAIL,
        detail=f"LLM router live-fire failed: {body.get('error')}",
        remedy="the common causes are a baseUrl missing the /v1 suffix (LiteLLM 404s against "
               "vllm) and an empty apiKey (OPENAI_API_KEY never exported) — see the "
               "llm-config-baseurl / llm-config-apikey checks, fix "
               ".colony-llm-config.json, and restart the sidecar. " + PLIST_ENV_REMEDY,
    )


def check_server_embedder(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """14. Embedder health — the same health_check the startup path runs."""
    status, body = _http_get(f"{base_url}/v1/host/embed/health", api_key, timeout)
    if status in (404, 501):
        return CheckResult(
            "server-embedder", SKIP,
            detail=f"embedder health not exposed (HTTP {status})",
        )
    if status == 200 and isinstance(body, dict):
        if body.get("status") == "ok":
            extra = []
            if body.get("dims"):
                extra.append(f"dims={body['dims']}")
            if body.get("latency_ms") is not None:
                extra.append(f"latency={body['latency_ms']}ms")
            return CheckResult(
                "server-embedder", PASS,
                detail="embedder healthy" + (f" ({', '.join(extra)})" if extra else ""),
            )
        return CheckResult(
            "server-embedder", WARN,
            detail=f"embedder degraded: status={body.get('status')} "
                   f"error={body.get('error') or 'n/a'} — memory recall falls back to "
                   "keyword search",
            remedy="check the sidecar log for EmbeddingPipeline init errors; verify "
                   "COLONY_EMBED_PROVIDER / COLONY_EMBED_MODEL and restart the sidecar",
        )
    return CheckResult("server-embedder", WARN, detail=f"unexpected HTTP {status}: {body}")


def check_server_blocked_approvals(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """15. Surface jobs stuck waiting for owner approval."""
    status, body = _http_get(f"{base_url}/v1/host/queue/jobs/blocked", api_key, timeout)
    if status == 501:
        return CheckResult(
            "server-blocked-approvals", SKIP, detail="task queue not wired (501)",
        )
    if status != 200 or not isinstance(body, list):
        return CheckResult(
            "server-blocked-approvals", FAIL, detail=f"HTTP {status}: {body}",
        )
    if not body:
        return CheckResult(
            "server-blocked-approvals", PASS, detail="no jobs blocked on owner approval",
        )
    hints = ", ".join(
        str(j.get("action_hint") or j.get("id")) for j in body[:5] if isinstance(j, dict)
    )
    return CheckResult(
        "server-blocked-approvals", WARN,
        detail=f"{len(body)} job(s) pending owner approval ({hints})",
        remedy="review them and POST /v1/host/queue/jobs/{id}/approve (or .../reject); "
               'approve with {"always": true} to grant a standing approval',
    )


def check_server_worker_liveness(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """16. A queue worker must be claiming agent_action jobs.

    Uses the existing authed ``/v1/host/queue/jobs/pending`` surface
    (QUEUED jobs come first and carry ``posted_at``) and computes the
    age client-side: any QUEUED agent_action job older than
    ``WORKER_LIVENESS_THRESHOLD_MINUTES`` means nothing is claiming —
    the cron-driven ``colony-queue-worker`` is absent or broken.
    """
    status, body = _http_get(
        f"{base_url}/v1/host/queue/jobs/pending?task_type=agent_action&limit=200",
        api_key, timeout,
    )
    if status in (404, 501, 503):
        return CheckResult(
            "server-worker-liveness", SKIP,
            detail=f"task queue not available (HTTP {status})",
        )
    if status != 200 or not isinstance(body, list):
        return CheckResult(
            "server-worker-liveness", FAIL, detail=f"HTTP {status}: {body}",
        )

    now = datetime.now(timezone.utc)
    threshold = WORKER_LIVENESS_THRESHOLD_MINUTES
    stale: List[dict] = []
    queued = 0
    for job in body:
        if not isinstance(job, dict) or job.get("status") != "queued":
            continue
        queued += 1
        raw = job.get("posted_at")
        if not raw:
            continue
        try:
            posted = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if (now - posted).total_seconds() > threshold * 60:
            stale.append(job)

    if stale:
        oldest_mins = max(
            (now - datetime.fromisoformat(str(j["posted_at"]).replace("Z", "+00:00"))
             ).total_seconds() / 60
            for j in stale
        )
        hints = ", ".join(
            str((j.get("payload") or {}).get("action_hint") or j.get("job_id"))
            for j in stale[:5]
        )
        return CheckResult(
            "server-worker-liveness", WARN,
            detail=f"{len(stale)} QUEUED agent_action job(s) older than {threshold} minutes "
                   f"(oldest {oldest_mins:.0f}m: {hints}) — queue worker appears absent, so "
                   "approved jobs are never claimed or executed",
            remedy=WORKER_CRON_REMEDY,
        )
    if queued:
        return CheckResult(
            "server-worker-liveness", PASS,
            detail=f"{queued} QUEUED agent_action job(s), all younger than "
                   f"{threshold} minutes",
        )
    return CheckResult(
        "server-worker-liveness", PASS,
        detail="no QUEUED agent_action jobs waiting on a worker",
    )


def check_server_skills_observations(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """17. The agent's skill index must be reported and reasonably fresh."""
    status, body = _http_get(f"{base_url}/v1/host/observations/skills", api_key, timeout)
    if status == 501:
        return CheckResult(
            "server-skills-observations", SKIP, detail="observation store not wired (501)",
        )
    if status != 200 or not isinstance(body, dict):
        return CheckResult(
            "server-skills-observations", FAIL, detail=f"HTTP {status}: {body}",
        )
    observations = body.get("observations") or []
    if not observations:
        return CheckResult(
            "server-skills-observations", WARN,
            detail="no skill observations recorded — Colony does not know which skills the "
                   "agent has installed",
            remedy="run the colony-skills-sync console command (installed with the pip "
                   "package; the wizard schedules it daily) or enable the plugin skills "
                   "sync so the agent reports its ~/.hermes/skills index",
        )
    newest: Optional[datetime] = None
    for obs in observations:
        raw = (obs or {}).get("observed_at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        return CheckResult(
            "server-skills-observations", WARN,
            detail=f"{len(observations)} observation(s) but none carry a parseable observed_at",
        )
    age_days = (datetime.now(timezone.utc) - newest).total_seconds() / 86400
    if age_days > 7:
        return CheckResult(
            "server-skills-observations", WARN,
            detail=f"skill observations are stale — newest is {age_days:.1f} days old",
            remedy="run the colony-skills-sync console command (or re-enable the plugin "
                   "skills sync / the wizard's daily cron entry)",
        )
    return CheckResult(
        "server-skills-observations", PASS,
        detail=f"{len(observations)} skill observation(s), newest {age_days:.1f} days old",
    )


def run_server_checks(base_url: str, api_key: str, timeout: float = 10.0) -> List[CheckResult]:
    """Run all HTTP checks, skipping the rest when the sidecar is down."""
    base_url = base_url.rstrip("/")
    results: List[CheckResult] = []

    # 10. Connectivity — everything else skips when this fails.
    try:
        status, body = _http_get(f"{base_url}/v1/host/health", api_key, timeout)
    except Exception as exc:  # noqa: BLE001 — URLError, OSError, timeouts
        results.append(CheckResult(
            "server-health", FAIL,
            detail=f"sidecar not reachable at {base_url}: {exc}",
            remedy="start it with 'colony start' (or 'colony service start'), then re-run "
                   "'colony doctor'",
        ))
        reason = f"sidecar unreachable at {base_url}"
        for name in SERVER_CHECK_NAMES[1:]:
            results.append(CheckResult(name, SKIP, detail=reason))
        return results

    if status == 200 and isinstance(body, dict):
        health_status = body.get("status", "unknown")
        if health_status == "ok":
            results.append(CheckResult(
                "server-health", PASS,
                detail=f"sidecar healthy ({len(body.get('capabilities') or [])} capabilities)",
            ))
        else:
            degraded = "; ".join(
                f"{k}: {v}" for k, v in (body.get("notes") or {}).items()
                if any(w in str(v).lower() for w in ("fail", "error", "not wired", "warning"))
            )
            results.append(CheckResult(
                "server-health", WARN,
                detail=f"sidecar reports status={health_status}"
                       + (f" — {degraded}" if degraded else ""),
                remedy="check the sidecar log; 'colony status' shows the degraded subsystems",
            ))
    else:
        results.append(CheckResult(
            "server-health", FAIL,
            detail=f"/v1/host/health returned HTTP {status}: {body}",
        ))

    results += _run("server-auth", check_server_auth, base_url, api_key, timeout)
    results += _run("server-owner-contact", check_server_owner_contact, base_url, api_key, timeout)
    results += _run("server-llm-router", check_server_llm, base_url, api_key, timeout)
    results += _run("server-embedder", check_server_embedder, base_url, api_key, timeout)
    results += _run("server-blocked-approvals", check_server_blocked_approvals,
                    base_url, api_key, timeout)
    results += _run("server-worker-liveness", check_server_worker_liveness,
                    base_url, api_key, timeout)
    results += _run("server-skills-observations", check_server_skills_observations,
                    base_url, api_key, timeout)
    return results


# ---------------------------------------------------------------------------
# Engine entry point + reporting
# ---------------------------------------------------------------------------

def run_doctor(
    colony_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 10.0,
) -> List[CheckResult]:
    """Run every local and server check; never raises."""
    url = colony_url or default_colony_url()
    key = api_key if api_key is not None else os.environ.get("COLONY_API_KEY", "")
    results = run_local_checks()
    results += run_server_checks(url, key, timeout=timeout)
    return results


def default_colony_url() -> str:
    """Resolve the sidecar URL the same way the other CLI commands do."""
    explicit = os.environ.get("COLONY_URL") or os.environ.get("COLONY_SIDECAR_URL")
    if explicit:
        return explicit
    host = os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("COLONY_SIDECAR_PORT", "7777")
    return f"http://{host}:{port}"


def summarize(results: List[CheckResult]) -> dict:
    counts = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def exit_code(results: List[CheckResult]) -> int:
    """0 when nothing failed (warns are OK), 1 otherwise."""
    return 1 if any(r.status == FAIL for r in results) else 0


def results_to_json(results: List[CheckResult]) -> dict:
    counts = summarize(results)
    return {
        "results": [r.to_dict() for r in results],
        "summary": counts,
        "ok": exit_code(results) == 0,
    }


_ICONS = {PASS: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "⚪"}
_COLORS = {PASS: "\033[92m", WARN: "\033[93m", FAIL: "\033[91m", SKIP: "\033[90m"}
_RESET = "\033[0m"


def format_report(results: List[CheckResult], colony_url: str = "", color: bool = True) -> str:
    """Human-readable report: aligned status lines, remedies indented."""
    lines: List[str] = []
    header = "🩺 Colony Doctor"
    if colony_url:
        header += f" — {colony_url}"
    lines.append(header)
    lines.append("")

    width = max((len(r.name) for r in results), default=0)
    for r in results:
        label = r.status.upper().ljust(4)
        if color:
            label = f"{_COLORS.get(r.status, '')}{label}{_RESET}"
        line = f"  {_ICONS.get(r.status, ' ')} {label} {r.name.ljust(width)}"
        if r.detail:
            line += f"  {r.detail}"
        lines.append(line)
        if r.remedy and r.status in (WARN, FAIL):
            lines.append(f"       ↳ {r.remedy}")

    counts = summarize(results)
    lines.append("")
    lines.append(
        f"  {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skip"
    )
    if counts[FAIL]:
        verdict = f"  🔴 {counts[FAIL]} check(s) failing — fix the remedies above"
    elif counts[WARN]:
        verdict = f"  🟡 healthy with {counts[WARN]} warning(s)"
    else:
        verdict = "  🟢 all checks healthy"
    lines.append(verdict)
    return "\n".join(lines)
