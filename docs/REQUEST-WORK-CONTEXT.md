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
inventory or proof of an external effect.

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
