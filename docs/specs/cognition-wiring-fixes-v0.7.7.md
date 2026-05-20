# Cognition Wiring Fixes - v0.7.7

**Status:** ✅ Implemented
**Commit:** 80f4784
**Released:** 2026-04-26
**Effort:** 2 hours
**Priority:** High (blocks cognition quality, remote agent connectivity)

## Executive Summary

Seven issues preventing Colony from full cognition capability and proper remote connectivity:

| # | Issue | Severity | Impact |
|---|-------|----------|--------|
| 1 | CognitionPipeline not wired | High | PerformanceIndexComputer, GapDetector return defaults |
| 2 | Missing BELONGS_TO edge type | Medium | Person-specific memory queries fail |
| 3 | colony start wrong host binding | High | Remote agents can't connect |
| 4 | Missing pyarrow/lancedb deps | Medium | Vector store falls back to keyword search |
| 5 | Missing HF_TOKEN warning | Low | Slower embedding model downloads |
| **6** | **_load_dotenv() wrong path** | **Critical** | **Root cause of Issue 3** |
| **7** | **EventBus not instantiated** | Low | No real-time metrics from events |

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

Without these, `MetaLearner.evaluate()` returns default CPI (0.5) and gap detection is skipped.

### Solution

Use `CognitionPipeline` instead of direct `MetaLearner` instantiation. `CognitionPipeline` is a factory that auto-wires all five components.

### Implementation

**File:** `sidecar/colony_sidecar/server.py`

```python
# BEFORE (lines 469-479):
# --- 11. Cognition (MetaLearner) ---
try:
    from colony_sidecar.intelligence.cognition.metalearner import MetaLearner
    if graph is not None:
        metalearner = MetaLearner(graph=graph)
        set_metalearner(metalearner)
        logger.info("MetaLearner initialized")
    else:
        logger.warning("MetaLearner skipped — ColonyGraph not available")
except Exception as exc:
    logger.warning("MetaLearner init failed: %s", exc)

# AFTER:
# --- 11. Cognition (CognitionPipeline) ---
cognition_pipeline = None
try:
    from colony_sidecar.intelligence.cognition.registry import CognitionPipeline
    from colony_sidecar.events.bus import EventBus
    
    if graph is not None:
        # Create EventBus for real-time metrics (see Issue 7)
        event_bus = EventBus()
        
        cognition_pipeline = CognitionPipeline(
            graph=graph,
            event_bus=event_bus,
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
metalearner = registry.cognition
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
# Used in 6 locations:
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


# ──────────────────────────────────────────────────────────────────────
# Convenience exports

# AFTER:
    # Owner → Memory (ownership)
    REMEMBERS = "REMEMBERS"

    # Memory → Person (memory about a person)
    ABOUT = "ABOUT"

    # Memory → Person (ownership/assignment)
    BELONGS_TO = "BELONGS_TO"


# ──────────────────────────────────────────────────────────────────────
# Convenience exports
```

### Verification

1. Neo4j warnings should disappear
2. Person-specific memory queries should work:
   ```bash
   curl -X POST http://localhost:7777/v1/host/memory/search \
     -H "Authorization: Bearer colony" \
     -d '{"identity": {"host_id": "test"}, "query": "test", "person_id": "owner"}'
   ```

---

## Issue 3: colony start Wrong Host Binding

### Problem

`colony start` binds to `127.0.0.1` even when `COLONY_SIDECAR_HOST=0.0.0.0` is set in `~/.colony/.env`.

**Root Cause:** See Issue 6 — `_load_dotenv()` loads from CWD, not `~/.colony/.env`.

The CLI code is correct:
```python
# cli.py:212
host = args.host or os.environ.get("COLONY_SIDECAR_HOST", "127.0.0.1")
```

But `COLONY_SIDECAR_HOST` is never in `os.environ` because `_load_dotenv()` looks in the wrong place.

### Solution

