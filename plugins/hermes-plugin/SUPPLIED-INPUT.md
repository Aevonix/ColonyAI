# Already captured host input

An authenticated host may run an existing native task inside
`apsimo_hermes.input_provenance.supplied_input(contact_id=..., session_id=...,
input_refs=[...], source_refs=[...])`. The caller must first validate the
participant, original input hashes and current inherited source revisions.
`contact_id` is a consistency check, never an authority grant. Ordinary native
participant and tool authority still apply.

`session_id` is the new native root session. Each input reference contains
`source_id` and `input_message_hash`; each source reference contains `source_id`
and `source_version`. These are exact lowercase SHA-256 hashes, not a topic or
a textual provenance claim. Native child turns inherit only a valid bound
parent scope. The context resets on exit, and copied worker contexts cannot
continue after that exit.

The host may use Hermes's `persist_user_message` to display the original human
input while the actual request contains a derived task. The adapter records
only the assistant source, with the original input dependencies and inherited
source dependencies. It does not add the task wrapper as human testimony.
From 1.0.33, this validated assistant-only output is retained even when the
native platform is excluded from automatic ordinary turn capture. The platform
filter still excludes ordinary turns without supplied input; the exception
does not grant participant or tool authority.
Inherited handles are labelled as unopened evidence and become available to
the scoped source reader only after their exact host-appended block reaches
the actual native request. Quoted marker text cannot add host handles.

Existing erasure reconciliation runs before requests, tools and the final
writer. `handoff.result` is populated only after the dependent assistant
envelope is durably queued; it is not a claim of backend delivery. It contains
the native session/turn and exact input/source dependencies. The host must
revalidate these before delayed result exposure, playback or further effects.
Canonical deletion follows recorded dependencies; arbitrary paraphrases,
native transcript files and archived copies remain outside this guarantee.

`handoff.failure` is either `None` or a typed summary containing `reason`,
`admitted` and `retryable`. A timeout or network failure during source freshness
verification reports `source_freshness_unavailable`. Only a failure before any
successful source admission has `retryable: true`. Confirmed erasure, invalid
scope, HTTP authorization failures and other unknown failures are not retryable.
Later successful verification never reopens the failed scope. The request stays
without source content or tools; transient verification is described as temporary,
not as a claim that the source was deleted.

A host may use that exact initial-failure result to start one fresh native task
under its existing deadline, after revalidating the original source envelope.
It must rebuild the agent and supplied-input scope so native prefetch and inherited
context run again. This adapter does not retry tasks, extend their budgets or
authorize replay after any successful admission or possible tool effect.

When `execution_registry_enabled` is true, a source-checked root execution also
retains these input IDs and hashes in its existing operational metadata. The
work reader opens a current, viewer-scoped excerpt of the original input, up to
240 characters, so another session can identify the request associated with
that execution. Long inputs and multiple input parents are marked partial.
This is an attributed request, not proof of task performance, a current task
plan, or a delegated child's assignment. Unbound executions keep unknown
purpose; task wrappers and process commands never supply a substitute.

The registered request adapter opts in with `input_context=true`, then includes
any displayed excerpt in the existing source-freshness check and answer
lineage. Older adapters receive operational metadata only. The registry stores
no excerpt text. Erased or inaccessible inputs disappear on the next read;
annotated inputs require the canonical source reader instead of a clipped work
label that could omit the correction. Active work and ancestry are selected
before optional excerpts within the same eight-record, 4,000-character budget.


Registered native platforms may wrap their actual message handler in
`transport_input(contact_id=..., platform=..., input_refs=..., source_refs=...)`
before Hermes allocates a session. Only the first independently authenticated
root turn on that platform can bind the scope. Wrap the execution handler,
including startup resume, rather than the earlier admission callback. Persist
`result` before the wrapper exits because native delivery runs afterwards.
Detached children cannot retain a scope after its owning handler closes.

An automatic resume can set the optional bounded `recall_query` to its already
validated original input. The memory provider uses it only after normal
participant checks, and only for a bound root session. Child queries remain
independent. This preserves Hermes' empty-event automatic continuation semantics
while retrieving task-relevant current evidence. Exact native plain-user-tail
merges preserve only the current observed recall suffix; historical rows still
receive ordinary erasure filtering before the wire row is recombined.
