# P8 shared integration canary and rollback

This runbook deploys the P8 shared integration without changing the host deployment, Hermes,
the custom voice system, ResponseGuard policy, or SharedFactsStore semantics.
Use one repository writer and one deployment operator.

## 1. Capture independent rollback points

Set paths explicitly and use a timestamped directory outside the source and
state trees:

```bash
export COLONY_SOURCE=/path/to/ColonyAI
export COLONY_STATE_DIR=${COLONY_STATE_DIR:-$HOME/.colony}
export BACKUP=$HOME/colony-rollbacks/p8-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP"
git -C "$COLONY_SOURCE" rev-parse HEAD > "$BACKUP/source-revision.txt"
git -C "$COLONY_SOURCE" status --short > "$BACKUP/source-status.txt"
python -m pip freeze > "$BACKUP/python-packages.txt"
printf '%s\n' "${COLONY_RECIPIENT_SIMULATOR_MODE:-off}" > "$BACKUP/p8-mode.txt"
```

Preserve the exact previous wheel or immutable release directory used by the
service. A Git revision does not restore a separately installed local package,
and a package version does not restore a dirty source checkout. Do not print or
copy credentials into the evidence bundle.

If P8 files already exist, stop the sole sidecar writer or use SQLite's online
backup operation for all three in the same backup generation:

```bash
for db in colony-p8-visibility.db colony-p8-arcs.db colony-p8-recipient-audit.db; do
  if test -f "$COLONY_STATE_DIR/$db"; then
    sqlite3 "$COLONY_STATE_DIR/$db" ".backup '$BACKUP/$db'"
  fi
done
```

Do not copy a live SQLite file with plain `cp`. Do not edit or delete ledger
rows, and do not mix databases restored from different generations.

## 2. Verify dark behavior from the candidate

From `sidecar/`:

```bash
env -u COLONY_RECIPIENT_SIMULATOR_MODE python -m pytest -q \
  tests/test_tom_p8_server_integration.py \
  tests/test_tom_p8_outbound_integration.py
```

Then run the complete suite with the mode unset. The acceptance bar is no P8
database creation on default/off/live/unknown settings and no existing context
or delivery regression.

## 3. Install dark

Build/install from the pinned candidate commit or switch an immutable release
symlink. Start with:

```text
COLONY_RECIPIENT_SIMULATOR_MODE=off
```

Verify the sidecar health, normal context assembly, normal text delivery, and
reported installed revision. No voice restart or voice probe is required by
this change because no voice component is touched.

## 4. Enable the shadow canary

Set only:

```text
COLONY_RECIPIENT_SIMULATOR_MODE=shadow
```

Restart the Colony sidecar, then check:

1. health reports `tom_p8_shadow` / the P8 shadow note;
2. all three P8 databases exist and pass `PRAGMA integrity_check`;
3. a scoped `tom:read` principal can read its own status/Deck projection;
4. a legacy bearer, unscoped principal, and cross-person selector receive 403;
5. a newly written authorized fact appears only for its exact recipient;
6. a pre-existing unscoped fact and a below-floor fact remain absent from
   context, relationship, Deck, Tom2, and simulation projections;
7. current-source and legacy-marker graph mirrors are absent from semantic
   recall, direct memory read, multimodal memory search, model tools, internal
   synthesis, and research artifacts;
8. relationship caches/autonomy profiles contain no P8 topic content, and only
   a request-sealed exact viewer can render current rapport topics;
9. Tom2 context/report paths omit every unresolved legacy ref and denied
   topology, and the report refuses non-owner/legacy authority;
10. a sampled non-real-time outbound has a sample followed by evaluation and
   `effective_action=observe_only`;
11. FaceTime and every phone/intercom/Meet/voice alias produces no sample; and
12. the existing delivery content, target, and outcome are identical whether
   the observer succeeds, recommends hold, raises, or mutates its detached
   snapshot; an oversize draft records its exact digest and incomplete
   coverage rather than a successful truncated evaluation.

Keep the canary synthetic or owner-reviewed. P8 is not a send gate and its
would-action is not permission. Do not set `live`; this integration treats it
as off.

## 5. Immediate functional rollback

Set:

```text
COLONY_RECIPIENT_SIMULATOR_MODE=off
```

Restart only the Colony sidecar. This detaches P8 and leaves additive ledgers
unused. Confirm health no longer advertises `tom_p8_shadow` and ordinary
context/delivery still work.

If code rollback is also required, stop the sidecar and reinstall/switch to the
preserved prior immutable Colony package. Restore the source checkout
separately using the recorded revision/worktree; never use `git reset --hard`
against a dirty checkout. Normally keep the additive P8 databases for
forensics. If data rollback is required, preserve the current generation first
and restore all related ledgers from one stopped-writer backup generation.

Rollback acceptance:

- installed Colony and source each match their independently recorded point;
- P8 is absent from health and no new P8 rows are written;
- canonical SharedFacts and existing delivery behavior remain available; and
- The host's custom phone/intercom/voice behavior required no Hermes or voice-code
  rollback.
