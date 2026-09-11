# Contributing to Colony

Thanks for your interest in Colony! This guide covers how to contribute.

## Development setup

Colony is Python-only (the former TypeScript/npm plugin was removed along with
OpenClaw support in v0.21.14).

```bash
git clone https://github.com/Aevonix/ColonyAI.git
cd ColonyAI/sidecar
pip install -e ".[dev]"
```

You need Python 3.11+. The lightweight profile uses local storage and one
OpenAI-compatible model endpoint; Neo4j is optional. Tests use local fixtures
unless a selected qualification explicitly requires a real model or service.

## Repository layout

| Path | Purpose |
|---|---|
| `sidecar/colony_sidecar/` | The Python package: FastAPI sidecar, CLI, all subsystems |
| `sidecar/colony_sidecar/api/` | Pydantic schemas and routers — the single source of truth for the HTTP contract |
| `sidecar/colony_sidecar/intelligence/` | Graph memory, mind model, cognition components |
| `sidecar/colony_sidecar/workers/` | Worker daemons (`colony-worker` etc.) and their systemd/launchd deploy templates under `workers/deploy/` |
| `sidecar/tests/` | Sidecar test suite, kept out of the installed product package |
| `plugins/` | Host integration plugins: `hermes-plugin` (general adapter), `colony-memory` (memory provider), `feeds-manage` |
| `docs/` | Public docs (harness integration, channel framework, feeds, prompts) |

## Making changes

1. **Fork and branch** — create a feature branch from `main`
2. **Write code** — follow existing patterns in the codebase
3. **Test** — the full suite must pass before submitting
4. **Commit** — see commit conventions below
5. **PR** — open a pull request against `main`

### Running the tests

```bash
cd sidecar
python -m pytest tests/ -q                  # sidecar suite
python -m pytest tests/test_doctor.py -q     # one file
python -m pytest -x                          # stop on first failure
```

From the repo root with the qualified Hermes and sidecar installed, run
the native adapter, hostworker and source-recall harness contracts separately:

```bash
python -m pytest tests/hermes_adapter -q
python -m pytest hostworker/tests -q
python -m pytest benchmarks/source_recall/test_harness.py -q
```

Their distinct `conftest.py` modules require separate collection. The harness
uses a local synthetic HTTP server; it is a storage/transport check, not a
measurement of model quality or a production smoke test.

### Commit conventions

Subjects follow a conventional-commit-ish style, matching the git history:

```
feat(autonomy): COLONY_AUTONOMY_PRESET - one knob for the agency posture
fix(trust): durable graduation/demotion notices
docs(prompts): record adoption status, eval harness, version attribution
refactor(plugins): ONE canonical memory provider at plugins/colony-memory
chore(generic): genericize remaining identity strings in two test fixtures
```

Use `feat` / `fix` / `docs` / `refactor` / `chore` / `test` with an optional
scope, and make the body explain the *why*.

## The genericity rule

**This repository is deployment-agnostic.** It must never contain personal,
persona, or deployment specifics: no real names, phone numbers, hostnames,
channel ids, API keys, or infrastructure details from any live deployment.
Deployment values come exclusively from configuration and environment
variables (see `.env.example`). Test fixtures use neutral names and TEST-NET
addresses.

Before pushing, grep your diff:

```bash
git diff main | grep -iE "your-name|your-host|real-channel-ids|keys"
```

If a feature needs a deployment-specific value to work, add an env var and
document it in `.env.example` instead of hardcoding it.

## Versioning

Colony uses **Semantic Versioning** (`MAJOR.MINOR.PATCH`): MINOR for compatible
features, PATCH for fixes and documentation, and MAJOR for incompatible public
contracts. The `colonyai` sidecar and `colony-hermes` integration are published
together on PyPI with the same version. The former npm package is retired.

## Release flow

1. Bump `version` in both `pyproject.toml` and `sidecar/pyproject.toml`
2. Add an entry at the top of `CHANGELOG.md` (`## vX.Y.Z — title`, prose + bullets)
3. Commit and tag: `git tag vX.Y.Z && git push --tags`
4. CI (`.github/workflows/release.yml`) publishes to PyPI, pushes the Docker
   image to GHCR (`ghcr.io/aevonix/colony`), and creates the GitHub release
   from the changelog entry — all automatically on the tag push

## Architecture notes

The target ownership boundaries and migration rules are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Keep deployment-specific hardware
and identity in private integrations. The notes below describe the existing
implementation, not a completed migration to that target.

- **The sidecar owns all state** — Neo4j for the graph, LanceDB for vectors,
  SQLite for records (contacts, commitments, initiatives, action journal, ...)
- **Plugins are thin** — HTTP client + type mappings, no business logic
- **LLM credentials come from the host** — pushed at runtime via
  `POST /v1/host/configure`; Colony never requires model keys of its own
- **Autonomy is earned** — new agentic subsystems must default to `off` or
  `shadow`, resolve their mode through `util/autonomy_preset.py`, and route
  actions through the trust engine and directive boundaries

## Reporting issues

- **Bugs:** open an issue with reproduction steps, logs, and environment info
- **Features:** open an issue describing the use case and proposed approach
- **Security:** email security@aevonix.ai — do not open public issues

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
