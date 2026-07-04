#!/usr/bin/env python3
"""Colony Doctor — generic health/validation for a Hermes+Colony integration.

Catches the plugin/hook regressions that silently break Colony on any agent host,
and re-validates automatically when the Hermes version changes (so a Hermes
update that shifts hook conventions is caught immediately rather than at runtime).

It runs TWO kinds of check:

  STATIC (no side effects — pure AST/source analysis of deployed Hermes plugins):
    - hook names registered must be in this Hermes build's VALID_HOOKS
    - hook callbacks must be SYNC (async hooks are invoked but never awaited ->
      their return value is silently dropped) and accept **kwargs (Hermes calls
      cb(**kwargs) with version-specific keys)
    - no direct `ctx.config` access (absent on PluginContext in some builds)
    - `register_slash_command` should be guarded / use `register_command`
    - plugin tool handlers must accept **kwargs (registry calls handler(args, **ctx))

  LIVE (HTTP only — no plugin execution):
    - sidecar reachable; contact-resolve endpoint answers
    - Colony LLM is configured with a generative provider (not an embedding model)

Exit code 0 = all PASS, 1 = at least one FAIL. Cron-friendly. Generic: it
discovers plugins and does not hardcode any particular agent.
"""
import ast
import json
import os
import sys
import urllib.request

HOME = os.path.expanduser("~")
PLUGINS_DIR = os.environ.get("COLONY_DOCTOR_PLUGINS_DIR", os.path.join(HOME, ".hermes", "plugins"))
STATE_FILE = os.path.join(HOME, ".hermes", ".colony_doctor_state.json")
SIDECAR_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")

FAILS, WARNS, OKS = [], [], []
def ok(m):   OKS.append(m);   print(f"  ✅ {m}")
def warn(m): WARNS.append(m); print(f"  ⚠️  {m}")
def fail(m): FAILS.append(m); print(f"  ❌ {m}")

# --------------------------------------------------------------------------
def hermes_version():
    try:
        import hermes_cli
        return getattr(hermes_cli, "__version__", "unknown")
    except Exception:
        return "unknown"

def valid_hooks():
    try:
        from hermes_cli.plugins import VALID_HOOKS
        return set(VALID_HOOKS)
    except Exception:
        return None  # unknown -> skip the name check rather than false-fail

def colony_key():
    try:
        for ln in open(os.path.join(HOME, ".colony", ".env")):
            if ln.startswith("COLONY_API_KEY="):
                return ln.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""

# --------------------------------------------------------------------------
# STATIC plugin analysis
# --------------------------------------------------------------------------
def iter_plugin_inits():
    """Yield (label, path) for every plugin __init__.py (one or two levels deep)."""
    if not os.path.isdir(PLUGINS_DIR):
        return
    for entry in sorted(os.listdir(PLUGINS_DIR)):
        d = os.path.join(PLUGINS_DIR, entry)
        if not os.path.isdir(d):
            continue
        init = os.path.join(d, "__init__.py")
        if os.path.isfile(init):
            yield entry, init
        for sub in sorted(os.listdir(d)):
            sinit = os.path.join(d, sub, "__init__.py")
            if os.path.isfile(sinit):
                yield f"{entry}/{sub}", sinit

import re as _re
HOOK_SHAPE = _re.compile(r"^(agent:|on_|pre_|post_|subagent_|transform_|api_)")

