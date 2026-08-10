# Governed action execution

Colony exposes one narrow mutation boundary for an external, owner-approved
action worker:

- `PUT /v1/host/actions/{uuid}` requires `actions:execute`.
- `GET /v1/host/actions/{uuid}` requires `actions:verify` and is read-only.

Both routes require the dedicated `host-action-worker` keyring role.
That role has `allow_unscoped_api: false`, exactly the two action scopes, the
`owner` audience, and an exact owner `viewer_person_id` repeated in
`person_ids`. The complete role template is in
`sidecar/api-keyring.example.json`. The legacy bearer, a wildcard/broadened
role, body identity fields, and an unbound owner role are all rejected.

## Execution contract

The PUT body is the exact bounded `ColonyGovernedActionExecutionV1`
document. It binds the URL action UUID, action and intent digests, validated
tool arguments, an owner-approval receipt digest, and an execution digest.
Approval data is evidence only: Colony derives the participant exclusively
from the authenticated keyring authority. The HTTP edge rejects declared or
streamed bodies above 32 KiB before parsing or touching the ledger.

The allowlist contains ten generic Colony operations:

- enable or disable the autonomy loop;
- create or resolve an owner commitment;
- record initiative feedback;
- record an owner insight;
- queue an owner-private durable research project; and
- complete, snooze, or dismiss a task.

Conversation context, sender IDs, phone numbers, prompts, and arbitrary tool
names are not accepted or forwarded. Responses use the fixed,
secret-free `ColonyGovernedActionExecutionResultV1` projection.

`colony_research` is a fast handoff, not an inline claim of completed
research. Its topic is bounded to 1,400 characters so the complete objective
survives Project planning and the canonical WorkOrder envelope without silent
truncation. A successful governed execution returns `outcome=queued` with
only the durable project ID, current lifecycle status, and requested depth.
The Project starts as `planning/pending`, is owner-private, and is restricted
to `memory:read`, `web:read`, and `reasoning`. The legacy ResearchPipeline
remains available to its existing routes but is never invoked by this action.

The ProjectEngine and its canonical WorkOrder adapter must both be attached,
and `COLONY_PROJECTS_MODE` must be `live`, before preparation succeeds. A
missing adapter or non-live mode produces a replay-stable `failed` preparation
record without starting an effect. If mode or adapter availability changes
after enqueue, the same Project is held resumably; it is not shadow-skipped or
locally executed. Only receipt-verified terminal WorkOrders can later support
a successful Project outcome.

The outer execution digest and returned effect digest use the host's canonical
UTF-8 JSON wire form. Argument, intent, ledger-replay, and stored-result
canonicalization retain their established ASCII-escaped representation.

## Durability and recovery

`governed-actions/ledger.db` is a separate SQLite ledger in an owner-only
mode-0700 directory under the Colony state directory. The database is an
owner-only mode-0600, single-link regular file opened without following
symlinks. It uses a full-synchronous rollback journal rather than persistent
WAL/SHM sidecars. An action advances through:

```text
prepared -> executing -> completed
              |             |
              +-> ambiguous +-> exact replay
prepared -> failed
```

`executing` is committed before a subsystem mutation begins. A completed
request is replayed byte-for-byte without another effect. Any exception after
execution starts, or an `executing` row found after restart, becomes durable
`ambiguous`; mutation retry is forbidden. A preparation failure is `failed`
because no effect has started. Verification uses only GET and cannot mutate.
Autonomy control is observationally honest: disable reports `disabled` only
after the loop is no longer running, otherwise `stop_requested`; enable uses
`enabled` only after a running state is observed, otherwise `start_requested`.

## Rollback posture

The route is inert without the exact scoped principal. For a source rollback,
remove that principal first, wait for in-flight PUTs to finish, then restore
the prior Colony revision. Preserve `governed-actions/ledger.db` with the rest
of the state directory: deleting it discards replay/ambiguity evidence and is
not a safe rollback. Re-enabling the route against the preserved ledger keeps
completed and uncertain actions from being dispatched twice. Colony's full
backup command snapshots this nested ledger with SQLite `VACUUM INTO` and
fails the backup rather than falling back to a potentially inconsistent raw
copy. Restore retains the private modes and refuses linked replacement paths.
It is create-only for this ledger: an existing ledger is never overwritten or
rewound by a normal restore. An incident recovery must first quarantine the
old ledger under a separate, explicit operator procedure.

Before rolling Colony back to a revision that predates governed Project
authority enforcement, disable ProjectEngine pursuit or explicitly hold every
nonterminal `source=governed_action` Project. Older code does not recognize
that source's immutable read-only capability boundary. Keep
`colony-projects.db` alongside the governed-action ledger so an exact replay
continues to discover the original durable Project rather than creating a
second handoff.
