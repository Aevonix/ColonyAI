# P7 Drive Governance Canary and Rollback

## Boundaries

Shadow changes only Colony's optional priority observer. Live additionally
requires the active owner-ratified charter to narrow new P3 goal admission;
it still does not authorize action execution or change voice, Hermes, channel
transports, or live service topology. Keep one writer responsible for the P7
state/flags during each phase.

## Preconditions

1. Deploy a pinned, clean Colony revision containing P7.
2. Confirm P3 policy decisions and project provenance are healthy.
3. Confirm the existing directive/global-pause store is readable.
4. Confirm the scoped owner approval principal has `approvals:decide`, the
   `owner` audience, and (after API integration) `charter:ratify`.
5. Run the focused suite:

   ```bash
   cd sidecar
   pytest -q tests/test_cognition_p7_drive_governance.py
   pytest -q tests/test_cognition_phase_b_runtime.py \
     tests/test_cognition_p6_p7_server_wiring.py
   ```

6. Back up the P7 database with SQLite's backup API or `.backup` while the
   writer is quiesced. Record the pinned commit, file hash, `PRAGMA
   user_version`, and row counts. The shared approval database must be backed
   up by its normal whole-system procedure; do not restore it independently
   during a P7-only rollback.

## Phase A: off

Leave `COLONY_DRIVE_GOVERNANCE_MODE` unset or set it to `off`.

`bootstrap` is a narrow initial-authority lane for an otherwise unchartered
deployment. It may register inert drive definitions, propose a root charter,
and apply that root charter only through the normal typed owner approval
route. It cannot activate a child revision or replace/revoke an existing
charter, record drive signals, or apply ranking. Project/effect/outbound modes
must remain separately held while bootstrap is selected. After the initial
charter is ratified under any charter key, bootstrap rejects every further
activation request or application across all keys; restart in `live` for later
revisions through the normal typed transition flow. Bootstrap does not impose a
one-drive template: deployment controllers may choose a narrower initial
charter, while the generic engine only keeps registered definitions inert.

Verify:

- imports and existing P3 processing remain unchanged;
- the P7 database is not created by lazy attachment; and
- no new approval requests use the `charter-transition:` job prefix.

Rollback: none; off is the baseline.

## Phase B: shadow

Set:

```text
COLONY_DRIVE_GOVERNANCE_MODE=shadow
```

Register one owner-private candidate drive with a low contribution budget,
record evidence-referenced signals for two non-sensitive test goals, and
propose one 24-hour charter revision. Do not create an approval decision in
this phase.

Verify:

- the revision remains `proposed`;
- requesting activation returns `shadow_transition_candidate` and creates no
  ApprovalRequest;
- `suggested_order` is deterministic across identical probes;
- `effective_order` remains the input order;
- each result says `authorization_effect: none`;
- private projections are absent for a shared/non-owner viewer; and
- missing/expired/disabled signals appear as explicit zero-contribution states.

Rollback: set mode to `off`. Retain the additive shadow ledger for inspection.

## Phase C: owner-ratified live canary

Use a disposable owner-private charter with:

- a one-hour revision lifetime;
- one drive;
- weight at or below `0.10`;
- maximum absolute contribution at or below `0.10`;
- at most two goals and one signal per drive; and
- an activation request TTL of 10 minutes.

Set mode to `live`, create the exact activation request, inspect its digest in
the existing approval surface, approve it with the scoped owner principal, and
ratify using the same server-derived authority.

Verify:

1. The lifecycle is `proposed -> active` and rows are append-only.
2. Exact ratification retry is idempotent; a changed operation/request fails.
3. Every selected goal resolves all five persisted P3 policy decisions.
4. Deny each P3 stage in a disposable fixture; no score produces an effective
   goal for any denied stage.
5. Issue the existing global autonomy pause. `effective_order` becomes empty,
   including when the active charter is temporarily unavailable.
6. Change a boundary after P3 admission. The fresh directive check holds the
   goal as `boundary_recheck_denied`.
7. Let one signal expire; its contribution becomes zero.
8. Revoke the canary charter through a second exact owner approval. The active
   revision becomes empty, ranking returns to passthrough/no-effect state, and
   live P3 reports an explicit charter hold for new cognition.

Do not expand budgets or lifetime until all checks pass.

## Production promotion

Promote only after a shadow observation window shows stable ordering and no
scope leaks. Propose a new child revision rather than editing the canary.
Ratify it through a fresh exact approval. Keep expiry at 90 days initially;
the supported maximum is 366 days.

Exit criteria:

- no missing/conflicting P3 decision resolutions;
- no unauthorized observer rows;
- no operation or authority replay conflicts in normal retries;
- global pause probe always empties effective order;
- ranking stays within configured signal/evidence/goal budgets; and
- existing P3/Project/WorkOrder tests remain green.

## Rollback

1. Set P3 to `shadow` (or `off`) before setting
   `COLONY_DRIVE_GOVERNANCE_MODE=off`; restart only the Colony process that
   owns the optional attachment. This avoids unintentionally widening live P3
   from the ratified charter back to the base deployment validator.
2. Verify no new P3 Project is created and all normal execution gates remain
   enabled. Restore P3 `live` only as an explicit operator decision after the
   base charter behavior is re-verified.
3. Keep the P7 SQLite ledger in place for audit. It has no effect in off mode.
4. If a code rollback is required, deploy the prior pinned Colony revision and
   retain/rename the P7 database. Do not downgrade or delete it in place.
5. Do not restore the shared approval database from a P7-only backup: it may
   contain unrelated newer approvals. P7's one-use transition records are
   local and harmless while off.

If corruption is suspected, copy the P7 database, run `PRAGMA quick_check`,
compare immutable payload digests, and start a fresh P7 database only after
the copied evidence is preserved. Re-propose and re-ratify; never synthesize an
`active` row.

## Build verification record (2026-07-12)

The contract test was run before implementation and failed at collection with
the expected missing `colony_sidecar.cognition.drive_governance` module. After
implementation:

- P7 focused contract: `30 passed`;
- P7 + P3 + directives + bounded approvals + P4 + Toolsmith matrix:
  `133 passed`; and
- complete Colony sidecar suite: `2664 passed, 118 skipped, 21 warnings`.

The 21 full-suite warnings are pre-existing async/deprecation warnings outside
the P7 files; P7 adds no warning.
