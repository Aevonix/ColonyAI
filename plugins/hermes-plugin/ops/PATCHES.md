# Hermes core patch migration

The canonical Colony sidecar integration has a zero-patch Hermes target. The
legacy `hermes-patch-runner.py` is now a read-only inventory command:

```bash
python hermes-patch-runner.py status --json
```

It exits `0` only when the registry is empty and never executes a patch. Migrate
required behavior to a Hermes plugin/config seam or an external Colony adapter,
then archive the deployment-specific patch with its rollback evidence.
