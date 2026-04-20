# Contributing to Colony

Thanks for your interest in Colony! This guide covers how to contribute.

## Development Setup

```bash
git clone https://github.com/Aevonix/ColonyAI.git
cd colony

# Python sidecar
cd sidecar
pip install -e ".[dev]"
colony init
colony start

# TypeScript plugin
cd ..
npm install
npm run build
```

## Code Structure

| Directory | Language | Purpose |
|-----------|----------|---------|
| `sidecar/colony_sidecar/` | Python | FastAPI sidecar — all 21 intelligence subsystems |
| `src/` | TypeScript | OpenClaw plugin — context engine, agent harness, lifecycle hooks |
| `sidecar/colony_sidecar/api/` | Python | API schemas (Pydantic) and routers — single source of truth |
| `sidecar/colony_sidecar/intelligence/` | Python | Core intelligence: graph memory, mind model, cognition |

## Making Changes

1. **Fork and branch** — Create a feature branch from `main`
2. **Write code** — Follow existing patterns in the codebase
3. **Test** — Run the test suite before submitting
4. **Commit** — Use clear, descriptive commit messages
5. **PR** — Open a pull request against `main`

### Python Tests

```bash
cd sidecar
pytest                    # Run all tests
pytest tests/test_vector/ # Just vector subsystem
pytest -x                # Stop on first failure
```

### TypeScript Tests

```bash
npm test
```

### Type Generation

Python schemas → OpenAPI → TypeScript types:

```bash
colony generate-types
npm run generate-types
```

## Architecture Notes

- **Sidecar owns all state** — Neo4j for graph memory, SQLite for contacts/goals/task queue
- **Plugin is thin** — HTTP client + type mappings, no business logic
- **LLM credentials come from the host** — Colony never stores API keys itself
- **Embedding models auto-detected** — Hardware scanner picks the right tier at init

## Reporting Issues

- **Bugs:** Open an issue with reproduction steps, logs, and environment info
- **Features:** Open an issue describing the use case and proposed approach
- **Security:** Email security@aevonix.ai — do not open public issues

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
