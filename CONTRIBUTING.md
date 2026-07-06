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

You need Python 3.11+. A running Neo4j and model endpoints are only required
to run the sidecar itself (`colony init` / `colony start`); the test suite
stubs them.

## Repository layout

| Path | Purpose |
|---|---|
| `sidecar/colony_sidecar/` | The Python package: FastAPI sidecar, CLI, all subsystems |
| `sidecar/colony_sidecar/api/` | Pydantic schemas and routers — the single source of truth for the HTTP contract |
| `sidecar/colony_sidecar/intelligence/` | Graph memory, mind model, cognition components |
| `sidecar/colony_sidecar/workers/` | Worker daemons (`colony-worker` etc.) and their systemd/launchd deploy templates under `workers/deploy/` |
| `sidecar/tests/` | The test suite (plus co-located tests inside `colony_sidecar/`) |
| `plugins/` | Host integration plugins: `hermes-plugin` (general adapter), `colony-memory` (memory provider), `hermes-context` (context engine), `feeds-manage` |
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
python -m pytest tests/ colony_sidecar/ -q   # full suite (1,600+ tests)
python -m pytest tests/test_doctor.py -q     # one file
python -m pytest -x                          # stop on first failure
```

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

Colony uses **Semantic Versioning** (`MAJOR.MINOR.PATCH`). Everything is
`0.MINOR.PATCH` until v1.0, which means the API may still change: MINOR bumps
for new subsystems/endpoints/features, PATCH for fixes, docs, and hardening.

There is a single versioned artifact: the `colonyai` package on PyPI. (The
former `@aevonix/colonyai` npm package no longer exists.)

## Release flow

1. Bump `version` in `sidecar/pyproject.toml`
2. Add an entry at the top of `CHANGELOG.md` (`## vX.Y.Z — title`, prose + bullets)
3. Commit and tag: `git tag vX.Y.Z && git push --tags`
4. CI (`.github/workflows/release.yml`) publishes to PyPI, pushes the Docker
   image to GHCR (`ghcr.io/aevonix/colony`), and creates the GitHub release
   from the changelog entry — all automatically on the tag push

## Architecture notes

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
