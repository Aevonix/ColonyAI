# PacoMind host worker

The stdlib-only action contract, capability catalog, execution ledger and
conformance checks used by PacoMind host adapters. This package has no sidecar or
web-server dependency.

Install `pacomind-hostworker` in a fresh environment and import
`pacomind_hostworker`. Its canonical action names and serialized contract
identifiers must match the configured PacoMind endpoint.

Run `python -m pacomind_hostworker.conformance` to check a host implementation.
