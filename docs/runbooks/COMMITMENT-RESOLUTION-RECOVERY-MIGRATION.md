# Commitment resolution recovery migration and rollback

This migration adds an immutable operation proof for retry-safe commitment
settlement. It is additive, but its rollback floor changes after the first
proof is written. Treat source, installed package, and coordinated state as
separate rollback artifacts.

## 1. Entry conditions and rollback artifacts

Keep resolution effects off while staging the candidate. Stop every process
that can resolve workspace concerns, settle commitments, or write either
database before capturing the pre-migration rollback generation.

Record and preserve:

- the exact prior and candidate source revisions;
- the effective installed Colony package or immutable release directory;
- SQLite online backups of `colony-commitments.db` and
  `colony-workspace.db` from the same stopped-writer generation;
- any host journal or effect ledger that attests decisions represented in
  those databases.

Use SQLite's online backup API or the CLI `.backup` command. Do not copy a live
WAL database as an ordinary file. Verify `PRAGMA quick_check` on every backup,
record its SHA-256 and mode, and keep the generation read-only. A git revision
does not replace an installed-package rollback artifact, and a commitments
backup alone is not a consistent owner-decision rollback point.

## 2. Effects-off schema canary

Start the pinned candidate while all concern-resolution effects remain off.
Opening `colony-commitments.db` creates the recovery table and its three
protective triggers only when all four objects were previously absent. A
partial, malformed, same-name, or behaviorally different object set fails
startup; it is never repaired in place.

Require all of the following before enabling an effect:

1. `/v1/host/health` is `ok` and advertises
   `commitment_resolution_recovery_v1`;
2. `CommitmentStore.resolution_recovery_readiness()` returns `ready=true`;
3. both live databases and their backups pass `PRAGMA quick_check`;
4. this query returns zero:

   ```sql
   SELECT COUNT(*) FROM commitment_resolution_operations;
   ```

5. workspace concern and commitment projections agree on the staged no-effect
   probe.

At this point the schema has migrated but no durable operation proof exists.
The audited legacy floor `4669d6618343175d3439acf37c03364bf9eb53d2`
can still open the database and perform ordinary commitment create, update,
and terminal delete operations. This is the only supported code-only downgrade
below the recovery-capable revision.

## 3. Enable and verify one bounded effect

Enable one retry-safe, owner-authorized concern settlement. Verify:

- one terminal workspace cascade receipt exists;
- one matching `commitment_resolution_operations` row exists;
- its commitment exists and has the proof's terminal status;
- replay returns the same operation and performs no additional effect;
- proof update/delete and bound commitment deletion fail closed;
- health continues to advertise the recovery capability.

After this first proof, the recovery-capable revision is the minimum
data-forward code floor. Older code can read much of the additive schema, but
it cannot correctly manage operation-bound commitments and must not be used as
a routine rollback target.

## 4. Routine rollback

Prefer a functional rollback: disable new resolution effects and leave the
newer, truthful state in place. Keep running the recovery-capable code floor.
Do not automatically restore a pre-effect database, erase owner decisions, or
rewind immutable proofs merely to make an older binary start.

A code-only rollback below the recovery-capable floor is permitted only when
the operation count is still exactly zero. Recheck it with all resolution
writers stopped immediately before switching source or installed package.

If the count is nonzero, keep the final recovery-capable revision or a tested
forward-compatible successor. The bound-delete trigger intentionally makes an
older terminal-delete path fail rather than erase proof-bearing state.

## 5. Emergency data rollback

An emergency downgrade below the recovery-capable floor is destructive to all
post-backup decisions and is not a normal rollback. It requires explicit
deployment-owner authorization and all of these conditions:

1. stop every workspace, commitment, action, and reconciliation writer;
2. preserve the current databases and journals read-only for audit;
3. select one exact pre-effect generation whose workspace, commitments, and
   related host evidence are mutually consistent;
4. restore the complete generation, never only `colony-commitments.db`;
5. restore the matching prior source and installed package;
6. run `PRAGMA quick_check` on each restored database and verify the operation
   count is zero before starting any writer;
7. reconcile the lost post-backup decisions with the owner before effects are
   enabled again.

Never mix a pre-effect commitments database with a post-effect workspace
database or host receipt ledger. Never down-migrate by dropping the operation
table or its triggers in place.

## 6. Exit criteria

The migration is complete only when the exact candidate source and installed
package are pinned, the coordinated backup generation is recoverable, health
advertises the recovery capability, one bounded proof and replay have passed,
and the deployment records the recovery-capable minimum code floor. Until
then, leave effects off and retain the prior installed artifact.