Fix Issue 6 first. The CLI already reads `COLONY_SIDECAR_HOST` correctly once the env var is loaded.

### Implementation

See Issue 6 implementation.

### Verification

```bash
# Set in ~/.colony/.env:
COLONY_SIDECAR_HOST=0.0.0.0

# Run from any directory (not just ~/.colony/):
cd /tmp
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

**Alternative:** Make optional for smaller installs:

```toml
[project.optional-dependencies]
embeddings = ["pyarrow>=15.0", "lancedb>=0.10"]
```

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

## Issue 6: _load_dotenv() Loads from Wrong Path

### Problem

`_load_dotenv()` in `cli.py` loads `.env` from the **current working directory**, not `~/.colony/.env`:

```python
# cli.py:2092-2108
def _load_dotenv() -> None:
    """Simple .env loader — doesn't override existing env vars."""
    env_path = os.path.join(os.getcwd(), ".env")  # ❌ WRONG
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            # ... parse .env
```

**Impact:**
- `COLONY_SIDECAR_HOST=0.0.0.0` in `~/.colony/.env` is **never loaded**
- Running `colony start` from `~/.colony/` works, but from anywhere else fails
- This is the **root cause of Issue 3**

### Solution

Try `~/.colony/.env` first, then fall back to CWD.

### Implementation

**File:** `sidecar/colony_sidecar/cli.py`

```python
# BEFORE (lines 2092-2108):
def _load_dotenv() -> None:
    """Simple .env loader — doesn't override existing env vars."""
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                # Don't override existing env vars
                if k not in os.environ:
                    os.environ[k] = v

# AFTER:
def _load_dotenv() -> None:
    """Load .env from ~/.colony/ first, then CWD.
    
    Does not override existing environment variables.
    """
    from pathlib import Path
    
    # Priority: ~/.colony/.env > CWD/.env
    env_paths = [
        Path.home() / ".colony" / ".env",
        Path.cwd() / ".env",
    ]
    
    for env_path in env_paths:
        if not env_path.exists():
            continue
        
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # Don't override existing env vars
                    if k not in os.environ:
                        os.environ[k] = v
        
        # Only load first found .env
        break
```

### Verification

```bash
# Set in ~/.colony/.env:
COLONY_SIDECAR_HOST=0.0.0.0

# Run from different directory:
cd /tmp
colony start -d

# Verify:
lsof -i :7777
# Should show: TCP *:7777 (LISTEN)
```

---

## Issue 7: EventBus Not Instantiated

### Problem

`CognitionPipeline` accepts an `event_bus` parameter for real-time metrics, but no `EventBus` is instantiated in `server.py`.

```python
# CognitionPipeline.__init__ (registry.py:42-50):
def __init__(
    self,
    graph: Any,
    event_bus: Optional[Any] = None,  # ← Not passed
    config: Optional[MetaLearnerConfig] = None,
) -> None:
    # ...
    if event_bus is not None:
        self._subscribe(event_bus)  # Subscribes to goal.completed, task.completed, anomaly.detected
```

**Impact:**
- Cognition pipeline can't subscribe to real-time events
- Metrics are only computed during scheduled ticks, not on event triggers
- Less responsive cognition

### Solution

Create `EventBus` in `server.py` and pass to `CognitionPipeline`.

### Implementation

**File:** `sidecar/colony_sidecar/server.py`

```python
# In Issue 1 implementation, add:
from colony_sidecar.events.bus import EventBus

# Create EventBus for real-time metrics
event_bus = EventBus()

cognition_pipeline = CognitionPipeline(
    graph=graph,
    event_bus=event_bus,  # ← Pass the event bus
)
```

**Optional:** Expose EventBus globally for other components:

```python
# Add to api/routers/host.py:
_event_bus: Optional[EventBus] = None

def set_event_bus(bus: EventBus) -> None:
    global _event_bus
    _event_bus = bus

def get_event_bus() -> Optional[EventBus]:
    return _event_bus
