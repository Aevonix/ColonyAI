# Theory of Mind v0.1 — Build Spec

## Overview

Theory of Mind (ToM) gives the agent a model of what each contact knows, believes, and feels. This is the social cognition layer — without it, the agent treats every conversation as starting from scratch. With it, the agent can track emotional valence, remember what it's told each person, and avoid repeating itself or missing emotional cues.

## What We're Building

Two subsystems in one:

1. **Affect Tracker** — per-contact emotional valence over time
2. **Shared Facts** — what the agent believes each contact knows (vs. what only the agent knows)

## What We're NOT Building

- No mind-reading — we track what's explicitly expressed or inferable from conversation
- No narrative/monologue — affect is a data signal, not a story
- No self-model — identity is already injected via Colony's identity subsystem

## Layer 1: Affect Store

### Data Model (SQLite)

```sql
CREATE TABLE affect_events (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    valence REAL NOT NULL,       -- -1.0 (negative) to 1.0 (positive)
    arousal REAL NOT NULL DEFAULT 0.5,  -- 0.0 (calm) to 1.0 (intense)
    source TEXT NOT NULL,         -- 'explicit' | 'inferred' | 'signal'
    trigger TEXT,                 -- what caused this: message excerpt, event type, etc.
    timestamp TEXT NOT NULL,
    session_id TEXT
);

CREATE INDEX idx_affect_contact ON affect_events(contact_id);
CREATE INDEX idx_affect_timestamp ON affect_events(timestamp);
```

### Current State (computed, not stored)

```sql
CREATE TABLE affect_state (
    contact_id TEXT PRIMARY KEY,
    current_valence REAL NOT NULL DEFAULT 0.0,
    current_arousal REAL NOT NULL DEFAULT 0.3,
    trend TEXT NOT NULL DEFAULT 'stable',  -- 'improving' | 'declining' | 'stable'
    last_event_id TEXT,
    last_updated TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0
);
```

### Valence Decay

Valence decays toward neutral (0.0) over time. Formula:

```
current_valence = current_valence * decay_factor^hours_since_last_event
```

Where `decay_factor = 0.95` (5% decay per hour). This means strong emotions fade but don't vanish instantly.

### API Endpoints

```
POST   /v1/host/affect/events          — record an affect event
GET    /v1/host/affect/state/{contact}  — get current affect state for a contact
GET    /v1/host/affect/history/{contact} — get affect history (paginated)
DELETE /v1/host/affect/events/{id}       — delete a specific event
```

### Context Assembly Injection

When affect is available for a contact, inject a section:

```json
{
  "id": "colony-affect",
  "title": "Emotional Context",
  "body": "Marc's mood: slightly positive (0.3), stable trend. Last shift 2h ago (positive reaction to Colony release progress).",
  "priority": 80
}
```

Priority 80 — higher than commitments (72) because emotional context should influence how the agent communicates.

### Autonomy Integration

- **Negative valence spike** (valence drops below -0.5 in a single event): emit `affect.negative_spike` event
- **Sustained decline** (trend = "declining" for 3+ consecutive checks): emit `affect.sustained_decline` event
- Autonomy worker checks every 30 minutes

---

## Layer 2: Shared Facts

### Data Model (SQLite)

```sql
CREATE TABLE shared_facts (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    fact TEXT NOT NULL,           -- the knowledge item
    source TEXT NOT NULL,         -- 'told_by_contact' | 'told_to_contact' | 'shared_context' | 'inferred'
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at TEXT NOT NULL,
    expires_at TEXT,              -- optional TTL
    metadata TEXT                 -- JSON blob for extra context
);

CREATE INDEX idx_shared_facts_contact ON shared_facts(contact_id);
CREATE INDEX idx_shared_facts_source ON shared_facts(source);
```

### API Endpoints

```
POST   /v1/host/mind/facts              — add a shared fact
GET    /v1/host/mind/facts?contact_id=   — list facts (filterable by contact)
GET    /v1/host/mind/facts/{id}          — get a specific fact
PATCH  /v1/host/mind/facts/{id}          — update a fact (confidence, expiry)
DELETE /v1/host/mind/facts/{id}           — delete a fact
```

### Context Assembly Injection

```json
{
  "id": "colony-shared-facts",
  "title": "Shared Knowledge with Marc",
  "body": "Things Marc knows: Colony v0.3.0 shipped today, Spark cluster is running, cognition substrate is live. Things only you know: Marc's API key for Moonshot, the digest channel config details.",
  "priority": 70
}
```

Priority 70 — lower than affect because emotional context should shape tone first, then content.

### Fact Categories

- `told_by_contact`: the contact told us something (they definitely know it)
- `told_to_contact`: we told the contact something (they should know it)
- `shared_context`: both parties were present when this was discussed
- `inferred`: the agent inferred the contact probably knows this (lower confidence)

---

## Layer 3: LLM Extraction

Both affect and shared facts should be auto-extracted from conversation turns when an LLM router is available.

### Affect Extraction

On each turn sync, if `_llm_router` is wired:
- Send last 2-3 messages to LLM with a prompt asking for valence/arousal rating
- If the LLM returns a non-neutral reading, create an affect event with `source='inferred'`
- Throttled: max 1 extraction per contact per 5 minutes

### Fact Extraction

On each turn sync, if `_llm_router` is wired:
- Send the conversation to LLM with a prompt asking what the contact now knows
- Compare with existing shared facts, add new ones
- This is the same pattern as commitment extraction — LLM-powered, not manual

---

## Layer 4: Cognition Integration

Affect and facts feed into the cognition trigger:

- **Negative spike** → auto-fire cognition trigger with reason `affect_spike`
- **New shared fact** → add to cognition context so the agent can reason about information asymmetry
- **Commitment + affect correlation**: if a commitment to a contact is overdue AND affect is declining, that's a high-priority cognition trigger

---

## Config

```bash
COLONY_AFFECT_ENABLED=true              # enable affect tracking
COLONY_AFFECT_DECAY_FACTOR=0.95         # hourly decay rate
COLONY_AFFECT_CHECK_INTERVAL_MINUTES=30  # autonomy check frequency
COLONY_FACTS_ENABLED=true               # enable shared facts
COLONY_TOM_LLM_EXTRACTION_ENABLED=false # LLM auto-extraction (off by default)
COLONY_TOM_EXTRACTION_THROTTLE_MINUTES=5 # min time between LLM extractions per contact
```

---

## Files to Create

- `sidecar/colony_sidecar/tom/__init__.py` — module init
- `sidecar/colony_sidecar/tom/affect.py` — AffectStore class
- `sidecar/colony_sidecar/tom/facts.py` — SharedFactsStore class
- `sidecar/tests/test_affect.py` — affect store tests
- `sidecar/tests/test_shared_facts.py` — shared facts tests

## Files to Modify

- `sidecar/colony_sidecar/api/routers/host.py` — affect + facts endpoints, context assembly injection
- `sidecar/colony_sidecar/api/schemas/host.py` — request/response schemas
- `sidecar/colony_sidecar/server.py` — store wiring + shutdown cleanup
- `sidecar/colony_sidecar/autonomy/condition_worker.py` — affect checks
- `src/sidecar-client.ts` — affect + facts API methods
- `src/types.ts` — TypeScript interfaces
- `src/config.ts` — config flags
- `src/context-cache.ts` — cache channels
- `src/event-handlers.ts` — event handlers
