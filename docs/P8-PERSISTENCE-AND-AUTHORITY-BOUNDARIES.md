# P8 persistence and fact-authority boundaries

Status: persistence primitives integrated into server startup, scoped context,
and ignored non-real-time delivery shadow observation. See
`docs/P8-SHARED-INTEGRATION.md`. ResponseGuard, the host deployment, Hermes, and the custom
Voice Core remain unchanged.

This slice closes three storage/boundary gaps left by the isolated P8 social
core while preserving its dark-by-default rollout:

- `tom/visibility_store.py` persists verified `FactVisibilityV1` envelopes;
- `tom/recipient_audit.py` journals sampled outbound references and later
  simulation evidence, then computes truthful high-salience coverage; and
- `tom/fact_adapters.py` separates untrusted content/confidence from fact
  identity, subject, audience, evidence, and freshness authority.

## Visibility-envelope ledger

`FactVisibilityStore.append()` accepts `FactCandidateV1`, not a bare mapping or
bare envelope. It recomputes the SHA-256 digest of the candidate's exact UTF-8
content, compares it to the visibility envelope, and stores only the envelope.
Fact content is never written to this database.

`fact_ref` is the immutable event identity. Replaying the exact envelope
returns the original sequence with `replayed=true`; reusing the fact reference
with changed content digest, scope, subject, shareability, freshness,
confidence, source, or evidence raises `VisibilityEnvelopeConflictError`.
SQLite triggers reject updates and deletes.

The schema includes these query indexes:

| Index | Leading fields | Purpose |
|---|---|---|
| viewer/freshness | `viewer_scope`, `fresh_until` | exact viewer candidate selection |
| subject/freshness | `subject_person_id`, `fresh_until` | subject reconciliation/migration |
| shareability/freshness | `shareability`, `fresh_until` | policy inventory |
| freshness | `fresh_until` | expiry inventory |

`project_authorized()` requires an attested `ViewerContextV1`, applies observed
time, freshness, confidence, and exact visibility decisions, scans at most
2,048 candidates, and returns at most 64 envelopes. Non-owner SQL selection is
restricted to the exact person, granted audiences, exact conversation, and
public scope before every selected row is decoded and checked again. An
unattested viewer causes no query and receives an empty projection. Ordering
uses stable fact references rather than insertion order.

`open_visibility_envelope_store(path)` defaults disabled and creates no parent
directory or database. Integration must pass `enabled=True` explicitly.

## Recipient-simulation audit and coverage

Coverage cannot be derived honestly from completed simulation rows alone: a
missed simulation would leave no row and appear successful. The audit ledger
therefore records two immutable event kinds for one `outbound_item_ref`:

1. `sample` is appended when the outbound item is selected for evaluation.
2. `evaluation` is appended after a simulation attempt and records whether the
   result actually says `evaluated=true`.

Event IDs and idempotency keys are unique. Samples bind the exact final draft
digest and are unique per outbound item. Simulation references are globally
single-use; evaluations are also unique per `(outbound_item_ref,
simulation_ref)` so an
`evaluated=false` attempt remains immutable but a later, separately identified
retry can close the coverage gap. Exact replay is a no-op; an alias or changed
replay is a conflict. An evaluation is rejected until its sample exists. Every
attempt must bind the same recipient, exact scope revision, and salience, and
its timestamp cannot predate the sample. Update, delete, and
`INSERT OR REPLACE` overwrite paths are rejected by SQLite triggers.

The audit row contains only:

- event, outbound, simulation, and recipient references;
- recipient scope revision;
- request, result, and exact draft SHA-256 digests;
- surface, risk, action, evaluation-path, risk-code, and repair-code values;
- salience/evaluated booleans and timestamps.

It deliberately has no draft/fact text, fact references, arc references,
related-risk references, dependency error text, credentials, or host topology.
Risk and repair related references are reduced to an explicit allowlist of
stable rule codes. The adapter recomputes the canonical result audit digest and
rejects unknown surfaces, actions, paths, risks, repairs, impossible
mode/evaluation combinations, wrong risk/fail behavior, or authority/effect
claims.

`project()` is owner-wide or exact recipient plus exact current scope revision,
returns at most 256 events, and gives unattested viewers an empty result without
querying the ledger. `coverage()` counts every authorized high-salience sample
against a matching `evaluated=true` event. It reports:

- `no_samples` when there is no denominator (never “complete”);
- `incomplete` plus bounded missing outbound references when any sample lacks
  a successful evaluation; or
- `complete` only when the non-empty sampled set is fully evaluated.

Missing-reference output is bounded to 64. Coverage validates the full
authorized ledger through fixed-size fetch pages, so an append-only lifetime
cap cannot permanently strand the result as unknown. Any corrupt row changes
the status to `indeterminate`; it can never claim complete. An off/unknown
simulator mode makes
`open_recipient_simulation_audit_store()` return `None` without creating state.

## Fact-authority adapter

`FactPayloadV1.from_untrusted()` accepts exactly `content` and `confidence`.
Every additional model/body key is rejected, including `fact_ref`,
`content_digest`, source, subject/person fields, viewer scope, shareability,
freshness, evidence, audiences, conversation, revision, and attestation.

