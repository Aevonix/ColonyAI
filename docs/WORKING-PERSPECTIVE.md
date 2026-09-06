# A first inspectable working perspective

This increment gives the agent source-backed communication and initiative
preferences, with inspectable attention. Explicit owner corrections can change
optional research ordering. Runtime outcomes do not establish cognitive ability,
general opinions, emotions, consciousness or a worldview.
Identity prose and deployment-specific values remain private configuration.

## Owner corrections

The existing `PreferenceLearner` projects corrections from canonical, attributed
ordinary owner turns into the existing turn ledger. It keeps the original source
turn and message hash, correction time, and supersession reference. Checkpoints,
other contacts, assistant prose and quoted instructions cannot make these owner
corrections. Replaying a source does not add another correction.

The first parser deliberately accepts a limited direct style vocabulary, such as
`Be concise.`, `Actually be detailed and thorough.`, `Use bullet points please.`,
and `Don't use emoji.` Ambiguous negations, comparisons, questions, reported
speech and task-specific requests stay raw source evidence. This is not a general
natural-language preference extractor. A longer request can still influence the
current conversation without becoming a standing preference.

The same source path supports three explicit operational corrections:

```
Prefer research initiatives.
Deprioritize research initiatives.
Use normal priority for research initiatives.
```

`knowledge acquisition` can replace `research`. These set a priority preference,
not permission to execute, send, spend or change production. These explicit owner
corrections are the only weights this perspective applies, until a later correction
or erasure. Implicit observations cannot overwrite them. A late-delivered older correction
does not displace a newer one. Source occurrence time is used when available;
otherwise the source's observed ingestion time supplies the ordering fallback.

Erasure deletes the linked derived correction in the source transaction. Reads
also verify current source and message membership. A value-free head marker
prevents erasure or a late replay from reactivating an older preference/cache
value. Earlier non-erased corrections remain historical, not active. Original
legacy cache values are retained for keys that have never entered the source
path; their earlier missing provenance is not invented or retroactively repaired.

`GET /v1/host/preferences` returns current preferences, the source-backed state
and up to 100 recent surviving corrections. The source-backed deployment's
`POST /v1/host/preferences/learn` accepts `source_id` and reprojects that retained
owner source. It no longer turns arbitrary endpoint text into a new correction.
Ordinary owner messages remain the main capture path.

## Runtime history and attention

The existing competence store retains outcome records and their available
provenance, including the distinction between recorded and corrected evidence.
The initiative executor
records a stable work/attempt reference and the last available model response
metadata. Unavailable model IDs, roles and weights revisions are `unknown`.
Runtime completion is not verified semantic task success.

Automatic outcome-derived research weights are retired. Completing a report with
the wrong count cannot earn a quality claim or a priority increase. A model
endpoint timeout cannot become a judgment that the agent is bad at research.
The self brief reports historical outcome labels, explicitly limiting claims
about output quality and current-model ability. A timeout alone does not establish
output quality or its cause. Old or unverified records are not verified task results.

Existing automatic opinion revisions remain unchanged in the ledger. Inspection
returns their original basis, model provenance and reducer version, marked
`legacy_non_governing`, plus up to 100 recent revisions in `opinion_history`.
They are neither injected as current opinions nor used for ranking. Existing
competence inspection and append-only evidence reconciliation remain available;
invalidating an outcome retains its recorded value and correction provenance.

`AutonomyLoop._phase_initiative` uses explicit owner weights to order optional
research and knowledge-acquisition candidates. It copies candidates, so recurring ticks do
not compound their priority. Urgent candidates and other initiative types keep
their original priority; an optional candidate cannot acquire urgent priority
through a preference. Existing consent and execution gates still apply.