def func_defs(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out

def has_kwargs(args: ast.arguments) -> bool:
    return args.kwarg is not None

def _check_callback(defs, hook_name, cbname):
    cb = defs.get(cbname)
    if isinstance(cb, ast.AsyncFunctionDef):
        fail(f"hook '{hook_name}' callback {cbname}() is async — Hermes invokes it "
             f"sync; its return value is silently dropped")
    elif isinstance(cb, ast.FunctionDef) and not has_kwargs(cb.args):
        posonly = [a.arg for a in cb.args.args if a.arg != "self"]
        if posonly:
            warn(f"hook '{hook_name}' callback {cbname}({', '.join(posonly)}) lacks "
                 f"**kwargs — may crash on Hermes-passed keys")

def _check_hook_name(hook_name, vhooks):
    if vhooks is not None and hook_name not in vhooks:
        fail(f"hook '{hook_name}' is NOT a valid Hermes hook (silently dropped)")
    else:
        ok(f"hook '{hook_name}' valid")

def analyze_plugin(label, path, vhooks):
    src = open(path, encoding="utf-8").read()
    if "register_hook" not in src and "register_tool" not in src and "register_context_engine" not in src:
        return  # not a hook/tool/engine plugin — nothing to validate here
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        fail(f"[{label}] does not parse: {e}")
        return
    defs = func_defs(tree)
    print(f"\n[{label}]  ({path.replace(HOME, '~')})")

    found_any = False
    seen_hooks = set()
    uses_register_hook = "register_hook" in src
    for node in ast.walk(tree):
        # (1) direct ctx.register_hook("name", cb) — literal name + Name callback
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "register_hook" and node.args:
                found_any = True
                nn = node.args[0]
                if isinstance(nn, ast.Constant) and isinstance(nn.value, str):
                    seen_hooks.add(nn.value)
                    _check_hook_name(nn.value, vhooks)
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Name):
                        _check_callback(defs, nn.value, node.args[1].id)
            if attr == "register_slash_command":
                found_any = True
                warn("uses register_slash_command (absent on PluginContext in newer "
                     "Hermes — use register_command, guarded by getattr)")
            if attr == "register_tool":
                found_any = True
                for kw in node.keywords:
                    if kw.arg == "handler" and isinstance(kw.value, ast.Lambda):
                        if not has_kwargs(kw.value.args):
                            fail("register_tool handler lambda lacks **kwargs — Hermes "
                                 "calls handler(args, **context); errors on task_id")
                        else:
                            ok("tool handler absorbs **kwargs")

        # (2) loop/tuple registration: ("hook_name", _callback) literal pairs —
        # catches dynamic `for n,f in ((...)): ctx.register_hook(n,f)` patterns
        if (uses_register_hook and isinstance(node, ast.Tuple) and len(node.elts) == 2
                and isinstance(node.elts[0], ast.Constant)
                and isinstance(node.elts[0].value, str)
                and isinstance(node.elts[1], ast.Name)):
            hn = node.elts[0].value
            if hn in seen_hooks:
                continue
            if hn in (vhooks or set()) or HOOK_SHAPE.match(hn):
                seen_hooks.add(hn)
                found_any = True
                _check_hook_name(hn, vhooks)
                _check_callback(defs, hn, node.elts[1].id)

    # direct ctx.config access
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "config"
                and isinstance(node.value, ast.Name) and node.value.id == "ctx"):
            warn("direct `ctx.config` access — PluginContext has no .config in some "
                 "Hermes builds (guard with getattr / load config independently)")
            break

    if not found_any:
        print("  (no hook/tool/command registrations)")

