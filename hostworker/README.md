# Apsimo host worker

The stdlib-only action contract, capability catalog, execution ledger and
conformance checks used by Apsimo host adapters. This package has no sidecar or
web-server dependency.

Install `apsimo-hostworker==0.2.0` in a fresh environment and import
`apsimo_hostworker`. Legacy `colony_hostworker` imports resolve to the same
module objects. Existing action names and serialized contract identifiers are
preserved so pending actions and their digests remain valid.

Run `python -m apsimo_hostworker.conformance` to check a host implementation.
