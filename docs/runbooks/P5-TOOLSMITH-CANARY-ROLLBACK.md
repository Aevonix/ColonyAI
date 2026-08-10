# P5 Toolsmith migration, canary, and rollback

This runbook is deliberately separate from voice and Hermes. It changes only
the pinned Colony release and Toolsmith state. Keep `COLONY_TOOLSMITH=off`
until the backup and legacy inventory are complete.

## Preconditions and rollback anchor

1. Stage a clean, pinned Colony release containing the P5 commit. Record its
   commit and tree IDs. Do not point a service at a mutable worktree.
2. Record the current release link/working directory and current Toolsmith
   flags.
3. Before any Colony-sidecar restart on the host deployment, use the standing
   call-state guard and proceed only at `SAFE_IDLE`.
4. Stop the sidecar that owns Toolsmith before taking the final state backup.
5. Back up, with timestamps and hashes:

   - `$COLONY_STATE_DIR/colony-toolsmith.db` using SQLite `.backup`;
   - its `-wal`/`-shm` state by taking the backup while the writer is stopped
     (do not copy a live SQLite file blindly);
   - `$COLONY_STATE_DIR/toolsmith_library/`;
   - the service definition and environment file;
   - the prior pinned Colony release or bundle.

If the Toolsmith database does not exist, record that fact instead of creating
an empty production database during backup.

## Principal staging

Add two narrow principals alongside current credentials; do not rotate or
revoke unrelated consumers during this canary.

- The capture adapter receives `toolsmith:evaluate` and no graduation scope.
- The authenticated owner/Operator Deck adapter receives
  `toolsmith:graduate`, the exact owner person binding, and the `owner`
  audience. A general agent/channel credential must not receive this scope.

Keep the legacy bearer for unrelated consumers during migration. The two P5
mutation routes reject it even while the rest of the API remains compatible.

## Additive migration canary

1. With Toolsmith still off, point only the candidate sidecar at a copy of the
   state directory and start it once with `COLONY_TOOLSMITH=shadow`. Confirm:

   - `PRAGMA quick_check` returns `ok`;
   - existing tool rows retain source/checksum/status;
   - candidate/artifact/capability fields are populated;
   - existing non-empty artifact digests are not rewritten;
   - legacy `shadow_runs` do not appear as `clean_comparison_receipts`;
   - no tool changes to `live`.

2. Run the focused suite:

   ```text
   python -m pytest -q tests/test_toolsmith.py tests/test_sandbox.py \
     tests/test_scoped_api_authority.py tests/test_auth_coverage.py
   ```

3. Run the full Colony suite from the pinned release before production
   activation.

## One-tool shadow canary

Use a disposable pure candidate with a harmless captured procedure. Keep the
candidate output outside the incumbent/user action path.

1. Verify the draft. Confirm `shadow_runs`/self-test success does not make the
   graduation binding eligible.
2. Submit at least the configured number of distinct captured pairs through
   the `toolsmith:evaluate` principal. For each receipt verify:

   - the same input is used for both candidate runs;
   - `deterministic=true`, `matched=true`, and `success=true`;
   - only digests appear in the stored audit projection;
   - an exact retry returns `replayed=true` and does not increment the count;
   - changing a used `capture_id` fails with a conflict.

3. Negative checks:

   - a mismatched incumbent output records failure and blocks graduation;
   - oversized/non-JSON/undeclared input is rejected;
   - directive-manager failure returns `boundary_check_error` and executes
     nothing;
   - anonymous, legacy, evaluator-only, and non-owner principals cannot
     graduate;
   - an expired, multi-use, digest-mismatched, or rebound authority fails.

4. From `GET /v1/host/self/tools/{id}`, copy the exact current candidate and
   artifact digests into a fresh owner approval envelope. Give it a unique
   authority/decision ID, `max_uses=1`, and a validity window no longer than
   15 minutes.
5. Submit through the owner-bound `toolsmith:graduate` principal. Confirm the
   status and dynamic provider change once, one graduation receipt exists,
   and an exact retry is an idempotent read.
6. Invoke the harmless tool with a read-only canary and compare the result to
   the incumbent. Then retire it through the existing disable route and
   confirm it disappears from the dynamic provider.

## Activation gate

Do not enable production Toolsmith until all are true:

- the full suite passes on the pinned candidate;
- the state backup restores successfully in a disposable directory;
- every existing live tool is inventoried;
- legacy live tools are either retired or explicitly scheduled for P5
  requalification (Doctor warns while any remain);
- capture and owner principals are independently scoped;
- the one-tool positive and negative canaries pass;
- operator-visible latency for read-only invocation is not materially worse
  than the prior sandbox path.

Start in `COLONY_TOOLSMITH=shadow`. Changing to `live` does not remove the
owner authority requirement; it only enables the normal live Toolsmith
cadence. Observe at least one full cadence before expanding use.

## Rollback

1. Set `COLONY_TOOLSMITH=off` first. Wait for/confirm `SAFE_IDLE`, then stop the
   Colony sidecar.
2. Point the service back to the recorded prior pinned release and restore the
   prior service definition/environment.
3. If no P5 state mutation occurred, the schema is additive and the older
   code can ignore the new columns/tables. If any comparison, retirement, or
   graduation occurred, restore `colony-toolsmith.db` with SQLite from the
   pre-canary backup and restore `toolsmith_library/` as one matched pair.
4. Start the prior sidecar, verify health/Doctor/context reads, and leave
   Toolsmith off until the incident is understood.
5. Preserve the failed database, release IDs, logs, and canary receipts for
   diagnosis. Do not delete or overwrite the rollback evidence.

Rollback success means the prior release and exact pre-canary Toolsmith state
are active, no P5 candidate tool is advertised, and normal Colony read paths
remain healthy.
