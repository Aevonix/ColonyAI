# Contributing to PacoMind

Develop against the current PacoMind baseline and its declared Hermes target.

## Development setup

PacoMind is Python-only.

```bash
git clone https://github.com/Aevonix/PacoMind.git
cd PacoMind/sidecar
pip install -e ".[dev]"
```

Use Python 3.12 for the current qualification target. The lightweight profile
uses SQLite and one OpenAI-compatible model endpoint. Tests use local fixtures
unless a selected qualification explicitly requires a real model or service.

## Repository layout

| Path | Purpose |
|---|---|
| `sidecar/pacomind/` | The Python package: FastAPI sidecar, CLI, all subsystems |
| `sidecar/pacomind/api/` | Pydantic schemas and routers: the single source of truth for the HTTP contract |
| `sidecar/pacomind/intelligence/` | Graph memory, mind model, cognition components |
| `sidecar/pacomind/workers/` | Worker daemons (`pacomind-worker` etc.) and their systemd/launchd deploy templates under `workers/deploy/` |
| `sidecar/tests/` | Sidecar test suite, kept out of the installed product package |
| `plugins/` | Host integration plugins: `hermes-plugin` (general adapter), `pacomind-memory` (memory provider), `feeds-manage` |
| `docs/` | Public docs (harness integration, channel framework, feeds, prompts) |

## Making changes

1. **Fork and branch**: create a feature branch from `main`
2. **Write code**: follow existing patterns in the codebase
3. **Test**: the full suite must pass before submitting
4. **Commit**: see commit conventions below
5. **PR**: open a pull request against `main`

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
feat(autonomy): PACOMIND_AUTONOMY_PRESET - one knob for the agency posture
fix(trust): durable graduation/demotion notices
docs(prompts): record adoption status, eval harness, version attribution
refactor(plugins): share the native adapter client
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

PacoMind uses **Semantic Versioning** (`MAJOR.MINOR.PATCH`): MINOR for compatible
features, PATCH for fixes and documentation, and MAJOR for incompatible public
contracts. The `pacomind` sidecar and `pacomind-hermes` integration are published together
on PyPI with the same version. `pacomind-hostworker` has its own package version.

Phase 1 establishes the first supported baseline; validation is still in
progress. Older PacoMind releases and migration paths are not supported public
contracts. Remove unused compatibility code when changing an area, while
accounting for current callers and retained state. Do not add compatibility
layers solely to preserve historical names.

## Release flow

1. Bump the synchronized versions in `pyproject.toml` and `sidecar/pyproject.toml`, including the `hermes` extra, and the two adapter manifests under `plugins/hermes-plugin` and `plugins/pacomind-memory`. The release workflow also builds and publishes `pacomind-hostworker`; if its packaged contents changed, bump `hostworker/pyproject.toml` and the source version fallback in `hostworker/pacomind_hostworker/__init__.py` before tagging. Do not publish changed hostworker bytes under an existing version.
2. Add an entry at the top of `CHANGELOG.md` (`## vX.Y.Z: title`, prose + bullets)
3. Commit and tag: `git tag vX.Y.Z && git push --tags`
4. CI (`.github/workflows/release.yml`) publishes to PyPI, pushes the Docker
   image to GHCR (`ghcr.io/aevonix/pacomind`), and creates the GitHub release
   from the changelog entry: all automatically on the tag push

## Architecture notes

The target ownership boundaries and remaining implementation work are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Keep deployment-specific hardware
and identity in private integrations. The notes below describe the existing
implementation, not a claim that every planned boundary is complete.

- **SQLite owns canonical source memory and typed world observations.** Other
  domains retain separate SQLite stores. Lance is an optional search index;
  the separate Neo4j memory graph still has executable callers. Hermes owns
  its transcripts and native work execution. Adapters own delivery outboxes.
- **Native adapters prepare context and capture turns.** They share the
  sidecar client and durable ingestion path rather than duplicating cognition.
- **Models are configured per instance.** Credentials stay in private
  configuration; the host configuration API is `POST /v1/host/configure`.
- **Enable new autonomy through measured behavior.** Start an unqualified
  loop in `off` or `shadow`, use its existing mode resolver, and require the
  applicable authority before consequential effects.

## Reporting issues

- **Bugs:** open an issue with reproduction steps, logs, and environment info
- **Features:** open an issue describing the use case and proposed approach
- **Security:** email security@aevonix.ai: do not open public issues

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
