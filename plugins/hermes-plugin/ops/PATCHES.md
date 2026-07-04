# Guarded framework patches (hermes-patch-runner)

Sometimes an integration needs behavior the host framework (Hermes) does not
yet expose through config, plugins, or hooks. Hand-editing the framework
checkout is the wrong answer: edits are silently lost on every `hermes update`,
nothing records what was changed, and a drifted edit can corrupt the install.

This directory ships a generic, repeatable alternative:

- `hermes-patch-runner.py`: a registry runner. One directory = the registry,
  one script = one patch. `status` verifies, `apply` heals, both are always
  safe to re-run.
- `colony-doctor.py` calls the runner (`apply --json`) on every doctor run, so
  a framework update that reverts the patches is healed automatically within
  one doctor cycle, and a framework change that breaks a patch's anchors is
  loudly reported instead of blind-patched.

## Layout on the host

```
~/.hermes/patches/                  <- the registry (HERMES_PATCH_DIR to override)
    <name>_patch.py                 <- one guarded patch
    <name>_patch.py.disabled        <- disabled patch (skipped)
~/.hermes/scripts/hermes-patch-runner.py
~/.hermes/scripts/colony-doctor.py
```

The patch definitions themselves are DEPLOYMENT-specific (they encode which
framework behavior a given deployment reroutes or tunes), so they do not live
in this public repo. Keep them in your private deployment repo and sync them to
`~/.hermes/patches/`. Only the runner, the doctor wiring, and this contract are
generic.

## The patch contract

Every patch script is standalone and must implement:

Invocation

- `<script>`: check-only. NEVER modifies the target.
- `<script> --apply`: apply if missing, and only if every anchor matches
  exactly.

Exit codes and first-line output

| exit | output prefix | meaning |
|------|---------------|---------|
| 0 | `OK:` | patch already present (marker found) |
| 0 | `APPLIED:` | apply mode: patch applied now; restart the service to load it |
| 1 | `MISSING:` | check mode: patch absent, anchors intact, re-appliable |
| 2 | `ERROR: FUNDAMENTAL_CHANGE` | target no longer matches the known original; the patch must be re-authored for this framework version; target NOT modified |
| 3 | `ERROR:` | apply attempted, post-apply validation failed, target ROLLED BACK |

Required internals

1. `MARKER`: a string present in the target iff the patch is applied. Checked
   first; if found, exit 0 immediately (idempotence).
2. `ANCHOR` block(s): exact copies of the original code being replaced. Each
   must match EXACTLY ONCE (`s.count(ANCHOR) == 1`) or the script reports
   FUNDAMENTAL_CHANGE and refuses to touch the target.
3. Backup before writing (`shutil.copy2(target, target + ".bak-<name>")`).
4. Post-write validation (`py_compile` for Python targets, `node --check` for
   JS, etc.). On failure: restore the backup, exit 3.

Skeleton

```python
#!/usr/bin/env python3
"""<one-line description: what behavior this patch changes and why>"""
import os, sys, shutil, py_compile

F = os.path.expanduser("~/.hermes/hermes-agent/<target file>")
MARKER = "<string present iff patched>"
ANCHOR = "<exact original block>"
PATCHED = "<replacement block containing MARKER>"

def main():
    apply = "--apply" in sys.argv
    if not os.path.exists(F):
        print("ERROR: target not found", file=sys.stderr); return 2
    s = open(F).read()
    if MARKER in s:
        print("OK: patch present"); return 0
    if s.count(ANCHOR) != 1:
        print("ERROR: FUNDAMENTAL_CHANGE, anchor matches "
              f"{s.count(ANCHOR)}x (need exactly 1); re-author for this "
              "framework version. NOT editing the target.", file=sys.stderr)
        return 2
    if not apply:
        print("MISSING: patch absent but original intact (re-appliable)"); return 1
    bak = F + ".bak-<name>"
    shutil.copy2(F, bak)
    open(F, "w").write(s.replace(ANCHOR, PATCHED, 1))
    try:
        py_compile.compile(F, doraise=True)
    except Exception as e:
        shutil.copy2(bak, F)
        print(f"ERROR: validation failed, rolled back: {e}", file=sys.stderr)
        return 3
    print("APPLIED: patch applied (restart the gateway to load it)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

## Runner usage

```bash
hermes-patch-runner.py list                 # what is registered
hermes-patch-runner.py status               # verify only, never modifies
hermes-patch-runner.py apply                # heal: re-apply whatever is missing
hermes-patch-runner.py apply --json         # machine-readable (doctor uses this)
hermes-patch-runner.py apply --only mimo    # subset by filename substring
```

Runner exit codes: `status` 0 all applied / 1 some missing / 2 drift or error;
`apply` 0 all applied / 2 drift / 3 a rollback happened. `apply` prints a
restart reminder whenever it actually applied something.

## Update-day workflow (framework update)

1. Snapshot the framework checkout (tar) before updating.
2. Update the framework (git checkout of the release tag + dependency sync).
   The update reverts all patched files to pristine upstream code.
3. `hermes-patch-runner.py apply`:
   - `APPLIED` for every patch whose anchors survived: done.
   - `FUNDAMENTAL_CHANGE` for any patch whose target code changed upstream:
     re-author THAT PATCH SCRIPT against the new source (fix the patch, never
     hand-edit the target), then re-run `apply`.
4. Restart the gateway; run `colony-doctor.py` for full verification.

Before updating, you can pre-flight against a future version without touching
anything: extract the target files at the new tag (`git show <tag>:<path>`)
into a temp tree and check each patch's ANCHOR/MARKER counts there.