`build_fact_candidate()` separately requires a typed
`ServerFactAuthorityV1`. It derives the content digest and constructs the
existing immutable visibility/candidate records. The type is a boundary aid,
not authentication: `tom/integration.py` builds it only after host-side person
resolution and a server-sealed scoped viewer. It never populates authority
fields from model output, fact metadata, or body channel/session claims.

## Rollout and rollback

The shared integration now owns one process writer per ledger and remains off
unless `COLONY_RECIPIENT_SIMULATOR_MODE=shadow` is explicit. It appends a
sample before every attempted non-real-time evaluation, exposes projections
only through server-sealed viewers, applies a strictly positive confidence
floor (default `0.5`, never lowerable by an internal override), and ignores all
simulator recommendations. Relationship caches contain no P8-derived content;
rapport topics are projected only for a request-sealed exact viewer. The
outbound observer receives a detached, structurally bounded snapshot with the
exact untruncated draft rather than live delivery dictionaries.

The configured server graph enforces current-source and legacy-marker mirror
exclusions across semantic recall, direct reads, multimodal memory-vector
results, model/internal consumers, and the borrowed research graph path. This
is a content boundary for SharedFacts compatibility mirrors, not invented
visibility authority for unrelated graph memories.

The context routers also contain legacy sources that have no visibility
envelope. With P8 attached, goals, initiatives, global briefings, world-model
search, insights, known-contact lists, cognition, owner directives, surprises,
and global temporal heads-up data are queried only for a server-attested exact
owner viewer. A non-owner or unsealed caller cannot cause those sources to be
queried. This fail-closed owner projection preserves useful owner context while
the producers are migrated; it does not make the underlying rows P8 facts.
Turning P8 off restores the pre-existing query/render contract.

Generic Colony identity, capability/skill descriptions, product self-knowledge,
and the configured agent timezone remain intentionally shared context. They do
not select or summarize a human, owner workspace, relationship, communication,
goal, or memory record. A producer that starts carrying person-specific content
must move behind an exact-person or visibility-envelope projection rather than
being added to this generic carve-out.

### Reasoning-tool execution boundary

Filtering the tool definitions sent to an LLM is not authorization. The shared
`ToolExecutor` therefore re-checks every model-returned batch, direct host tool
call, and dynamically graduated tool immediately before its handler runs.
First-party tools have one reviewed effect class:

- `calculate` and `web_search` are public/general reads;
- memory, relationship, repository, filesystem, goal, boundary, and self-state
  reads are private reads; and
- every state-changing tool is a mutation. Unknown/dynamic tools default to
  mutation until explicitly classified.

While P8 is attached, caller capabilities are derived from middleware
authority, never `HostIdentity`, turn context, or tool arguments. An unsealed or
non-owner caller receives only public/general tools. Private reads require the
server-attested exact owner; mutations additionally require the exact
`tools:mutate` scope. An explicit tool list may narrow that set but cannot
broaden it. The executor also receives the allowed-name set, so a model call to
an unadvertised tool returns `tool_not_authorized` without invoking a handler.

On every configured server, independent of P8 mode, the same executor maps the
tool and its actual arguments to a `DirectiveGuard` action. An explicit owner
boundary refuses execution. A missing, crashing, or malformed boundary
dependency fails closed for private reads and mutations; public/general reads
remain usable so a directive-store incident does not disable basic calculation
and research. P8-off keeps the legacy HTTP identity compatibility but does not
bypass a healthy configured standing-boundary check.

The immediate functional rollback is mode `off` plus a Colony-sidecar restart.
Preserve additive databases for forensics; do not edit ledger rows or restore
one P8 database from a different backup generation. Follow
`docs/runbooks/P8-SHARED-INTEGRATION-CANARY-ROLLBACK.md` for independent source
and installed-package rollback. The integration rejects real-time surfaces and
is not inserted into the custom voice path.

Focused verification:

```bash
cd sidecar
python -m pytest -q \
  tests/test_tom_p8_visibility_store.py \
  tests/test_tom_p8_recipient_audit.py \
  tests/test_tom_p8_fact_adapters.py \
  tests/test_tom_p8_server_integration.py \
  tests/test_tom_p8_outbound_integration.py \
  tests/test_recall_person_boost.py
```

## Remaining integration gaps

- No existing graph/world-model/goal/initiative/briefing/surprise fact source
  emits stored candidates yet; untyped global context is contained to an exact
  attested owner rather than given invented authority or shown to a guest.
- Outbound items do not consistently carry the exact fact references used by
  their producer; the integration never invents missing provenance.
- Arc extraction, broader shadow social rules, and owner-reviewed false-positive
  evidence remain separate P8 work.
- The generic shared-context carve-out above needs ongoing content review, and
  legacy private tool handlers still need typed per-argument person/resource
  authority before any can be safely exposed to non-owner callers.
- P8 stays shadow-only and advisory. No host-deployment, Hermes, ResponseGuard,
  SharedFactsStore, Meet, or Voice Core semantics are changed.
