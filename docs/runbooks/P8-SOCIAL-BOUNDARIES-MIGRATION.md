# P8 social-boundaries migration and rollback

This runbook applies to the P8 core in `tom/visibility.py`, `tom/arcs.py`, and
`tom/recipient_simulator.py`. The candidate is additive, defaults off, and does
not alter the host's custom voice stack, Hermes, `SharedFactsStore`, ResponseGuard,
or any live service by itself.

The shared server integration has since landed behind a shadow-only switch.
Use `docs/runbooks/P8-SHARED-INTEGRATION-CANARY-ROLLBACK.md` for its deployment
and rollback. In that integration, `live` intentionally maps to off; the
future advisory described in section 6 below is not implemented.

## Safety invariants

- Use one writer for the P8 integration branch and one process writer for the
  arc ledger.
- Derive recipient identity and scope from authenticated server/transport
  state. Body fields, prompt text, and model output are not attestation.
- Filter facts before ranking or rendering. Never retry a scoped miss against
  global memory.
- Treat the arc ledger as append-only. Correction is a compensating event, not
  SQL update/delete.
- Give every arc link/transition event a server-derived visibility envelope
  exactly matching its open event. Never merge a differently scoped evidence
  reference and rely on projection time to hide it.
- Keep `COLONY_RECIPIENT_SIMULATOR_MODE=off` through schema creation and health
  verification, then use `shadow` before any non-real-time advisory use.
- The simulator never grants send or approval authority. Existing action and
  delivery authority remains mandatory.
- Never synchronously gate `voice`, `phone`, `intercom`, or `meet`. Those
  surfaces are asynchronous observation only.

## 1. Record source and local-install rollback points

Do this before changing the installed Colony package or service unit:

```bash
mkdir -p "$BACKUP"
git -C "$COLONY_SOURCE" rev-parse HEAD > "$BACKUP/source-revision.txt"
git -C "$COLONY_SOURCE" status --short > "$BACKUP/source-status.txt"
python -m pip freeze > "$BACKUP/python-packages.txt"
python - <<'PY' > "$BACKUP/installed-colony.txt"
from importlib.metadata import PackageNotFoundError, version
for name in ("colony-sidecar", "colony_sidecar"):
    try:
        print(name, version(name))
    except PackageNotFoundError:
        pass
PY
printf '%s\n' "${COLONY_RECIPIENT_SIMULATOR_MODE:-off}" \
  > "$BACKUP/p8-mode.txt"
```

Do not copy the whole environment or print credentials. Preserve the exact
prior wheel or immutable release directory used by the service; a version
string alone is not a rollback artifact. Deploy from a pinned commit or built
wheel, not from an untracked gateway checkout.

If `colony-arcs.db` already exists, stop its sole writer or use SQLite online
backup semantics:

```bash
sqlite3 "$COLONY_STATE_DIR/colony-arcs.db" \
  ".backup '$BACKUP/colony-arcs.db'"
```

Do not transform or overwrite `colony-shared-facts.db` during this slice.

## 2. Reproduce and verify the core

From the pinned candidate source:

```bash
cd sidecar
python -m pytest -q \
  tests/test_tom_p8_visibility.py \
  tests/test_tom_p8_arcs.py \
  tests/test_tom_p8_recipient_simulator.py \
  tests/test_tom_p8_boundary_corpus.py
```

Before deployment, also run the full Colony test suite from the same revision:

```bash
cd sidecar
env -u COLONY_RECIPIENT_SIMULATOR_MODE python -m pytest -q
```

The unset environment is intentional: the default/no-effect behavior is part
of rollback safety.

## 3. Deploy dark

Install the pinned wheel or switch the immutable release symlink with:

```text
COLONY_RECIPIENT_SIMULATOR_MODE=off
```

Do not add server, host-router, delivery, ResponseGuard, or voice wiring in the
same rollout. Import the three modules and open a disposable `ArcStore` to
verify file permissions and append-only triggers. With mode off, invoke a
simulator using dependencies that would raise if touched; it must return
`evaluated=false`, `recommended_action=no_effect`, and no side effect.