```

### Verification

```python
# After startup:
from colony_sidecar.api.routers.host import get_event_bus
bus = get_event_bus()
assert bus is not None
assert len(bus._subscribers) > 0  # CognitionPipeline subscribed
```

---

## Implementation Checklist

- [x] **Issue 1:** Replace `MetaLearner` with `CognitionPipeline` in `server.py`
- [x] **Issue 1:** Create `EventBus` and pass to `CognitionPipeline`
- [x] **Issue 2:** Add `BELONGS_TO = "BELONGS_TO"` to `EdgeType` enum in `schema.py`
- [x] **Issue 3:** Verify fix after Issue 6 is resolved ✅ Host binding: `*:7777`
- [x] **Issue 4:** Add `pyarrow>=15.0` and `lancedb>=0.10` to `pyproject.toml`
- [x] **Issue 5:** Add HF_TOKEN documentation to setup wizard
- [x] **Issue 5:** Add HF_TOKEN to `.env.example`
- [x] **Issue 6:** Fix `_load_dotenv()` to check `~/.colony/.env` first
- [x] **Issue 7:** Create `EventBus` in `server.py`
- [x] **Issue 7:** (Optional) Expose EventBus via host router — skipped (not needed for MVP)

## Testing Checklist

### Unit Tests

- [ ] `test_cognition_pipeline.py`: Verify all components wired
- [ ] `test_schema.py`: Verify `BELONGS_TO` in `EdgeType` enum
- [ ] `test_cli.py`: Verify `_load_dotenv()` loads from `~/.colony/.env`
- [ ] `test_cli.py`: Verify host binding respects `COLONY_SIDECAR_HOST`
- [ ] `test_events.py`: Verify EventBus creation and subscription

### Integration Tests

- [ ] Start Colony, verify no "not wired" warnings in logs
- [ ] Start Colony from `/tmp` with `~/.colony/.env` containing `COLONY_SIDECAR_HOST=0.0.0.0`
- [ ] Verify external access to Colony (not just localhost)
- [ ] Query memory with `person_id`, verify no Neo4j warnings
- [ ] Verify vector store initializes without errors
- [ ] Emit test event, verify cognition pipeline receives it

### Regression Tests

- [ ] `colony init` still works on fresh install
- [ ] `colony start` still works without `.env`
- [ ] `colony start` from project directory with local `.env` still works
- [ ] Embedding model download still works without `HF_TOKEN` (slower but functional)

---

## Dependency Graph

```
Issue 6 ──→ Issue 3
   │
   └── Fix _load_dotenv() first, Issue 3 is automatically fixed

Issue 7 ──→ Issue 1
   │
   └── EventBus needed for full CognitionPipeline wiring
```

**Recommended implementation order:**
1. Issue 6 (_load_dotenv path)
2. Issue 2 (BELONGS_TO edge type)
3. Issue 4 (pyarrow/lancedb deps)
4. Issue 1 + Issue 7 (CognitionPipeline + EventBus)
5. Issue 5 (HF_TOKEN docs)

---

## Release Notes

### v0.7.7

**Fixes:**
- **Critical:** `_load_dotenv()` now loads from `~/.colony/.env` first (fixes remote agent connectivity)
- CognitionPipeline now auto-wires all cognition components (MetricsCollector, PerformanceIndexComputer, GapDetector, StrategyAdjuster)
- EventBus now created and passed to CognitionPipeline for real-time metrics
- Added `BELONGS_TO` edge type to schema (fixes person-specific memory queries)
- Added `pyarrow` and `lancedb` to dependencies (vector store now works out of box)
- Added `HF_TOKEN` documentation to setup wizard (faster model downloads)

**Migration:**
- No breaking changes
- Existing `.env` files work as-is
- `colony start` now correctly reads `~/.colony/.env` from any directory
- For remote agent support, ensure `COLONY_SIDECAR_HOST=0.0.0.0` in `~/.colony/.env`
