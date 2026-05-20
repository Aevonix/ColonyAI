# Pattern Extraction + Surprise — Build Spec

## Overview

Pattern Extraction identifies recurring structures in Colony's world model: which entities appear together, which relationship types repeat, which temporal patterns emerge. Surprise is the flip side: when something breaks the pattern, it's noteworthy.

Together they give the agent a sense of "this is normal" vs. "this is unusual" — the foundation of curiosity and attention allocation.

## What We're Building

1. **Pattern Store** — observed patterns with frequency and recency
2. **Surprise Engine** — anomaly scoring when observations deviate from patterns
3. **Extraction Workers** — pull patterns from the world model on a schedule

## What We're NOT Building

- No machine learning models — statistical pattern matching only
- No real-time stream processing — batch extraction on a schedule
- No cross-agent pattern sharing — that's SuperColony territory

## Layer 1: Pattern Store

### Data Model (SQLite)

```sql
CREATE TABLE patterns (
    id TEXT PRIMARY KEY,
    pattern_type TEXT NOT NULL,     -- 'entity_cooccurrence', 'relation_frequency', 'temporal_sequence', 'attribute_cluster'
    description TEXT NOT NULL,      -- human-readable pattern description
    pattern_key TEXT NOT NULL,      -- normalized key for dedup (e.g. "entity:A→entity:B")
    frequency INTEGER NOT NULL DEFAULT 1,
    last_seen TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    metadata TEXT,                  -- JSON: type-specific data
    source TEXT NOT NULL DEFAULT 'extraction',  -- 'extraction' | 'manual' | 'inferred'
    active INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX idx_patterns_key ON patterns(pattern_key);
CREATE INDEX idx_patterns_type ON patterns(pattern_type);
CREATE INDEX idx_patterns_frequency ON patterns(frequency DESC);
```

### API Endpoints

```
POST   /v1/host/patterns                — register a pattern (manual or extraction)
GET    /v1/host/patterns                 — list patterns (filterable by type, min frequency)
GET    /v1/host/patterns/{id}            — get a specific pattern
PATCH  /v1/host/patterns/{id}            — update a pattern
DELETE /v1/host/patterns/{id}            — delete a pattern
POST   /v1/host/patterns/extract         — trigger extraction run
```

### Pattern Types

- **entity_cooccurrence**: Two entities frequently appear together (e.g., "User" + "ColonyAI")
- **relation_frequency**: A relationship type appears often (e.g., "person→works_on→project")
- **temporal_sequence**: Events follow a temporal pattern (e.g., "commit → CI run → deploy")
- **attribute_cluster**: Entities share attribute patterns (e.g., all projects have "status" and "priority")

### Extraction Logic

On extraction run:
1. Query world model for all entities and relationships
2. Compute cooccurrence pairs (entities that share relationships)
3. Count relationship type frequencies
4. Detect attribute clusters (shared keys across entities of same type)
5. Upsert into pattern store (increment frequency if pattern_key exists)

---

## Layer 2: Surprise Engine

### Data Model (SQLite)

```sql
CREATE TABLE surprises (
    id TEXT PRIMARY KEY,
    observation TEXT NOT NULL,       -- what was observed
    expected TEXT,                   -- what was expected (based on patterns)
    surprise_score REAL NOT NULL,    -- 0.0 (expected) to 1.0 (completely unexpected)
    pattern_id TEXT,                 -- the violated pattern (if any)
    context TEXT,                    -- JSON: surrounding context
    timestamp TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,  -- has this been acknowledged?
    resolution TEXT                  -- how it was resolved
);

CREATE INDEX idx_surprises_score ON surprises(surprise_score DESC);
CREATE INDEX idx_surprises_timestamp ON surprises(timestamp);
CREATE INDEX idx_surprises_resolved ON surprises(resolved);
```

### Surprise Scoring

When a new observation comes in:

1. **No matching pattern** → surprise = 0.7 (moderately surprising, never seen before)
2. **Pattern violated** (entity in unexpected context) → surprise = 0.5 + (pattern_confidence * 0.5)
3. **Low-frequency pattern match** → surprise = 0.2 (rare but known)
4. **High-frequency pattern match** → surprise = 0.0 (expected)

### API Endpoints

```
POST   /v1/host/surprises               — record a surprise observation
GET    /v1/host/surprises                — list surprises (filterable, sorted by score)
GET    /v1/host/surprises/{id}           — get a specific surprise
PATCH  /v1/host/surprises/{id}           — resolve/acknowledge a surprise
DELETE /v1/host/surprises/{id}           — delete a surprise
GET    /v1/host/surprises/unresolved     — get unresolved high-score surprises
```

### Autonomy Integration

- **High surprise** (score >= 0.8): emit `surprise.high` event
- **Surprise accumulation** (5+ unresolved surprises in 1 hour): emit `surprise.accumulation` event
- Autonomy worker checks every 30 minutes
- High surprise auto-fires cognition trigger with reason `surprise_anomaly`

### Context Assembly Injection

```json
{
  "id": "colony-surprises",
  "title": "Noteworthy Observations",
  "body": "Unexpected: 'User mentioned a new project called BlueBio' (surprise: 0.8, no prior pattern). Unresolved surprises: 2.",
  "priority": 75
}
```

Priority 75 — between affect (80) and shared facts (70). Surprises are worth knowing but shouldn't override emotional context.

---

## Layer 3: Extraction Workers

### Scheduled Extraction

Autonomy scheduler runs pattern extraction periodically:

- **Entity cooccurrence**: Every 6 hours
- **Relation frequency**: Every 6 hours
- **Attribute clusters**: Every 12 hours
- **Temporal sequences**: Every 24 hours (needs more data)

Each extraction:
1. Queries the world model (graceful no-op if not wired)
2. Computes patterns
3. Upserts into pattern store
4. Emits `pattern.extracted` event with count of new/updated patterns

### Config

```bash
COLONY_PATTERNS_ENABLED=true                # enable pattern extraction
COLONY_SURPRISE_ENABLED=true                # enable surprise engine
COLONY_PATTERNS_EXTRACTION_INTERVAL=21600   # seconds (6h default)
COLONY_SURPRISE_THRESHOLD=0.8               # high-surprise threshold
COLONY_SURPRISE_CHECK_INTERVAL_MINUTES=30   # autonomy check frequency
```

---

## Files to Create

- `sidecar/colony_sidecar/patterns/__init__.py` — module init
- `sidecar/colony_sidecar/patterns/store.py` — PatternStore class
- `sidecar/colony_sidecar/patterns/extract.py` — extraction logic
- `sidecar/colony_sidecar/surprise/__init__.py` — module init
- `sidecar/colony_sidecar/surprise/store.py` — SurpriseStore class
- `sidecar/colony_sidecar/surprise/scorer.py` — surprise scoring
- `sidecar/tests/test_patterns.py` — pattern store tests
- `sidecar/tests/test_surprise.py` — surprise store tests

## Files to Modify

- `sidecar/colony_sidecar/api/routers/host.py` — pattern + surprise endpoints, context assembly
- `sidecar/colony_sidecar/api/schemas/host.py` — request/response schemas
- `sidecar/colony_sidecar/server.py` — store wiring + shutdown + scheduler
- `sidecar/colony_sidecar/autonomy/condition_worker.py` — surprise checks
- `src/sidecar-client.ts` — API methods
- `src/types.ts` — TypeScript interfaces
- `src/config.ts` — config flags
- `src/context-cache.ts` — cache channels
- `src/event-handlers.ts` — event handlers
