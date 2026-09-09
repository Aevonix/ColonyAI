# Already captured host input

An authenticated host may run an existing native task inside
`colony_hermes.input_provenance.supplied_input(contact_id=..., session_id=...,
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
