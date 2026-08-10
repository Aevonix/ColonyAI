# P4 controlled-learning migration and rollback

This slice makes the Selfhood Benchmark the canonical evidence surface and
`ExperimentEngine` the sole writer of `AdaptiveParamStore`. It does not touch
the host voice core, Hermes, or any live service.

## Safety contract

- `COLONY_COGNITION_P4_MODE=off|shadow|live` defaults to `off`.
- `off` preserves the prior weekly-rollup experiment evaluator for rollback.
- `shadow` records proposals, assignments, exposures, outcomes, causal status,
  and decisions but never changes an adaptive parameter.
- `live` requires either an exact reversible range in
  `COLONY_EXPERIMENT_PREGRANTS_JSON` or the existing immutable-digest bounded
  owner-approval authority.
- Only receipt-linked outcomes associated with an immutable exposure may judge
  a controlled experiment. General benchmark traffic cannot be joined later.
- A configured count of receipt-linked owner-negative reactions aborts and
  reverts immediately.
- `supported` requires the control and variant sample floors, total sample
  floor, minimum effect, and configured power gate. Missing evidence never
  receives a neutral or flattering score.
- Cohort experiments do not promote their result into a global parameter.
  Their completed evidence is a promotion proposal; a separate bounded live
  experiment is required to change the global value.
- Global experiments have no control cohort, remain causally `observed`, and
  are reverted rather than advertised as causal improvement at the deadline.

## Additive data changes

`colony-benchmark.db` gains:

- immutable `benchmark_metric_definitions` rows;
- sample principal, definition version, source/receipt/exposure references,
  evidence status, and stable sample id columns;
- definition identity and evidence count on rollups.

`colony-experiments.db` gains:

- design, assignment, sample, power, authority, and causal-status columns;
- immutable `experiment_exposures`;
- immutable `experiment_outcomes`;
- immutable `experiment_mutations`.

`colony-comms.db` gains exact outbound, reply, reaction, and receipt reference
columns. The learning feedback store gains only a bounded window query. No
legacy table or column is removed. Old releases can ignore all new objects.

## One-writer rule

When P4 is `shadow` or `live`, `AdaptiveParamStore.set()` and `.reset()` reject
any caller that does not hold the in-process capability claimed by the one
`ExperimentEngine`. The legacy `StrategyAdjuster` now emits proposals and never
applies them. Legacy CPI is deprecated; its missing dimensions are unavailable,
not synthesized, and the API should return the canonical benchmark summary.

## Required boot wiring

The implementation intentionally does not edit `server.py`, because P3 owns
that shared integration file. Apply this shape in the canonical server writer:

```python
from colony_sidecar.initiatives.approval_authority import ApprovalAuthorityStore
from colony_sidecar.intelligence.learning.feedback_store import FeedbackStore
from colony_sidecar.self_model.experiments import (
    ExperimentEngine,
    ExperimentStore,
    experiment_pregrants_from_env,
)

_correction_store = FeedbackStore(
    db_path=str(state_dir / "colony-learning-feedback.db"))

_bench = SelfhoodBenchmark(
    BenchmarkStore(db_path=str(state_dir / "colony-benchmark.db")),
    corrections=_correction_store,
)
set_benchmark(_bench)

_experiments = ExperimentEngine(
    ExperimentStore(db_path=str(state_dir / "colony-experiments.db")),
    params=_adaptive_params,
    benchmark=_bench,
    approval_authority=ApprovalAuthorityStore(),
    pregranted_ranges=experiment_pregrants_from_env(),
)
set_experiments(_experiments)
```

After `CognitionPipeline` is constructed, wire its detector adapter to the same
engine (proposal-only; it never calls `start`):

```python
cognition_pipeline.strategy_adjuster.set_experiment_proposer(_experiments)
```

Use one `ApprovalAuthorityStore` database, not a second experiment-specific
authority database. The default store already follows `COLONY_STATE_DIR`.

Persist every accepted learning correction in `_correction_store` before (or
atomically beside) passing it to `ContinuousLearner`. The correction
`context_hash` must be the exact outbound `external_ref`; otherwise it is useful
feedback but cannot enter `responses.correction_rate`.

## Required host API wiring

P3's host-router writer should make these mechanical changes:

1. `GET /self/benchmark` returns `SelfhoodBenchmark.snapshot()` as today.
2. `POST /self/benchmark/samples` derives `sample_principal` from
   `request_authority(request).principal_id`. In P4 shadow/live it calls
   `add_evidence_sample()` and requires `definition_version`, `source_ref`, and
   (for effect claims) `receipt_ref`; it must not trust the body's `source` as a
   principal. Keep `add_sample()` only in `off` compatibility mode.
3. `POST /self/experiments` accepts `metric_version`, `assignment_mode`, sample
   floors, power/effect gates, and owner-negative limit. It derives `source`
   from the authenticated principal. Catch `ExperimentApprovalRequired` and
   return HTTP 202 with the proposal and approval request id.
4. Add `POST /self/experiments/{id}/start` for an approved proposal,
   `POST /self/experiments/{id}/exposures`,
   `POST /self/experiments/{id}/outcomes`, and
   `GET /self/experiments/{id}/evidence`. These are thin calls to `start`,
   `assign_exposure`, `record_outcome`, and `evidence`.