# --------------------------------------------------------------------------
# LIVE checks
# --------------------------------------------------------------------------
def http_json(path, key, params=""):
    url = SIDECAR_URL + path + (("?" + params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    return json.load(urllib.request.urlopen(req, timeout=8))

def live_checks():
    print("\n[live]")
    key = colony_key()
    try:
        http_json("/v1/host/health", key)
        ok("sidecar reachable (/v1/host/health)")
    except Exception as e:
        fail(f"sidecar unreachable: {e}")
        return
    try:
        http_json("/v1/host/contacts/resolve", key, "gateway=whatsapp&address=__doctor_probe__")
        ok("contact-resolve endpoint answers")
    except urllib.error.HTTPError as e:
        (ok if e.code in (404, 200) else fail)(f"contact-resolve endpoint answers (HTTP {e.code})")
    except Exception as e:
        fail(f"contact-resolve failed: {e}")
    # Colony LLM provider sane (generative, not an embedding model)
    cfg_path = os.path.join(os.environ.get("COLONY_STATE_DIR", os.path.join(HOME, ".colony", "data")),
                            ".colony-llm-config.json")
    try:
        cfg = json.load(open(cfg_path))
        models = " ".join(str(v) for v in (cfg.get("models") or {}).values()).lower()
        prov = cfg.get("provider", "?")
        if "embed" in models:
            fail(f"Colony LLM uses an embedding model as a chat tier: {models}")
        else:
            ok(f"Colony LLM configured (provider={prov}, models ok)")
    except Exception:
        warn("no persisted Colony LLM config (.colony-llm-config.json) — using defaults")

# --------------------------------------------------------------------------
def config_checks():
    """Validate the durable, out-of-repo machine config this integration relies on
    (these survive Hermes updates but aren't in the repo, so the doctor is their
    safety net): SOUL doctrine, colony LLM, model output/context window, cron
    delivery routing, and the scheduled launchd jobs."""
    print("\n[config & durability]")
    import subprocess as _sp
    # SOUL.md colony doctrine
    try:
        s = open(os.path.join(HOME, ".hermes", "SOUL.md"), encoding="utf-8").read()
        miss = [m for m, present in (
            ("memory-as-self", ("my own mind" in s or "my own memory" in s)),
            ("authoritative-time", "Current Time" in s),
            ("outreach-governance", ("never spam" in s.lower() or "approval first" in s.lower())),
        ) if not present]
        (warn if miss else ok)(f"SOUL doctrine: {'missing ' + ', '.join(miss) if miss else 'memory-as-self + time + outreach all present'}")
    except Exception:
        warn("SOUL.md not found")
    # config.yaml: colony LLM + model output/context
    try:
        from hermes_cli.config import load_config, cfg_get
        c = load_config()
        col = cfg_get(c, "plugins", "colony", default={}) or {}
        large = str(col.get("llm_large", "")).lower()
        if col.get("llm_provider") and "embed" not in large:
            ok(f"colony LLM configured (provider={col.get('llm_provider')}, model={col.get('llm_large')})")
        else:
            warn("plugins.colony LLM not explicitly configured (relies on auto-detect)")
        mt = cfg_get(c, "model", "max_tokens", default=None)
        cl = cfg_get(c, "model", "context_length", default=None)
        if mt:
            ok(f"model output cap set ({mt}); context_length {cl}")
        else:
            warn("model.max_tokens unset — a reasoning model can burn the budget on reasoning and truncate replies")
    except Exception as e:
        warn(f"config.yaml check skipped: {e}")
    # cron: colony jobs should not deliver to the owner DM (origin)
    try:
        jobs = (json.load(open(os.path.join(HOME, ".hermes", "cron", "jobs.json"))) or {}).get("jobs", [])
        bad = [j.get("name") for j in jobs
               if "olony" in (j.get("name") or "") and j.get("deliver") == "origin"]
        (warn if bad else ok)(f"cron delivery: {'these colony jobs still hit the owner DM -> ' + str(bad) if bad else 'colony jobs route off the owner DM'}")
    except Exception:
        pass
    # launchd scheduled jobs
    for label in ("ai.aevonix.colony-doctor", "ai.hermes.activity-monitor"):
        try:
            r = _sp.run(["launchctl", "list", label], capture_output=True, text=True, timeout=5)
            (ok if r.returncode == 0 else warn)(f"launchd {label}: {'loaded' if r.returncode == 0 else 'NOT loaded'}")
        except Exception:
            pass


# --------------------------------------------------------------------------
def patch_checks():
    """Verify/heal the guarded host-side patch registry via the patch runner.

    Deployment patches to the Hermes framework source are managed exclusively
    by hermes-patch-runner.py over a registry directory (default
    ~/.hermes/patches, override HERMES_PATCH_DIR). Each patch self-verifies
    its anchors per the runner contract: the runner re-applies anything a
    framework update reverted, and reports FUNDAMENTAL_CHANGE (never
    blind-patching) when a patch must be re-authored for the installed
    version. Deployments with no patch registry skip this section entirely.
    """
    print("\n[framework patches]")
    import subprocess as _sp
    patch_dir = os.environ.get("HERMES_PATCH_DIR",
                               os.path.join(HOME, ".hermes", "patches"))
    try:
        entries = os.listdir(patch_dir)
    except Exception:
        entries = []
    has_patches = any(
        (n.endswith("_patch.py") or n.endswith("_patch")) and not n.startswith(".")
        for n in entries
    )
    if not has_patches:
        print("  (no patch registry: nothing to verify)")
        return
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "hermes-patch-runner.py")
    if not os.path.exists(runner):
        fail(f"patch registry {patch_dir} exists but hermes-patch-runner.py "
             "is missing next to the doctor (cannot verify/heal)")
        return
    try:
        r = _sp.run([sys.executable, runner, "apply", "--dir", patch_dir, "--json"],
                    capture_output=True, text=True, timeout=180)
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        fail(f"patch runner failed to execute: {e}")
        return
    for res in data.get("results", []):
        name = res.get("name", "?")
        state = res.get("state", "error")
        detail = res.get("detail", "")
        if state == "ok":
            ok(f"patch {name}: applied")
        elif state == "applied":
            warn(f"patch {name}: was missing and has been RE-APPLIED")
        elif state == "fundamental-change":
            fail(f"patch {name}: FUNDAMENTAL_CHANGE, anchors no longer match the "
                 f"installed Hermes; re-author the patch (target NOT modified). {detail}")
        elif state == "rollback":
            fail(f"patch {name}: apply failed validation and was rolled back. {detail}")
        else:
            fail(f"patch {name}: {detail}")
    if data.get("restart_needed"):
        warn("patches were re-applied this run: restart the gateway to load them "
             "(launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway)")


def main():
    print("=" * 64)
    print("Colony Doctor")
    print("=" * 64)
    hv = hermes_version()
    vhooks = valid_hooks()
    prev = {}
    try:
        prev = json.load(open(STATE_FILE))
    except Exception:
        pass
    changed = prev.get("hermes_version") and prev.get("hermes_version") != hv
    print(f"Hermes version: {hv}" + (f"  (CHANGED from {prev['hermes_version']} — re-validating)"
                                     if changed else ""))
    print(f"VALID_HOOKS known: {'yes' if vhooks is not None else 'NO (skipping name checks)'}")

    print("\n--- STATIC plugin checks ---")
    for label, path in iter_plugin_inits():
        analyze_plugin(label, path, vhooks)

    print("\n--- LIVE checks ---")
    live_checks()

    print("\n--- CONFIG & durability checks ---")
    config_checks()

    print("\n--- FRAMEWORK PATCH checks ---")
    patch_checks()

    print("\n" + "=" * 64)
    print(f"RESULT: {len(OKS)} ok, {len(WARNS)} warn, {len(FAILS)} fail"
          + ("  [HERMES VERSION CHANGED]" if changed else ""))
    print("=" * 64)
    try:
        json.dump({"hermes_version": hv, "fails": len(FAILS), "warns": len(WARNS)},
                  open(STATE_FILE, "w"))
    except Exception:
        pass
    sys.exit(1 if FAILS else 0)

if __name__ == "__main__":
    main()
