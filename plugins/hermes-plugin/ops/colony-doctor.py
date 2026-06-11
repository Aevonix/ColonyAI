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
