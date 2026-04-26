# Cognition Wiring Fixes - v0.7.7

**Status:** Draft
**Effort:** 2-3 hours
**Priority:** High (blocks cognition quality, remote agent connectivity)

## Executive Summary

Five issues preventing Colony from full cognition capability and proper remote connectivity:

| Issue | Severity | Impact |
|-------|----------|--------|
| CognitionPipeline not wired | High | PerformanceIndexComputer, GapDetector return defaults |
| Missing BELONGS_TO edge type | Medium | Person-specific memory queries fail |
| colony start wrong host binding | High | Remote agents can't connect |
| Missing pyarrow/lancedb deps | Medium | Vector store falls back to keyword search |
| Missing HF_TOKEN warning | Low | Slower embedding model downloads |

---

## Issue 1: CognitionPipeline Not Wired

### Problem

`server.py` instantiates `MetaLearner` directly without wiring its dependencies:

```python
# Current (server.py:471-479):
try:
    from colony_sidecar.intelligence.cognition.metalearner import MetaLearner
    metalearner = MetaLearner(graph=graph)
    set_metalearner(metalearner)
    logger.info("MetaLearner initialized")
except Exception as exc:
    logger.warning("MetaLearner init failed: %s", exc)
```

`MetaLearner` requires four dependencies to be wired via setters:
- `MetricsCollector` — records goal/task completion metrics
- `PerformanceIndexComputer` — computes Cognitive Performance Index (CPI)
- `GapDetector` — detects gaps between current and desired performance
- `StrategyAdjuster` — proposes adjustments to close gaps

Without these, `MetaLearner.evaluate()` returns default CPI (0.5) and `GapDetector` raises `RuntimeError("GapDetector not wired")`.

### Solution

Use `CognitionPipeline` instead of direct `MetaLearner` instantiation. `CognitionPipeline` is a factory that auto-wires all five components.

### Implementation

**File:** `sidecar/colony_sidecar/server.py`

```python
# BEFORE (lines 469-479):
# --- 11. Cognition (MetaLearner) ---
try:
    from colony_sidecar.intelligence.cognition.metalearner import MetaLearner
    metalearner = MetaLearner(graph=graph)
    set_metalearner(metalearner)
    logger.info("MetaLearner initialized")
except Exception as exc:
    logger.warning("MetaLearner init failed: %s", exc)

# AFTER:
# --- 11. Cognition (CognitionPipeline) ---
cognition_pipeline = None
try:
    from colony_sidecar.intelligence.cognition.registry import CognitionPipeline
    if graph:
        cognition_pipeline = CognitionPipeline(
            graph=graph,
            event_bus=event_bus if 'event_bus' in dir() else None,
        )
        set_metalearner(cognition_pipeline.meta_learner)
        logger.info("CognitionPipeline initialized with all components wired")
    else:
        logger.warning("CognitionPipeline skipped — ColonyGraph not available")
except Exception as exc:
    logger.warning("CognitionPipeline init failed: %s", exc, exc_info=True)
```

### Verification

```python
# In autonomy loop, check:
assert metalearner.is_fully_wired  # Should be True
assert metalearner._metrics is not None
assert metalearner._performance_index is not None
assert metalearner._gap_detector is not None
assert metalearner._strategy_adjuster is not None
```

---

## Issue 2: Missing BELONGS_TO Edge Type

### Problem

Cypher queries use `BELONGS_TO` relationship type but it's not defined in `EdgeType` enum:

```python
# Used in 4 locations:
# - intelligence/graph/client.py:484, 485, 514, 540
# - intelligence/synthesis/connection_discoverer.py:181, 182, 266

MATCH (m:Memory)-[:BELONGS_TO]->(p:Person {id: $person_id})
```

```python
# schema.py EdgeType enum:
class EdgeType(str, Enum):
    KNOWS = "KNOWS"
    MENTIONS = "MENTIONS"
    ...
    MERGED_INTO = "MERGED_INTO"
    # BELONGS_TO = "BELONGS_TO"  # MISSING
```

### Solution

Add `BELONGS_TO` to `EdgeType` enum.

### Implementation

**File:** `sidecar/colony_sidecar/intelligence/graph/schema.py`

```python
# BEFORE (lines 155-160):
    # Owner → Memory (ownership)
    REMEMBERS = "REMEMBERS"

    # Memory → Person (memory about a person)
    ABOUT = "ABOUT"

# AFTER:
    # Owner → Memory (ownership)
    REMEMBERS = "REMEMBERS"

    # Memory → Person (memory about a person)
    ABOUT = "ABOUT"

    # Memory → Person (ownership/assignment)
    BELONGS_TO = "BELONGS_TO"
```

### Verification

1. Neo4j warnings should disappear
2. Person-specific memory queries should work:
   ```bash
   curl -X POST http://localhost:7777/v1/host/memory/search \
     -H "Authorization: Bearer colony" \
     -d '{"identity": {"host_id": "test"}, "query": "test", "person_id": "marc"}'
   ```

---

## Issue 3: colony start Wrong Host Binding

### Problem

`colony start -d` always binds to `127.0.0.1`, ignoring `COLONY_SIDECAR_HOST` from `.env`:

```python
# cli.py start command spawns:
subprocess.run([
    sys.executable, "-m", "uvicorn", "colony_sidecar.server:app",
    "--host", "127.0.0.1",  # ❌ Hardcoded
    "--port", str(port),
])
```

This prevents remote agents from connecting.

### Solution

Read `COLONY_SIDECAR_HOST` from `.env` and pass to uvicorn.

