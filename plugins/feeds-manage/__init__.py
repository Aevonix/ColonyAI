"""feeds-manage — agent tools over the Colony feeds framework.

Gives the agent real tools to create/list/manage spec-driven intelligence
feeds conversationally.  "Keep me informed about X" -> the agent authors a
feed spec (YAML) and calls feed_create; when the spec names no destination,
the feed is wired to deliver into the conversation the request came from.

The plugin shells out to the feeds CLI so the framework can live in any
python install.  Config (~/.colony-feeds.json):
    {
      "python":      "/path/to/python3",          # with PyYAML available
      "pythonpath":  "/path/to/ColonyAI/sidecar", # where colony_sidecar lives
      "specs_dir":   "~/.hermes/data/feeds/_specs"
    }
"""

import json
import logging
import os
import subprocess

logger = logging.getLogger("hermes.plugin.feeds_manage")

_CFG_PATH = os.path.expanduser("~/.colony-feeds.json")
_DEFAULTS = {
    "python": "python3",
    "pythonpath": "",
    "specs_dir": "~/.hermes/data/feeds/_specs",
}


def _cfg():
    cfg = dict(_DEFAULTS)
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def _cli(*args, timeout=180):
    cfg = _cfg()
    env = dict(os.environ)
    if cfg["pythonpath"]:
        env["PYTHONPATH"] = os.path.expanduser(cfg["pythonpath"])
    proc = subprocess.run(
        [os.path.expanduser(cfg["python"]), "-m", "colony_sidecar.feeds.cli", *args],
        capture_output=True, text=True, timeout=timeout, env=env)
    out = (proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")).strip()
    return proc.returncode, out


def _session_deliver_target(context):
    """Best-effort delivery target for 'the conversation this request came from'."""
    try:
        from gateway.session_context import get_session_env  # type: ignore
        env = get_session_env() or {}
        platform = env.get("HERMES_SESSION_PLATFORM") or env.get("platform")
        chat_id = env.get("HERMES_SESSION_CHAT_ID") or env.get("chat_id")
        if platform and chat_id:
            return f"{platform}:{chat_id}"
    except Exception:
        pass
    return "origin"


def _tool_feed_create(args, **context):
    spec_yaml = (args or {}).get("spec_yaml", "")
    if not spec_yaml.strip():
        return "feed_create needs spec_yaml (the full feed spec as YAML text)"
    cfg = _cfg()
    specs_dir = os.path.expanduser(cfg["specs_dir"])
    os.makedirs(specs_dir, exist_ok=True)

    # Default destination = the conversation the request came from.
    try:
        import yaml  # type: ignore
        raw = yaml.safe_load(spec_yaml)
        if isinstance(raw, dict) and not raw.get("destination"):
            raw["destination"] = {"kind": "deliver",
                                  "deliver": _session_deliver_target(context)}
            spec_yaml = yaml.safe_dump(raw, sort_keys=False)
        name = raw.get("name", "unnamed") if isinstance(raw, dict) else "unnamed"
    except Exception as e:
        return f"spec_yaml is not valid YAML: {e}"

    spec_path = os.path.join(specs_dir, f"{name}.yaml")
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(spec_yaml)

    rc, out = _cli("validate", spec_path)
    if rc != 0:
        return f"spec INVALID (fix and retry):\n{out}"
    rc, out = _cli("create", spec_path)
    if rc != 0:
        return f"create failed:\n{out}"
    if (args or {}).get("run_now"):
        _cli("run", name, "collect", timeout=600)
        _cli("run", name, "distill")
        out += "\n(collect ran; distill triggered in background)"
    return f"feed '{name}' created.\n{out}"


def _tool_feed_list(args, **context):
    rc, out = _cli("list")
    return out or "(no feeds)"


def _tool_feed_status(args, **context):
    rc, out = _cli("status", (args or {}).get("name", ""))
    return out


def _tool_feed_pause(args, **context):
    rc, out = _cli("pause", (args or {}).get("name", ""))
    return out


def _tool_feed_resume(args, **context):
    rc, out = _cli("resume", (args or {}).get("name", ""))
    return out


def _tool_feed_run(args, **context):
    a = args or {}
    rc, out = _cli("run", a.get("name", ""), a.get("stage", "collect"), timeout=600)
    return out


def _tool_feed_delete(args, **context):
    a = args or {}
    extra = ["--purge"] if a.get("purge") else []
    rc, out = _cli("delete", a.get("name", ""), *extra)
    return out


_NAME_PARAM = {"type": "object",
               "properties": {"name": {"type": "string", "description": "Feed instance name (slug)"}},
               "required": ["name"]}

_TOOL_SCHEMAS = [
    {"name": "feed_create", "handler": _tool_feed_create,
     "description": ("Create a new intelligence feed from a YAML spec. Author the spec from the "
                     "user's request: name (kebab-case slug), title, topic (1 paragraph charter), "
                     "cadence {collect, distill, optional digest/alerts/discovery}, sources "
                     "(x_searches / github_search+github_keywords / arxiv {categories, keywords} / "
                     "rss / forum_urls), optional registry of tiered sources, optional scoring "
                     "keyword_categories, optional llm {provider, model} pin. Omit destination to "
                     "deliver briefs to THIS conversation; or set destination explicitly "
                     "(kind=deliver with a platform:chat_id target, kind=command with a "
                     "send_command, or kind=file for archive-only)."),
     "parameters": {"type": "object",
                    "properties": {
                        "spec_yaml": {"type": "string", "description": "Full feed spec as YAML text"},
                        "run_now": {"type": "boolean",
                                    "description": "Also run collect+distill immediately (default false)"}},
                    "required": ["spec_yaml"]}},
    {"name": "feed_list", "handler": _tool_feed_list,
     "description": "List all feed instances and their state.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "feed_status", "handler": _tool_feed_status,
     "description": "Show one feed's queue freshness, latest brief, and job health.",
     "parameters": _NAME_PARAM},
    {"name": "feed_pause", "handler": _tool_feed_pause,
     "description": "Pause all scheduled jobs of a feed.", "parameters": _NAME_PARAM},
    {"name": "feed_resume", "handler": _tool_feed_resume,
     "description": "Resume a paused feed.", "parameters": _NAME_PARAM},
    {"name": "feed_run", "handler": _tool_feed_run,
     "description": "Run one stage of a feed now (collect runs inline; distill/digest/discovery "
                    "are triggered in the background).",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string"},
                                   "stage": {"type": "string",
                                             "enum": ["collect", "distill", "digest", "alert", "discovery"]}},
                    "required": ["name", "stage"]}},
    {"name": "feed_delete", "handler": _tool_feed_delete,
     "description": "Delete a feed's jobs and shims. Set purge=true to also delete its data.",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string"},
                                   "purge": {"type": "boolean"}},
                    "required": ["name"]}},
]


def register(ctx):
    n = 0
    for schema in _TOOL_SCHEMAS:
        handler = schema["handler"]
        try:
            ctx.register_tool(
                name=schema["name"],
                toolset="feeds_manage",
                schema={k: v for k, v in schema.items() if k != "handler"},
                handler=(lambda args=None, _h=handler, **kw: _h(args, **kw)),
            )
            n += 1
        except Exception as e:
            logger.warning("feeds-manage: failed to register %s: %s", schema["name"], e)
    logger.info("feeds-manage plugin registered (%d/%d tools)", n, len(_TOOL_SCHEMAS))
