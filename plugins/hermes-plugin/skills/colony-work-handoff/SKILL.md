---
name: colony-work-handoff
description: Resume, inspect or hand off an authorized task that spans sessions or runs in the background with Colony. Use existing work records to avoid duplicate execution and distinguish an accepted task, a running worker and a verified result.
---

# Continue the existing work

Use the latest shared-work context before starting a second worker. A task marked
accepted or queued has not necessarily started; a healthy process has not
necessarily made progress. Match the user's task to its recorded identifier,
inputs and intended outcome. An unavailable snapshot means status is unknown,
not that the work disappeared.

For a listed commitment that the current turn will work on directly, use
`colony_commitment_work(operation="claim", commitment_id=...)` before undertaking
it. If another session holds it, inspect `status` and use that session's retained
progress rather than duplicate its work. `release` relinquishes the undertaking;
it does not fulfill the commitment or cancel its worker. A claim grants no new
permission. Continue within the user's existing authorization without asking
again merely because the session or worker changed.

`colony_accept_local_draft(question, sources, commitment_id=...)` queues an
explicitly requested comparison or summary of selected absolute local UTF-8
paths. It is accepted work until the runner retains an actual draft. Use its
optional commitment ID only when it matches an existing obligation. The runner
claims that commitment itself; do not hold a foreground claim while handing it
off. It is not a generic download, coding,
device-control or sending executor. `colony_read_work_source(source=...)` is for
the bound draft worker, not a general chat source reader.

For another task class, use the executor actually provided by the deployment.
Retain its job ID and inspect that job's status or output. Do not create a second
scheduler, polling cron or background shell solely because a turn is ending.
If submission returns an ambiguous error, check whether the existing job was
accepted before submitting again. Use a documented resume or retry path after
an observed failure; do not kill a progressing job to make its status simpler.

Judge progress using task evidence: retained output, an advancing checkpoint,
completed source reads or the executor's terminal result. A heartbeat establishes
liveness only. If the executor already has a completion observer, let it report
through the original channel rather than adding repeated notifications.

At handoff, retain only what the next worker needs:
the objective, job or commitment ID, completed result, remaining step and any
actual blocker. Use an executor-provided update operation when available;
otherwise leave the handoff in the existing conversation. Do not invent a
work-record write API or a parallel ledger. Keep deployment paths and private
artifacts in private state.

At completion, inspect the actual output and the requested outcome. A generated
draft can be complete while its factual verification and broader commitment
remain unfinished. Say which result was observed and which part remains; a
successful tool call, a queued job or a finished subtask is not proof that the
whole request is done.