Exit criteria:

- normal context assembly and delivery behavior are unchanged;
- no simulator dependency is queried in off mode;
- no new voice-path import, await, latency, or restart is introduced;
- the deployed package reports the pinned source revision.

## 4. Integrate producers without enforcement

Use the exact seams in `docs/TOM-P8-SOCIAL-BOUNDARIES.md`. Keep each change
additive and independently reversible:

1. attested viewer construction;
2. fact visibility envelope persistence/adapters;
3. sole arc writer and idempotent producers;
4. outbound fact-reference propagation;
5. append-only simulation audit store;
6. non-real-time shadow invocation.

Open every phase by reproducing the corresponding old failure as a test. Do
not migrate legacy facts by inventing subjects, viewers, confidence, or
freshness. Mark unavailable fields unknown and exclude those facts from a
recipient projection until evidence supplies a valid envelope.

For an existing arc database, validate read-only before enabling its writer:

```sql
PRAGMA integrity_check;
SELECT COUNT(*) FROM arc_events;
SELECT arc_id,COUNT(*) FROM arc_events GROUP BY arc_id
ORDER BY COUNT(*) DESC LIMIT 20;
```

An invalid arc history is a canary failure; do not edit its rows in place.

## 5. Shadow canary

Set:

```text
COLONY_RECIPIENT_SIMULATOR_MODE=shadow
```

Start with synthetic people and messages, then owner-reviewed low-risk
non-real-time traffic. The effective action must remain `observe_only`; no
simulator output is allowed to call transport, mutate a draft, consume an
approval, or alter an arc.

Required corpus:

- Alice, Bob, and Carol each receive only their own private fact and arc;
- stale, low-confidence, conflicting, and unknown facts remain absent;
- an unattested recipient causes no fact or arc lookup;
- a cross-person draft reference produces a critical would-hold record without
  revealing the other person's content;
- closure occurs only from explicit receipt evidence;
- identical simulation input replays to the exact same audit digest;
- concurrent identical arc events append once;
- dependency failures produce the documented risk-class advisory;
- completed voice observations, including compound aliases such as
  `google_meet` and `phone_call`, say `observe_async` and are never awaited by
  a voice turn;
- text aliases (`text`, `rcs`, and non-call `whatsapp`) are not accidentally
  classified as real-time voice.

Graduation evidence must include zero disallowed content disclosures in the
person-crossing corpus and an audit record for every sampled high-salience
message. Review false positives and repair usefulness in the Operator Deck
before considering a live non-real-time advisory.

## 6. Narrow non-real-time advisory

Only after the durable audit store and outbound fact-reference propagation are
working may selected text/chat delivery callers set mode `live`. Keep the
existing delivery authority and ResponseGuard independently active. The
simulator result is one advisory input; it is neither permission to send nor a
replacement for an approval.

Do not enable live mode for the custom voice request path. The core will still
return `observe_async`, but deployment should avoid the call entirely on that
latency-sensitive path.

## Rollback

1. Set `COLONY_RECIPIENT_SIMULATOR_MODE=off`. This is the immediate functional
   rollback and must require no database change.
2. Stop the sole arc/simulation-audit writer. Confirm ordinary delivery and
   context assembly no longer import or call P8 adapters.
3. Switch the service to the preserved prior immutable release or reinstall
   the exact saved wheel, then verify its source revision. Restore the prior
   service environment—not a reconstructed approximation.
4. Leave additive P8 databases in place if the old release ignores them. If a
   data rollback is necessary, preserve the current files for forensics and
   restore all related P8 ledgers from one backup generation while writers are
   stopped. Do not mix arc and simulation receipts from different instants.
5. Run the prior release's health and focused context/delivery tests. Confirm
   the custom voice path's existing latency and behavior with its normal probe;
   P8 rollback must not require a voice or Hermes code rollback.

Never use `git reset --hard` against a dirty live checkout. Preserve local
changes, select a pinned revision through an immutable release/worktree, and
make the repository rollback independent of the installed-package rollback.
