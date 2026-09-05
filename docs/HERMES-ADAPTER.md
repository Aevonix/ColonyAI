# Native Hermes adapter distribution

`colony-hermes` packages the existing general adapter and memory provider for
installation into the Python environment that runs Hermes. The Colony sidecar
is a separate service. The adapter does not install the sidecar, a context
engine, a worker daemon, or operating-system services.

Build from the repository root:

```sh
python -m pip install build
python -m build
```

Install the resulting wheel with the Python interpreter that runs Hermes:

```sh
python -m pip install dist/colony_hermes-0.1.0-py3-none-any.whl
```

The wheel exposes `colony` through `hermes_agent.plugins` and `colony-memory`
through `hermes_agent.memory_providers`. It maps the canonical source files in
`plugins/hermes-plugin/` and `plugins/colony-memory/` to importable packages.
Only `catalog.py` and `contract.py` from `hostworker/colony_hostworker/` are
included in the adapter's private catalog package. Source-checkout plugin paths
and the existing installer's copying behavior remain compatible.

## Activation and current limits

Installing the wheel makes the adapters discoverable. It does not change a
Hermes profile, select a memory provider, or enable tools. Activation requires
the existing general-adapter configuration, `plugins.enabled: [colony]`, and
`memory.provider: colony-memory` in the selected private profile. Preserve
other enabled plugins when editing that list. The existing coexistence settings
are also required in the Hermes process environment:

```sh
COLONY_GENERAL_PLUGIN_ACTIVE=1
COLONY_MEMORY_WORKER_TOOLS=0
COLONY_MEMORY_TURN_WRITER=disabled
```

Configure the sidecar URL and contact through the current `memory.config`
and `plugins.colony` configuration, and supply `COLONY_API_KEY` privately. The
general adapter needs a private writable turn outbox and verified participant
bindings. Consequential tools retain their existing mediator requirements.
This packaging change does not provision those dependencies.

Do not silently replace a selected external memory provider. Existing
directory installations also need explicit migration: Hermes gives directory
memory providers precedence over pip providers, while a pip general plugin can
override a same-name directory plugin. Remove or archive obsolete plugin
directories only as part of an intentional profile migration.

When the memory provider is selected, native CLI discovery exposes
`hermes colony-memory status`, `goals`, `context`, and `sync`. These commands
retain their existing environment and explicit-argument configuration behavior.
The Typer app remains available to existing callers.

Known follow-up work includes unifying provider setup/configuration, selected
profile paths, startup recovery, and durable compression callbacks. Packaging
does not establish production readiness or a lossless memory contract.

## Qualification

The qualification target is Hermes v0.21.0, tag `v2026.8.31`, commit
`29112bef099274229cadff79cdff7bf7b99c4b77`, Python 3.11 through 3.13. Other Hermes
releases are unqualified until the native-loader checks pass against them.
Hermes is installed separately; this package does not select or upgrade it.

Install test dependencies into an isolated environment with the target Hermes
checkout available, then run:

```sh
python -m pip install build pytest "setuptools>=77" wheel
python -m pytest tests/hermes_adapter -q
```

The tests build a wheel and a source distribution, rebuild from the source
distribution, install the wheel outside the checkout, and exercise the actual
Hermes general and memory loaders. Native-loader tests require Hermes and
are explicitly skipped when it is absent. No live sidecar, model endpoint,
production profile, or channel is contacted.
