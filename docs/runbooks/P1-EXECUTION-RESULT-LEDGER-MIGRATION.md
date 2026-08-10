# P1 execution-result ledger migration and rollback

This change is source-only until the host Action Plane emits
`ExecutionResultV1` and an independently operated receipt verifier is wired for
mutation/disclosure effects. It does not modify live services or live data.

## Forward migration

1. Stop new project dispatch and snapshot both `colony-projects.db` and
   `task_queue.db` together. Their relationship is the rollback unit.
2. Open a copy of `colony-projects.db` with `ProjectStore`. The migration only
   adds `steps.work_order_ref`, `steps.work_order_digest`,
   `steps.work_order_issued_at`, `steps.result_ref`, and three project-ledger
   tables. It does not rewrite or delete old rows.
3. Verify old projects/steps remain readable and the three new tables exist:
   `project_work_orders`, `project_execution_results`, and
   `project_execution_attempts`.
4. Deploy the typed host result producer and external-effect receipt verifier
   before enabling canonical project dispatch. A legacy `completed` payload is
   deliberately retained as `unverified`, never success.
   Colony loads a host-neutral verifier factory through:

   ```text
   PYTHONPATH=<pinned-host-release-root>
   COLONY_WORK_ORDER_RECEIPT_VERIFIER=<module>:<factory>
   COLONY_WORK_ORDER_RECEIPT_VERIFIER_CONFIG=<bounded JSON object>
   ```

   For the host deployment, pin the release root and use
   `ops.action_plane_receipt_verifier:ActionPlaneReceiptVerifier` with the
   canonical Action Plane database and exact executor identity. Never point
   `PYTHONPATH` at a mutable working tree. If the import, JSON, constructor, or
   verifier interface is invalid, ProjectEngine initialization fails closed;
   Colony does not silently continue with an unverified external-effect path.
5. Reconcile any pre-P1 WorkOrder jobs. The adapter detects their old transport
   IDs and returns `legacy_migration_hold` rather than posting duplicate work.
   Cancel or explicitly settle each old job before canonical reissue.
6. Canary one no-effect WorkOrder and one independently receipted external
   WorkOrder. Confirm the step points to the immutable WorkOrder/result rows and
   duplicate polling creates no duplicate attempt.

The current generic `deliver` WorkOrder does not carry a bounded recipient and
message envelope. Keep disclosure dispatch disabled until that authority data
is part of the immutable WorkOrder digest; do not infer a recipient or route it
through an unrelated conversational transport.

## Rollback

Old Colony code ignores the additive columns/tables, so a code rollback can
read the migrated project database. A code-only rollback is **not** sufficient
after new canonical jobs have been posted: the old adapter derives a different
job ID and could post duplicate work.

For rollback after dispatch begins, pause project dispatch, restore the paired
pre-migration `colony-projects.db` and `task_queue.db` snapshots, then restore
the prior source revision. Preserve the newer databases read-only for audit.
Do not down-migrate or delete the P1 ledger tables in place.
