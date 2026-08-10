# Competence evidence reconciliation

Colony treats competence as earned authority, so an unverified completion may
not be repaired by editing a counter. `CompetenceStore` keeps the original
event and aggregate intact and applies an append-only reconciliation ledger at
read time.

There are two safe outcomes:

- If an external artifact proves the exact event, invalidate it or replace its
  outcome using its row id and immutable fingerprint.
- If the old data cannot be correlated one-to-one, declare an exact domain and
  half-open time window unavailable. Colony excludes that window from trust
  evidence and publishes affected benchmark metrics with `value: null` and an
  evidence-gap reason. It never estimates which rows were probably wrong.

The worker governor now records its job id, versioned outcome contract,
outcome-classification reason, and audit verdict with every non-neutral event.
This provides exact provenance for future reconciliation. Older events are
labelled `legacy_unattributed` / `legacy.unversioned` unless their call site
already supplied provenance.

## Rollback-first workflow

Do not run this against live state until code containing this schema is the
pinned release. The tool itself never starts, stops, or reloads a service.

First inspect one exact domain/window. Inspection opens SQLite read-only and
does not migrate the database:

```bash
PYTHONPATH=sidecar python3 scripts/reconcile_competence.py inspect \
  --db /path/to/colony-self-model.db \
  --domain worker:agent_action \
  --since 2026-07-06T00:00:00Z \
  --until 2026-07-13T00:00:00Z
```

Correlate the returned event ids, fingerprints, timestamps, and source refs
against durable job/action receipts. Do not infer a match from ordering alone.

An exact-event manifest looks like this:

```json
{
  "schema": "colony.competence-reconciliation/v1",
  "created_by": "owner-or-named-operator",
  "reason": "policy-skipped callbacks were recorded as completed work",
  "provenance": {
    "source": "signed-task-ledger-export",
    "artifact_sha256": "<sha256>",
    "review_id": "<immutable-review-id>"
  },
  "event_corrections": [
    {
      "event_id": 1234,
      "target_fingerprint": "<fingerprint-from-inspect>",
      "disposition": "invalidate"
    }
  ]
}
```

Use `disposition: replace` plus `replacement_outcome: failure|success|timeout`
only when the external receipt proves the replacement. A later correction
must name the current ledger id in `supersedes`; conflicting rewrites fail.

When correlation is insufficient, use an evidence gap instead:

```json
{
  "schema": "colony.competence-reconciliation/v1",
  "created_by": "owner-or-named-operator",
  "reason": "legacy callbacks have no stable job-to-event linkage",
  "provenance": {
    "source": "reconciliation-review",
    "artifact_sha256": "<sha256>"
  },
  "evidence_gaps": [
    {
      "domain": "worker:agent_action",
      "since_ts": 1783296000.0,
      "until_ts": 1783900800.0
    }
  ]
}
```

Dry-run is the default. It validates on a disposable SQLite backup, including
legacy schema migration, and leaves the source database untouched:

```bash
PYTHONPATH=sidecar python3 scripts/reconcile_competence.py apply \
  --db /path/to/colony-self-model.db \
  --manifest /path/to/reconciliation.json
```

Applying requires a new, non-existing backup path. The consistent SQLite
backup is completed before the append transaction begins; the result includes
its SHA-256 digest:

```bash
PYTHONPATH=sidecar python3 scripts/reconcile_competence.py apply \
  --db /path/to/colony-self-model.db \
  --manifest /path/to/reconciliation.json \
  --commit \
  --backup /path/to/backups/colony-self-model.before-reconciliation.db
```

Restore that backup to roll back both the additive schema and ledger. Never
delete ledger rows manually.

## Benchmark and trust behavior

- Event corrections are applied before calibration, trust, delivery, and
  `actions.success` calculations.
- An unresolved gap returns confidence `0.0` for that domain and prevents
  auto-graduation. This is insufficient evidence, not recorded failure.
- Existing weekly rollups whose evidence revision changed are returned as
  unavailable (`stale_after_competence_reconciliation`) until the affected ISO
  week is recomputed.
- Recomputing a week overwrites its derived rollup with the current correction
  revision. A gap remains explicitly unavailable.
- Resolve a gap only with a new append-only `resolve_gaps` entry naming its
  ledger id and the new reason/provenance. Reopening uncertainty is a new gap.

The raw event log is no longer automatically pruned. Databases migrated from
older releases may already have missing pre-retention events; snapshots expose
`event_history_complete: false` when the aggregate proves that condition.

After the reconciled database is active, recompute each affected week through
the authenticated host endpoint:

```text
POST /v1/host/self/benchmark/compute?week=2026-W28
```

Confirm the returned `actions.success.detail.metric_definition` is
`colony.actions-success/v2` and its reconciliation revision matches the
effective ledger. Do not copy a number from the old rollup into the new one.