5. Replace `/cognition/cpi` and the CPI portion of `/cognition/cycle` with
   `legacy_cpi_payload(_benchmark)`. Remove the old response model that forces
   nonexistent memory/reasoning/social/autonomy zeros.

The authority map is already implemented:

| Route | Required scope |
|---|---|
| benchmark reads, deprecated CPI read | `cognition:benchmark-read` |
| benchmark samples/probes/compute | `cognition:benchmark-manage` |
| experiment/parameter/evidence reads | `cognition:experiment-read` |
| propose/start/expose/outcome/abort | `cognition:experiment-manage` |

Approval decisions still require the existing `approvals:decide` authority.
Experiment mutation scope does not replace approval scope.

## Evidence producers

Before live graduation, wire exact references at the existing chokepoints:

- delivery competence events: verified receipt plus `delivery_id` in evidence;
- owner reaction turns: `reply_to_ref=<delivery_id>` and explicit reaction;
- outbound owner responses: stable `external_ref` plus delivery receipt;
- corrections: `context_hash=<outbound external_ref>`;
- recall probes: viewer-authorized fact subject passed as `person_id` to graph
  recall;
- experiment consumers: request an exposure before using a selected value,
  then submit the verifier receipt against that exposure.

An ordinary inbound message, transport success without a receipt, unscoped
recall result, or caller-provided source label is not causal evidence.

## Deployment sequence

### 1. Back up and record lineage

With the service stopped or SQLite online backup semantics:

```bash
sqlite3 "$COLONY_STATE_DIR/colony-benchmark.db" ".backup '$BACKUP/colony-benchmark.db'"
sqlite3 "$COLONY_STATE_DIR/colony-experiments.db" ".backup '$BACKUP/colony-experiments.db'"
sqlite3 "$COLONY_STATE_DIR/colony-comms.db" ".backup '$BACKUP/colony-comms.db'"
sqlite3 "$COLONY_STATE_DIR/approval_authority.db" ".backup '$BACKUP/approval_authority.db'"
git rev-parse HEAD > "$BACKUP/source-revision.txt"
env | grep '^COLONY_COGNITION_P4_MODE=' > "$BACKUP/p4-environment.txt" || true
```

Keep the backup private. Do not print or copy API keys with the environment
snapshot.

### 2. Deploy dark

Set `COLONY_COGNITION_P4_MODE=off`, deploy the pinned revision, and verify that
schema migration is additive and the current API remains healthy. Do not enable
a pregrant yet.

### 3. Shadow

Set `COLONY_COGNITION_P4_MODE=shadow`. Run a synthetic cohort experiment using
a non-owner test metric, balanced control/variant exposures, and verifier
receipts. Confirm:

- adaptive parameter before and after is byte-for-byte identical;
- every outcome joins exactly one exposure;
- unrelated benchmark samples are absent from experiment evidence;
- insufficient samples end as reverted/observed or suggestive;
- supported appears only after every configured gate;
- one negative owner-reaction receipt immediately aborts;
- restart preserves the experiment, exposures, outcomes, and causal status.

### 4. Narrow live canary

Use either one owner-approved proposal or a narrow pregrant such as:

```text
COLONY_EXPERIMENT_PREGRANTS_JSON={"recall.min_relevance":[0.0,0.2]}
```

Do not grant an entire registered hard-bound range by default. Canary one
parameter and one metric. Keep all voice and Hermes paths unchanged.

## Verification

Focused tests:

```bash
cd sidecar
python -m pytest -q \
  tests/test_cognition_p4_controlled_learning.py \
  tests/test_selfhood_benchmark.py \
  tests/test_experiments.py \
  tests/test_adaptive_params.py \
  tests/test_scoped_api_authority.py \
  tests/test_bounded_approval_authority.py
```

Database inspection is read-only:

```sql
SELECT id,status,execution_mode,assignment_mode,causal_status,
       authority_mode,decision_reason
FROM experiments ORDER BY created_at DESC LIMIT 20;

SELECT experiment_id,cohort,COUNT(*)
FROM experiment_exposures GROUP BY experiment_id,cohort;

SELECT experiment_id,COUNT(*),COUNT(DISTINCT receipt_ref)
FROM experiment_outcomes GROUP BY experiment_id;
```

The last two counts must agree for one-outcome-per-exposure experiments. A
causal claim with no control cohort or reused receipt is a failed canary.

## Rollback

1. Set `COLONY_COGNITION_P4_MODE=off` to stop controlled exposure use.
2. Abort each running live experiment through `ExperimentEngine.abort()` while
   the new release is still running. It restores the baseline only if the
   parameter still equals that experiment's variant; it never overwrites a
   superseding owner change.
3. Verify adaptive parameters match the pre-canary snapshot.
4. Switch to the prior pinned source revision and prior environment.
5. Leave additive tables in place if the prior release starts normally. If a
   database-level rollback is required, stop its writer and restore all four
   databases from the same backup generation; do not mix experiment and
   approval ledgers from different instants.

Rollback does not require reverting the host voice core, Hermes, or communication
services. This slice has no dependency on their real-time paths.
