# Current work during a native turn

Hermes requests Colony memory once at the beginning of a turn. A turn can then
make several model calls while tools run or other sessions finish tasks. The
initial work snapshot is therefore explicitly labeled as observed at turn start.

The existing `llm_request` middleware now reads a small operational projection
before each actual model call, after reconciling source erasure. It uses
`GET /v1/host/executions?projection=request` with the current resolved contact
and session, an eight-record limit and a 250 ms total transport deadline. It
does not repeat semantic memory retrieval, create a work record or add a worker.

The owner projection includes operational identifiers, state, observation age,
configured worker labels and a retained report digest when available. It omits
task questions, draft text, transcript content and local report paths. The
response is bounded to 4,000 characters and states when records or sources are
omitted or unavailable. It is a partial observation, not a complete process
inventory or proof of an external effect. Session, parent execution, worker
attempt and native task/run identifiers remain joinable; claim tokens and locks
are never added to the request projection.

The full API and request response include `work_sources`: each reader's status,
returned record counts, known totals, truncation and read timestamp when known.
Request responses also report how many rows from each source fit the excerpt.
The prompt names source coverage and takes records round-robin across readers,
so a backlog of accepted drafts cannot consume all slots before another session
or cron is considered. A row that cannot fit is omitted without preventing
shorter later rows from appearing. Read availability does not verify process
liveness. Counts can overlap because an accepted initiative and a native board
can describe the same undertaking; do not sum them into a unique task count.

| Reader | Observed boundary |
| --- | --- |
| `execution` | Registered, nonterminal native turns, including bound children; expired observations have unknown liveness |
| `native_kanban` | Explicitly selected boards or the current native board, with per-board availability |
| `local_work` | Canonical accepted local drafts and bound internal reviews, with native associations |
| `worker_work` | Claimed/running jobs in the attached canonical queue; an unattached queue is unavailable, not idle |
| `native_cron` | The explicitly bound profile's native execution ledger |
| `reported_worker` | Configured local status files, including unverified terminal reports; no configured reader is `not_observed` |

Independent ledger reads run concurrently with 200 ms read deadlines. A failed
reader becomes unavailable while other observations remain usable. These are
separate snapshots, not one atomic snapshot across databases. Cancelling the
await does not kill an underlying read-only thread. Selected readers retain
their existing bounded SQLite reads. Other profiles, unregistered processes and
independent direct-delivery receipts are outside this inventory. `complete`
remains false. Full API reads can request up to 100 records per reader; they
remain bounded and disclose omissions.

One request-only block, delimited by `colony-work-request-v1`, supersedes the
turn-start snapshot. A later call replaces that block instead of accumulating
snapshots. Chat requests use a system message; Responses requests use the
instructions field. User content, tool results and native transcript storage
are preserved. A failed or late read replaces earlier operational context with
an explicit unavailable notice; it does not imply that work stopped.

Resolved owner turns and explicitly attested local system turns receive this
view. A child inherits its parent's already-bound scope. Guests, unresolved
participants, background review and cron transport scopes do not receive this
owner projection. The server independently checks the existing owner authority;
the projection is not a new authority grant. A selected local worker running
through an explicitly attested CLI keeps that existing scope.

The native integration fixture runs one turn through three model requests and
two native file reads. A concurrent HTTP writer completes an existing neutral
initiative between requests. The next request sees its completion and report
digest; a subsequent endpoint failure produces unavailable context. Both owner
SMS resolution and attested CLI are exercised, with one memory prefetch and no
operational block in the native transcript. Provider replies and the concurrent
writer are controlled integration inputs, not evidence of general model quality.

Production acceptance should observe the same boundary with the installed
artifact and actual configuration. Broader worker coverage and preventing
every conflicting promise remain separate work. Shared context helps a model
notice changes; consequential coordination still uses the durable commitment
and task ownership contracts.