### Implementation

**File:** `sidecar/colony_sidecar/cli.py`

```python
# BEFORE (in start command):
host = "127.0.0.1"
port = args.port or int(os.environ.get("COLONY_SIDECAR_PORT", "7777"))

# AFTER:
# Load from .env if present
env_path = Path.home() / ".colony" / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

host = args.host or os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
port = args.port or int(os.environ.get("COLONY_SIDECAR_PORT", "7777"))
```

Also add `--host` argument to CLI:

```python
start_p.add_argument("--host", help="Bind address (default: from COLONY_SIDECAR_HOST or 127.0.0.1)")
```

### Verification

```bash
# Set in ~/.colony/.env:
COLONY_SIDECAR_HOST=0.0.0.0

# Run:
colony start -d

# Check:
lsof -i :7777
# Should show: TCP *:7777 (LISTEN), not TCP localhost:7777
```

---

## Issue 4: Missing pyarrow/lancedb Dependencies

### Problem

`pyarrow` and `lancedb` are required for vector store but not in `pyproject.toml`:

```
Vector store wiring failed (recall will use keyword fallback): No module named 'pyarrow'
Vector store wiring failed: No module named 'lancedb'
```

### Solution

Add to dependencies in `pyproject.toml`.

### Implementation

**File:** `sidecar/pyproject.toml`

```toml
# BEFORE:
dependencies = [
    "fastapi>=0.109",
    "uvicorn[standard]>=0.29",
    ...
    "sentence-transformers>=3.0",
]

# AFTER:
dependencies = [
    "fastapi>=0.109",
    "uvicorn[standard]>=0.29",
    ...
    "sentence-transformers>=3.0",
    "pyarrow>=15.0",      # Vector store backend
    "lancedb>=0.10",      # Vector store
]
```

**Note:** These are optional dependencies for vector embeddings. Consider making them optional:

```toml
[project.optional-dependencies]
embeddings = ["pyarrow>=15.0", "lancedb>=0.10"]
```

And handle gracefully in code if not installed.

### Verification

```bash
pip install -e ".[embeddings]"
python -c "import lancedb; print('OK')"
```

---

## Issue 5: Missing HF_TOKEN Warning

### Problem

Without `HF_TOKEN`, HuggingFace Hub rate-limits downloads:

```
Warning: You are sending unauthenticated requests to the HF Hub. 
Please set a HF_TOKEN to enable higher rate limits and faster downloads.
```

### Solution

Document `HF_TOKEN` in setup wizard and `.env.example`.

### Implementation

**File:** `sidecar/colony_sidecar/setup.py`

Add to Step 7 (embedding model download):

```python
# In Step 7, before downloading:
hf_token = os.environ.get("HF_TOKEN")
if not hf_token:
    print("  ⚠️ No HF_TOKEN set — downloads may be slower due to rate limits")
    print("     Get a token at: https://huggingface.co/settings/tokens")
    print("     Set it with: echo 'HF_TOKEN=hf_xxx' >> ~/.colony/.env")
```

**File:** `sidecar/.env.example`

```bash
# Add:
# HuggingFace token for faster model downloads (optional)
# HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxx
```

### Verification

With `HF_TOKEN` set:
- No warning in logs
- Faster model downloads on first run

---

## Implementation Checklist

- [ ] **Issue 1:** Replace `MetaLearner` with `CognitionPipeline` in `server.py`
- [ ] **Issue 2:** Add `BELONGS_TO = "BELONGS_TO"` to `EdgeType` enum in `schema.py`
- [ ] **Issue 3:** Load `COLONY_SIDECAR_HOST` from `.env` in `cli.py` start command
- [ ] **Issue 3:** Add `--host` argument to `colony start` CLI
- [ ] **Issue 4:** Add `pyarrow>=15.0` and `lancedb>=0.10` to `pyproject.toml`
- [ ] **Issue 5:** Add HF_TOKEN documentation to setup wizard
- [ ] **Issue 5:** Add HF_TOKEN to `.env.example`

## Testing Checklist

### Unit Tests

- [ ] `test_cognition_pipeline.py`: Verify all components wired
- [ ] `test_schema.py`: Verify `BELONGS_TO` in `EdgeType` enum
- [ ] `test_cli.py`: Verify host binding respects `COLONY_SIDECAR_HOST`

### Integration Tests

- [ ] Start Colony, verify no "not wired" warnings in logs
- [ ] Start Colony with `COLONY_SIDECAR_HOST=0.0.0.0`, verify external access
- [ ] Query memory with `person_id`, verify no Neo4j warnings
- [ ] Verify vector store initializes without errors

### Regression Tests

- [ ] `colony init` still works on fresh install
- [ ] `colony start` still works without `.env`
- [ ] Embedding model download still works without `HF_TOKEN` (slower but functional)

---

## Release Notes

### v0.7.7

**Fixes:**
- CognitionPipeline now auto-wires all cognition components (MetricsCollector, PerformanceIndexComputer, GapDetector, StrategyAdjuster)
- Added `BELONGS_TO` edge type to schema (fixes person-specific memory queries)
- `colony start` now respects `COLONY_SIDECAR_HOST` from `.env` (enables remote agent connections)
- Added `pyarrow` and `lancedb` to dependencies (vector store now works out of box)
- Added `HF_TOKEN` documentation to setup wizard (faster model downloads)

**Migration:**
- No breaking changes
- Existing `.env` files work as-is
- For remote agent support, add: `COLONY_SIDECAR_HOST=0.0.0.0`
