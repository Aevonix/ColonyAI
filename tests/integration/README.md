# Integration Tests

Integration tests spin up the Python sidecar and hit it from the TypeScript plugin client to verify the HTTP contract works end-to-end.

## Running

### Option 1: Manual sidecar start

```bash
# Terminal 1: Start sidecar
cd sidecar
COLONY_SIDECAR_PORT=8765 COLONY_API_KEY=test-key python3 -m colony_sidecar.server

# Terminal 2: Run integration tests
SIDECAR_URL=http://localhost:8765 SIDECAR_API_KEY=test-key npm run test:integration
```

### Option 2: Let tests start sidecar (requires Python)

```bash
npm run test:integration
```

The test setup will try to spawn `python3 -m colony_sidecar.server` if no `SIDECAR_URL` is set.

## CI

Integration tests are **skipped in CI** by default (no Python environment). To run in CI:

1. Set up a sidecar service in your CI workflow
2. Set `SIDECAR_URL` and `SIDECAR_API_KEY` environment variables

## What's Tested

- Health endpoint
- Memory read/search (returns empty when unwired)
- Safety check (passes when unwired)
- Context assembly (enriched endpoint)
- Signals ingestion
- Turns sync
- Goals/Contacts/Insights listing (empty when unwired)
- Skills registry
- Autonomy status
- Identity status
- Error handling (501 for unwired reasoning)