One persisted attention snapshot records the last ranking, original and effective
priorities, applicable correction IDs, and the existing load probe. It
is explicitly a dated decision snapshot, not a complete live process inventory.
Missing load sources are not proven idle. This supplements the separate shared
execution view rather than replacing it. A snapshot from the retired automatic
weighting version remains inspectable as `historical_only` and is omitted from
the current prompt brief until a new ranking replaces it.

Owner context uses this same state and includes source and revision references.
`GET /v1/host/self` exposes the inspectable perspective. Both inspection routes
use `context:read` and require owner person authority; preference reprojection
requires `memory:write` and owner person authority. Affect and relationship scores
never grant access to these routes or change action authority.

### Activating a selected continuous cycle

An existing installation can run attention and durable proposals without waking
its unrelated legacy maintenance phases:

```dotenv
COLONY_AUTONOMY_MODE=proactive
COLONY_AUTONOMY_PHASES=initiative,execute,telemetry
COLONY_AUTONOMY_PROPOSALS_ONLY=true
```

The same sidecar timer generates candidates, applies the stored perspective,
and persists deduplicated pending initiatives. The `execute` phase in this mode
does not invoke legacy self-maintenance skills, enqueue work, broadcast or
deliver proposals. The dated attention snapshot includes descriptions; stored
initiative context retains `candidate_id` for matching that ranking. Restarting
the sidecar preserves both state and pending records. Autonomy status reports
the selected phases and proposal mode. Unset phase selection keeps the existing
phase set; empty or unknown names are rejected.

This selection governs this loop only. Keep independent executors, workers,
delivery and other background services disabled unless separately intended.
Already-admitted delivery reconciliation keeps its existing behavior. Review
and explicitly supersede stale generated proposals before enabling an executor;
do not treat a historical pending record as a fresh instruction or erase the
original tasks and commitments. Selected skill evaluation uses the native
[measured update path](HERMES-ADAPTER.md#measured-skill-updates), independently of
this proposal cycle.

Observation-backed volatile proposals use the existing freshness limits, including
five minutes for system health. Reading an old stored snapshot does not renew its
time or turn it into a current failure. Unknown, missing and restart-result status
values do not mean unhealthy; system investigation requires a recognized failure
status or a valid elevated error rate. Invalid observation timestamps remain
unknown. A stale refresh returns unavailable context rather than certifying an
old healthy or failed snapshot as current. The stored observations are retained.

The legacy `.bak` scan describes only the newest file in its checked directory
and explicitly leaves recovery coverage unverified. It does not determine the
last complete backup, inspect other archive locations or update backup markers.

Owner preferences, source history and attention persist independently of the
interaction model. Historical runtime records retain their available model
provenance; changing processors does not transfer a claim of competence. There
is no automatic quality grader or outcome-derived strategy update in this loop.

## Qualification and remaining acceptance

Neutral tests drive actual source ingestion, SQLite stores, the initiative
executor's outcome recording, the autonomy phase, scoped HTTP middleware and
context assembly. Three completed-but-wrong count results or three runtime
timeouts leave optional research at 0.80. An owner correction can move it to
0.64, behind a 0.77 follow-up, while an urgent 0.95 commitment remains first.
A fresh store instance and another
session produce the same stored preference and ranking. Tests also cover replay,
late delivery, source erasure, guest denial, preserved correction precedence and
uncertain wording retained as evidence. Legacy opinion revisions cannot govern
ranking, and evidence invalidation preserves both original records and the
correction history.

No real model output is needed to calculate this deterministic ranking. Model
responses in the outcome test are controlled fixtures, not claims of model
accuracy. Production acceptance still requires an actual owner correction
through the deployed native adapter, a later session observing the correction,
and an autonomy tick whose recorded before/after ordering matches what runs.
Do not call this a complete self or general opinion system before that behavior
is observed, and do not expand it into a personality simulator first. For R1,
this is inspectable owner-directed state and its dated decision trace. It does
not close S2: task-quality regression detection and verified improvement require
their own measured task result, rather than credit for a completed execution.
