# Accepted local source drafts

An owner can request a specific local comparison or summary with
`colony_accept_local_draft` or `POST /v1/host/commitments/local-draft`.
Acceptance names the question and one to eight absolute UTF-8 source paths.
The native tool derives the owner, session, turn and notification origin from
the actual transport. These are not model-selected arguments. An optional
`commitment_id` associates an existing open obligation. A standalone draft does
not create one.

## Native execution

`colony init --local-work` verifies function calling and creates a native
`colony-drafts` board and worker profile. The selected Hermes gateway owns
dispatch, claims, attempts, process recovery, terminal state and delivery.
Colony retains the explicit acceptance, commitment scope and report validation.
There is no new scheduler, service or Hermes core patch.

The host selects `COLONY_LOCAL_WORK_ENABLED=true`,
`COLONY_LOCAL_WORK_EXECUTOR=kanban`, `COLONY_LOCAL_WORK_BOARD` and
`COLONY_LOCAL_WORK_PROFILE`. Plugin `native_local_work` configuration names the
same board/profile, private report destination and instance directory. Setup
writes these bindings. The worker alone opts into the supported native
`kanban_complete` override, which validates the report before native completion.
Other profiles retain their ordinary terminal tool.

An accepted initiative is associated with one native task before that task
becomes ready. Concurrent and interrupted handoffs recover that association,
including when its task was later archived. Each worker must hold the current
native task/run/claim. An earlier attempt cannot complete a later one.
Native attempt counts appear with the accepted task in the owner current-work
view; native liveness and the verified sidecar result remain distinct facts.

The constrained worker reads selected sources through Hermes `read_file` and
captures their hashes. Every selected source must be read fully, within the
64 KiB and native 2,000-line limits. It cannot modify inputs, run a shell,
delegate work or choose an external recipient. Generated task instructions do
not enter owner turn-memory capture. Retained history is still native history.

## Routing, results and recovery

The gateway resolves the local `planning` role in the Colony interpreter and
refreshes the dedicated native profile before promoting new work. Nonempty
native dispatch ticks refresh that profile for subsequent attempts. The upstream
tick hook runs after dispatch, so a worker already launched keeps its earlier
valid snapshot. Model/endpoint changes reach following processes without a
gateway restart. Hermes owns the configured fallback chain. A failed refresh
does not replace the last valid profile or promote new work.

Completion requires a nonempty draft with every selected source handle and the
captured source hashes. Report and receipt are retained before sidecar/native
acknowledgment. Recovery reuses that report instead of drafting again. It may
need a finishing model call; zero additional inference is not promised.
A completed draft remains an **unverified interpretation** and never fulfils
its broader commitment. Cancellation fences subsequent reads and result acceptance.

When acceptance has a verified native channel origin, Hermes handles its normal
completion notification and retained artifact delivery to that origin. CLI
acceptances have no fabricated chat destination. Native crash/block notices can
precede completion; this is not a guarantee of one total message after a failure.

## Upgrade from the cron lane

Existing in-flight cron assignments retain their original result/recovery path.
Unassigned pending drafts move to native Kanban under the existing acceptance
transaction. The native dispatch hook pauses only the configured legacy draft
job after those assignments drain. Historical executions and reports remain.
The old runner is retained solely for this compatibility window; fresh installs
create no polling job. Rerun setup with `--local-work` after installing the matching
adapter, then restart the selected sidecar and gateway to load the binding.

This class covers selected local text evidence. It does not establish semantic
accuracy, arbitrary goal completion, or authorization for unrelated effects.
