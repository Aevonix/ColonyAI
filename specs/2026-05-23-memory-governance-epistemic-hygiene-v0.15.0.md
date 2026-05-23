# Memory Governance and Epistemic Hygiene v0.15.0

**Status:** DRAFT
**Date:** 2026-05-23
**Author:** Agent
**Target Release:** v0.15.0

---

## 1. Overview

Colony's graph memory (`colony_sidecar.intelligence.graph`) stores beliefs as `:Memory` nodes with Ebbinghaus decay. This system works for retrieval but lacks governance: no source anchoring, no confidence refinement, no ground-truth reconciliation, no write policy, and no structured lifecycle beyond alive/pruned.

This spec introduces six architectural tiers that transform the memory system from a passive retrieval store into an actively managed belief state with provenance, conflict resolution, and source-of-truth reconciliation.

**Goal:** Prevent graph memory from becoming a second source of hallucination by binding every memory to its origin, tracking confidence as a computed property, reconciling against external sources, and governing the write/overwrite lifecycle.

**Non-goal:** Replacing the graph database, modifying Hermes core harness, or building a general-purpose knowledge base.

---

## 2. Current State Assessment

### 2.1 What Exists and Works

| Component | File | Status |
|-----------|------|--------|
| `ColonyGraph.store_memory()` | `intelligence/graph/client.py:165` | Functional — creates `:Memory` with embedding + entity links |
| `ColonyGraph.record_turn()` | `intelligence/graph/client.py:271` | Functional — stores episodic memories from conversations |
| `ColonyGraph.recall()` | `intelligence/graph/client.py:322` | Functional — vector + graph recall with strength filtering |
| `ColonyGraph.decay_memories()` | `intelligence/graph/client.py:464` | Functional — Ebbinghaus decay (daily) |
| `ColonyGraph.prune_weak_memories()` | `intelligence/graph/client.py:505` | Functional — deletes below threshold (weekly) |
| `MemoryConsolidator` | `intelligence/graph/consolidator.py:90` | Functional — dedup + conflict flagging (hourly) |
| `MemoryConsolidator._merge_pair()` | `intelligence/graph/consolidator.py:216` | Functional — merge with provenance array |
| `MemoryConsolidator._detect_conflicts()` | `intelligence/graph/consolidator.py:270` | Functional — negation heuristic + `CONFLICTS_WITH` edges |
| Autonomy loop phases | `autonomy/loop.py:916-960` | Functional — consolidation, decay, prune scheduled |

### 2.2 Critical Gaps (Block Reliable Belief Management)

**Gap A — No source anchoring**
`store_memory()` accepts `content`, `memory_type`, `entities`, `metadata`, `importance`, `person_id`, `session_id`. There is no `source_type`, `source_uri`, or `source_version`. A memory about a project setting cannot be traced back to the file it came from.

**Gap B — Single confidence primitive**
Only `importance` (float 0-1) and `strength` (computed by decay) exist. No distinction between writer confidence, source reliability, corroboration count, or contradiction count. A user assertion and an LLM inference have the same confidence model.

**Gap C — No ground-truth reconciliation**
Memories sourced from files are never re-checked against the file system. If a config file changes, the old memory decays naturally but is never explicitly marked as superseded. The agent may recall stale config indefinitely if decay hasn't reached threshold.

**Gap D — No write governance**
`store_memory()` is ungated. Any caller can write at any importance. There is no policy for protected memories, no authorization for overwrite, and no lifecycle state machine beyond "exists" or "pruned."

**Gap E — Provenance is merge-only**
The `provenance` array is only populated by `MemoryConsolidator._merge_pair()`. There is no provenance for initial creation, no supersession chain, and no tombstone for deleted memories.

**Gap F — Consolidator uses APOC**
`_merge_pair()` calls `apoc.create.relationship()` which requires the APOC plugin. If APOC is unavailable, consolidation crashes. This is a deployment dependency that should be eliminated or made optional.

### 2.3 What Does NOT Exist (Clarifying Scope)

1. **There is NO automated ground-truth verification.** The reconciliation phase described in this spec is new. Currently, the agent trusts its memory until it decays.
2. **There is NO epistemic state machine.** Memories are `:Memory` nodes forever (until pruned). There is no `:SUPERSEDED`, `:STALE`, or `:DEPRECATED` label.
3. **There is NO write audit trail.** Who called `store_memory()` is not recorded. The `session_id` field is optional and often `None`.
4. **The `CONFLICTS_WITH` edge does NOT trigger resolution.** It flags conflicts but takes no automated action. The autonomy loop does not have a conflict-resolution phase.
5. **`recall()` does NOT rank by confidence.** It ranks by `relevance = vector_score * strength`. A corroborated fact and a wild inference with the same strength are treated equally.

---

## 3. Design Principles

1. **Source-first.** Every memory must declare its origin. Unsourced memories are allowed but marked `source_type: inference` with low default confidence.
2. **Ground truth wins.** When a source file changes, memories derived from that file invalidate automatically. The graph is a cache, not a canon.
3. **User assertions are protected.** Memories with `source_type: user_assertion` get `protected: true`. They skip decay, consolidation, and prune. Only a newer user assertion can supersede them.
4. **Confidence is computed, not set.** The initial `importance` is a seed. Effective confidence derives from source reliability, corroboration, contradiction, recency, and verification state.
5. **Epistemic states are explicit.** Every memory has a state (`inferred`, `observed`, `corroborated`, `verified`, `stale`, `superseded`, `deprecated`, `archived`). `recall()` filters by default.
6. **No core harness changes.** All implementation stays within Colony's existing extension points: graph client, autonomy loop phases, API endpoints, and event broadcasting.
7. **APOC elimination.** The consolidation merge logic must work without APOC, using native Cypher only.

---

## 4. Data Model Changes

### 4.1 Memory Node Schema (Expanded)

New and modified properties on `:Memory`:

| Property | Type | Required | Default | Mutable |
|----------|------|----------|---------|---------|
| `id` | string (UUID) | Yes | auto | No |
| `content` | string | Yes | — | No |
| `type` | string | Yes | — | No |
| `importance` | float | Yes | 1.0 | No |
| `strength` | float | Yes | importance | Yes (decay) |
| `recalls` | int | Yes | 0 | Yes |
| `created_at` | datetime | Yes | now | No |
| `accessed_at` | datetime | Yes | now | Yes |
| `embedding` | float[] | No | null | No |
| `metadata` | string (JSON) | No | "{}" | No |
| `session_id` | string | No | null | No |
| `source_type` | enum string | Yes | "inference" | No |
| `source_uri` | string | No | null | No |
| `source_version` | string | No | null | No |
| `content_hash` | string | No | null | No |
| `base_confidence` | float | Yes | importance | No |
| `source_reliability` | float | Yes | lookup | No |
| `corroboration_count` | int | Yes | 0 | Yes |
| `contradiction_count` | int | Yes | 0 | Yes |
| `effective_confidence` | float | Yes | computed | Yes |
| `epistemic_state` | enum string | Yes | "inferred" | Yes |
| `protected` | bool | Yes | false | No |
| `last_verified_at` | datetime | No | null | Yes |
| `superseded_by` | string (UUID) | No | null | Yes |
| `provenance` | string[] | No | [] | Yes |

### 4.2 Source Type Enum

```python
class MemorySourceType(str, Enum):
    CONVERSATION = "conversation"      # From record_turn, dialogue
    FILE = "file"                      # From file ingestion, docs, code
    TOOL_OUTPUT = "tool_output"        # From tool execution
    USER_ASSERTION = "user_assertion"  # Explicitly stated by user
    INFERENCE = "inference"            # Agent-derived, unverified
```

### 4.3 Source Reliability Lookup

Static table (not stored per memory, referenced at creation):

| source_type | source_reliability | protected | decay_eligible |
|-------------|-------------------|-----------|----------------|
| `user_assertion` | 1.0 | true | false |
| `file` | 0.9 | false | true |
| `tool_output` | 0.85 | false | true |
| `conversation` | 0.7 | false | true |
| `inference` | 0.5 | false | true |

### 4.4 Epistemic State Enum

```python
class EpistemicState(str, Enum):
    # Active states (returned by recall() by default)
    INFERRED = "inferred"           # Just created, single source
    OBSERVED = "observed"           # Encountered twice or more
    CORROBORATED = "corroborated"   # Confirmed by >=2 distinct source_types
    VERIFIED = "verified"           # Ground-truth reconciliation passed
    # Terminal states (filtered from recall() by default)
    STALE = "stale"                 # Source changed, not yet superseded
    SUPERSEDED = "superseded"       # Newer memory exists with SUPERSEDES edge
    DEPRECATED = "deprecated"       # CONFLICTS_WITH exists, lower confidence
    ARCHIVED = "archived"           # Moved to cold storage after 30d terminal
```

### 4.5 New Relationships

| Relationship | From | To | Properties |
|-------------|------|-----|-----------|
| `SUPERSEDES` | `:Memory` (newer) | `:Memory` (older) | `superseded_at: datetime` |
| `CONFLICTS_WITH` | `:Memory` | `:Memory` | `detected_at: datetime`, `reason: string` |
| `CORROBORATES` | `:Memory` | `:Memory` | `corroborated_at: datetime` |
| `DERIVED_FROM` | `:Memory` | `:Entity` or `:FileAnchor` or `:Tool` | `derivation_type: string` |

The `MERGED_INTO` relationship is **retained and consistently created** during consolidation merges (fixing a pre-existing inconsistency where `_detect_conflicts()` checked for it but `_merge_pair()` never created it). See §8 for details.

### 4.6 New Node Label: `:FileAnchor`

Represents an external file that memories derive from.

| Property | Type | Purpose |
|----------|------|---------|
| `path` | string | Absolute file path |
| `last_seen_hash` | string | SHA-256 of file content at last reconciliation |
| `last_seen_at` | datetime | When the file was last read |
| `exists` | bool | Whether the file currently exists on disk |

**Creation:** `:FileAnchor` nodes are created lazily by `FileReconciler` on first encounter of a file-sourced memory. The Cypher is:
```cypher
MERGE (fa:FileAnchor {path: $path})
SET fa.last_seen_hash = coalesce(fa.last_seen_hash, $hash),
    fa.last_seen_at = coalesce(fa.last_seen_at, datetime()),
    fa.exists = true
```

When a `:Memory` is stored with `source_type = "file"`, a `DERIVED_FROM` edge is created:
```cypher
MATCH (m:Memory {id: $memory_id})
MERGE (fa:FileAnchor {path: $path})
MERGE (m)-[:DERIVED_FROM {derivation_type: 'file_source'}]->(fa)
```

---

## 5. Tier-by-Tier Specification

### Tier 1: Source Anchoring

**Scope:** Add `source_type`, `source_uri`, `source_version`, `content_hash` to `store_memory()` and all callers.

**Changes:**

1. **Modify `ColonyGraph.store_memory()` signature:**
```python
async def store_memory(
    self,
    content: str,
    memory_type: str,
    entities: List[str],
    metadata: Dict[str, Any] | None = None,
    importance: float = 1.0,
    person_id: Optional[str] = None,
    session_id: Optional[str] = None,
    source_type: str = "inference",           # NEW
    source_uri: Optional[str] = None,         # NEW
    source_version: Optional[str] = None,     # NEW
    content_hash: Optional[str] = None,       # NEW
) -> str:
```

2. **Set derived fields at creation:**
```python
source_reliability = SOURCE_RELIABILITY.get(source_type, 0.5)
protected = source_type == MemorySourceType.USER_ASSERTION
base_confidence = importance
epistemic_state = EpistemicState.INFERRED
```

3. **Update Cypher `CREATE` statement** to include all new properties.

4. **Update callers:**
- `record_turn()` → `source_type="conversation"`, `source_uri=session_id`
- `/v1/host/turns/sync` endpoint → passes `source_type="conversation"` via `record_turn()`
- File ingestion pipeline (part of Tier 3 reconciliation) → `source_type="file"`, `source_uri=filepath`, `source_version=git_sha`, `content_hash=sha256`
- Tool output capture (deferred to v0.16.0) → `source_type="tool_output"`, `source_uri=tool_name`
- Direct API calls via `/memory/write` → caller specifies `source_type`; router validates

**Validation:**
- `source_type="user_assertion"` is rejected by the `/memory/write` router. It is only permitted via the authenticated turn-sync pipeline or a dedicated `/memory/assert` endpoint (future work). This prevents code from falsely claiming user authority.

### Tier 2: Confidence Refinement

**Scope:** Replace `strength`-only ranking with `effective_confidence` computed from multiple signals.

**Formula:**
```python
def compute_effective_confidence(
    base_confidence: float,
    source_reliability: float,
    corroboration_count: int,
    contradiction_count: int,
    recalls: int,
    last_verified_at: datetime | None,
    created_at: datetime,
    epistemic_state: str,
    now: datetime,
) -> float:
    # Source weight
    confidence = base_confidence * source_reliability

    # Corroboration / contradiction adjustment
    net_support = corroboration_count - contradiction_count
    confidence *= min(1.0, 1.0 + net_support * 0.1)

    # Recall reinforcement (diminishing returns)
    confidence *= min(1.3, 1.0 + recalls * 0.03)

    # Recency discount (separate from Ebbinghaus decay)
    days_old = max(0, (now - created_at).days)
    recency_factor = math.exp(-days_old / 365.0 * 0.1)  # ~10% per year
    confidence *= recency_factor

    # Verification boost
    if last_verified_at and (now - last_verified_at).days < 7:
        confidence *= 1.2

    # State clamp
    if epistemic_state == EpistemicState.VERIFIED:
        confidence = max(confidence, 0.9)
    elif epistemic_state in (EpistemicState.STALE, EpistemicState.SUPERSEDED):
        confidence *= 0.3
    elif epistemic_state == EpistemicState.DEPRECATED:
        confidence *= 0.1

    return min(1.0, max(0.0, confidence))
```

**Changes:**
1. Add `compute_effective_confidence()` to `client.py` as a static method.
2. Modify `decay_memories()` to compute `effective_confidence` in addition to `strength`.
3. Modify `recall()` to sort by `effective_confidence` (primary) and `relevance` (secondary), with `min_confidence` parameter defaulting to 0.1.

**Migration note:** Existing memories without `effective_confidence` get backfilled on the first decay pass using `base_confidence = coalesce(m.importance, 1.0)` and default values for missing fields.

### Tier 3: Ground Truth Reconciliation

**Scope:** Daily phase that re-reads file-sourced memories and invalidates stale ones. Tool output reconciliation is **deferred to v0.16.0** due to idempotency and side-effect risks.

**New class: `FileReconciler`** (`intelligence/graph/reconciler.py`)

```python
class FileReconciler:
    def __init__(self, graph: ColonyGraph, project_root: str):
        self.graph = graph
        self.project_root = project_root

    async def run(self, dry_run: bool = False) -> ReconciliationResult:
        """Reconcile all file-sourced memories against disk."""
```

**Algorithm:**
1. Query all `:Memory` nodes where `source_type = "file"` and `epistemic_state in ['inferred', 'observed', 'corroborated', 'verified']`
2. Group by `source_uri` (file path)
3. For each file:
   a. `MERGE (fa:FileAnchor {path: $path})` if not exists; set `fa.exists = true`
   b. Check if file exists on disk. If not: mark all memories from this file `epistemic_state = STALE`, set `fa.exists = false`
   c. If exists: compute SHA-256 of file content
   d. Compare against `content_hash` on each memory and `fa.last_seen_hash`
   e. If hash matches: update `fa.last_seen_hash`, `fa.last_seen_at`, set `last_verified_at = now()` on memory
   f. If hash differs: mark old memory `epistemic_state = STALE`, update `fa.last_seen_hash` and `fa.last_seen_at`, emit `file_changed` event
4. For stale memories, attempt to find the new fact in the updated file using **exact substring match** of the memory `content` against the file text. If the memory content is not a direct substring, no superseding memory is created automatically.
   a. If found: create new `:Memory` with updated content, `SUPERSEDES` edge to old memory, `epistemic_state = OBSERVED`
   b. If not found: old memory stays `STALE` (fact may have been removed)

**Changes:**
1. Add `FileReconciler` class.
2. Add `_phase_reconciliation()` to autonomy loop (daily, after decay).
3. Add `POST /memory/reconcile` endpoint for manual trigger (see §6.4).

**Tool output reconciliation:** Deferred to v0.16.0. Tool re-execution has idempotency and side-effect risks that require a separate safety review.

### Tier 4: Write Governance

**Scope:** Policy enforcement at `store_memory()` time.

**Policy matrix (enforced in `store_memory()`):**

| source_type | max_importance | protected | auto_overwrite | consolidation_target |
|-------------|---------------|-----------|----------------|---------------------|
| `user_assertion` | 1.0 | true | false (newer user_assertion only) | never |
| `file` | 0.95 | false | true (by newer file memory with same entity) | normal |
| `tool_output` | 0.9 | false | true (by newer tool output) | normal |
| `conversation` | 0.8 | false | true | normal |
| `inference` | 0.7 | false | true | aggressive |

**Importance clamping:** If `importance > max_importance` for the source type, log a warning and clamp to the max. Do not reject the write — silent clamping prevents caller disruption while enforcing the policy.

**Overwrite rules:**
- If a `:Memory` exists with same `source_type`, `source_uri`, and overlapping entities, the new memory may supersede the old.
- For `user_assertion`, overwrite only if the user explicitly corrects themselves (detected via negation in conversation + same entity).
- For `file`, overwrite automatically on reconciliation when hash changes.
- For `tool_output`, overwrite if re-run produces different output.

**Protected memory rules:**
- `protected = true` → skip in `decay_memories()`, `prune_weak_memories()`, and `MemoryConsolidator`
- `protected` memories still participate in `recall()` and conflict detection
- Only `source_type = "user_assertion"` gets `protected = true`

### Tier 5: Epistemic State Machine

**Scope:** Explicit states with automated transitions.

**State transition rules:**

```
inferred ──(encountered again, same entity, any source)──> observed
observed ──(corroboration_count >= 2 from distinct source_types)──> corroborated
corroborated ──(ground-truth reconciliation passes)──> verified

any active ──(source file deleted or hash changed)──> stale
stale ──(new memory SUPERSEDES)──> superseded
any ──(CONFLICTS_WITH exists and conflicting memory has higher effective_confidence)──> deprecated
superseded ──(30 days in terminal state)──> archived
deprecated ──(30 days in terminal state)──> archived
stale ──(30 days in terminal state, no superseder found)──> archived
```

**Archived memories:**
- Relabeled from `:Memory` to `:ArchivedMemory`
- Removed from vector store
- Kept in graph for audit trail
- `recall()` never returns `:ArchivedMemory`

**Changes:**
1. Add `transition_epistemic_state()` method to `ColonyGraph`.
2. Modify `decay_memories()` to apply state transitions after strength computation.
3. Modify `MemoryConsolidator._merge_pair()` to set `epistemic_state = SUPERSEDED` on the merged memory instead of `DETACH DELETE`.
4. Modify `prune_weak_memories()` to only target `epistemic_state in [INFERRED, OBSERVED, STALE]` and `protected = false`.

### Tier 6: Semantic Versioning for Facts

**Scope:** Preserve history of belief changes via `SUPERSEDES` chain.

**Rules:**
1. When a memory is updated (not merged), create a NEW memory node with the updated content.
2. Link old → new with `SUPERSEDES` edge: `(new)-[:SUPERSEDES {superseded_at: datetime()}]->(old)`
3. Set old memory `epistemic_state = SUPERSEDED`, `superseded_by = new.id`
4. The new memory starts at `epistemic_state = INFERRED` (or `OBSERVED` if it's a re-encounter)
5. `recall()` follows `SUPERSEDES` chains up to 3 hops to find the current belief, but only returns the latest node.

**Consolidator change:**
- Instead of `DETACH DELETE` the merged node, mark it `SUPERSEDED` with a `SUPERSEDES` edge to the keeper.
- The keeper absorbs the content but the history is preserved.

---

## 6. API Changes

### 6.1 New Endpoints

All endpoints are under `/v1/host/memory/` to match the existing memory surface.

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| POST | `/memory/reconcile` | Trigger manual file reconciliation | Bearer |
| GET | `/memory/conflicts` | List memories with `CONFLICTS_WITH` edges | Bearer |
| POST | `/memory/verify` | Mark a memory as manually verified | Bearer |
| GET | `/memory/stats` | Count by epistemic state, source type | Bearer |

### 6.2 Modified Endpoints

| Method | Path | Change |
|--------|------|--------|
| POST | `/memory/write` | Expand `MemoryWriteRequest` with `source_type`, `source_uri`, `source_version`, `content_hash`. Add `source_type` validation in router. |
| POST | `/memory/search` | Add `min_confidence` to `MemorySearchRequest`; filter out terminal epistemic states by default |
| POST | `/memory/read` | Return new fields in `MemoryEntry` |

### 6.3 Request/Response Schemas

**Existing schemas to expand** (`colony_sidecar/api/schemas/host.py`):

```python
class MemoryEntry(BaseModel):
    id: str
    content: str
    type: Optional[str] = None
    strength: Optional[float] = None
    effective_confidence: Optional[float] = None      # NEW
    epistemic_state: Optional[str] = None             # NEW
    source_type: Optional[str] = None                 # NEW
    source_uri: Optional[str] = None                  # NEW
    source_version: Optional[str] = None              # NEW
    content_hash: Optional[str] = None                # NEW
    protected: Optional[bool] = None                  # NEW
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None
    score: Optional[float] = None

class MemoryWriteRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None
    content: str
    type: Optional[str] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    strength: Optional[float] = None
    source_type: Optional[str] = "inference"           # NEW
    source_uri: Optional[str] = None                   # NEW
    source_version: Optional[str] = None               # NEW
    content_hash: Optional[str] = None                 # NEW

class MemorySearchRequest(BaseModel):
    identity: HostIdentity
    query: str
    limit: Optional[int] = None
    min_score: Optional[float] = None
    min_confidence: Optional[float] = 0.1             # NEW
    person_id: Optional[str] = None
    types: Optional[List[str]] = None
    tags: Optional[List[str]] = None
```

**Note on `content_hash` for conversations:** The `content_hash` of a conversation-turn memory is the SHA-256 of the `summary` text passed to `record_turn()`. This allows exact-match deduplication even when `session_id` differs.

### 6.4 Graph Client Methods (New and Modified)

In addition to the existing `store_memory()`, `recall()`, `read_memories()`, `decay_memories()`, `prune_weak_memories()`, `touch_memory()`, and `record_turn()`, the following methods are added or modified in `ColonyGraph`:

**Modified `store_memory()`:**
- Signature expanded with `source_type`, `source_uri`, `source_version`, `content_hash`
- At creation, computes `source_reliability` from lookup table, sets `protected = (source_type == "user_assertion")`, `base_confidence = min(importance, max_importance_for_source)`, `epistemic_state = INFERRED`
- **Corroboration check:** Before creating the node, query for existing memories with embedding similarity > 0.85 (or exact `content_hash` match) and a different `source_type`. If found, increment `corroboration_count` on the existing memory and set the new memory's `epistemic_state = OBSERVED`.
- **Contradiction check:** Before creating, query for existing memories sharing an entity where content contains negation words and the existing memory does not (or vice versa). If found, create `CONFLICTS_WITH` edge and increment `contradiction_count` on both.

**New `verify_memory(memory_id: str) -> None`:**
```python
async def verify_memory(self, memory_id: str) -> None:
    async with self.driver.session(database=self.database) as session:
        await session.run("""
            MATCH (m:Memory {id: $memory_id})
            SET m.last_verified_at = datetime(),
                m.epistemic_state = CASE WHEN m.epistemic_state IN ['inferred', 'observed', 'corroborated']
                                         THEN 'verified' ELSE m.epistemic_state END
        """, memory_id=memory_id)
```

**New `get_memory(memory_id: str) -> Optional[Dict[str, Any]]`:**
```python
async def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
    async with self.driver.session(database=self.database) as session:
        result = await session.run(
            "MATCH (m:Memory {id: $id}) RETURN m {.*} AS memory", id=memory_id
        )
        record = await result.single()
        return dict(record["memory"]) if record else None
```

**Modified `read_memories()`:**
- Must return all new properties (`effective_confidence`, `epistemic_state`, `source_type`, etc.) so that `/memory/read` can populate `MemoryEntry` fully.

**Modified `decay_memories()`:**
- After updating `strength`, run a second batched query to update `effective_confidence` using the Cypher equivalent of `compute_effective_confidence()`.

**Modified `record_turn()`:**
- Pass `source_type="conversation"`, `source_uri=session_id`, `content_hash=sha256(summary.encode()).hexdigest()` to `store_memory()`.
```

**New schemas** (add to `host.py`):

```python
class MemoryReconcileRequest(BaseModel):
    identity: HostIdentity
    dry_run: Optional[bool] = False

class MemoryReconcileResponse(BaseModel):
    files_checked: int = 0
    memories_verified: int = 0
    memories_staled: int = 0
    memories_superseded: int = 0
    errors: List[str] = []

class MemoryConflictEntry(BaseModel):
    memory_id_a: str
    memory_id_b: str
    entity_name: str
    reason: str
    detected_at: Optional[str] = None

class MemoryConflictsResponse(BaseModel):
    conflicts: List[MemoryConflictEntry] = []
    total: int = 0

class MemoryVerifyRequest(BaseModel):
    identity: HostIdentity
    memory_id: str

class MemoryVerifyResponse(BaseModel):
    memory_id: str
    verified: bool
    effective_confidence: float = 0.0

class MemoryStatsResponse(BaseModel):
    by_state: Dict[str, int] = Field(default_factory=dict)
    by_source: Dict[str, int] = Field(default_factory=dict)
    total_active: int = 0
    total_archived: int = 0
    protected_count: int = 0
```

### 6.5 Router Implementation Details

**`POST /memory/write` validation:**
```python
if body.source_type == "user_assertion":
    # Only the turn-sync handler and authenticated reasoning endpoints
    # may claim user_assertion. The generic memory_write endpoint rejects
    # it to prevent code from falsely claiming user authority.
    raise HTTPException(
        status_code=403,
        detail="source_type 'user_assertion' is reserved for authenticated turn sync"
    )
```
The `turn_sync` endpoint (`/v1/host/turns/sync`) is the only public path that may pass `source_type="conversation"`. Internal autonomy-loop writes use `source_type="inference"`.

**`POST /memory/reconcile`:**
```python
async def memory_reconcile(body: MemoryReconcileRequest):
    reconciler = FileReconciler(_graph, project_root=os.getcwd())
    result = await reconciler.run(dry_run=body.dry_run)
    return MemoryReconcileResponse(...)
```

**`GET /memory/conflicts`:**
```python
async def memory_conflicts():
    query = (
        "MATCH (m1:Memory)-[c:CONFLICTS_WITH]->(m2:Memory) "
        "WHERE m1.epistemic_state <> 'archived' AND m2.epistemic_state <> 'archived' "
        "RETURN m1.id AS memory_id_a, m2.id AS memory_id_b, "
        "       c.reason AS reason, c.detected_at AS detected_at"
    )
    rows = await _graph.run_query(query, {})
    return MemoryConflictsResponse(conflicts=[...])
```

**`POST /memory/verify`:**
```python
async def memory_verify(body: MemoryVerifyRequest):
    await _graph.verify_memory(body.memory_id)
    # verify_memory sets last_verified_at=now(), epistemic_state='verified',
    # and recomputes effective_confidence with verification boost
    mem = await _graph.get_memory(body.memory_id)
    return MemoryVerifyResponse(...)
```

**`GET /memory/stats`:**
```python
async def memory_stats():
    # Two aggregation queries
    by_state = await _graph.run_query(
        "MATCH (m:Memory) RETURN m.epistemic_state AS state, count(*) AS cnt", {}
    )
    by_source = await _graph.run_query(
        "MATCH (m:Memory) RETURN m.source_type AS source, count(*) AS cnt", {}
    )
    ...
```

---

## 7. Autonomy Loop Integration

### 7.1 New Phases

| Phase | Schedule | File | Priority |
|-------|----------|------|----------|
| `_phase_reconciliation()` | Daily (after decay) | `autonomy/loop.py` | After decay, before prune |
| `_phase_conflict_resolution()` | Hourly (after consolidation) | `autonomy/loop.py` | After consolidation |
| `_phase_archive()` | Weekly (before prune) | `autonomy/loop.py` | Before prune |

### 7.2 Phase Details

**`_phase_reconciliation()`:**
1. Instantiate `FileReconciler(graph, project_root=os.getcwd())`
2. Call `await reconciler.run()`
3. Log counts: `files_checked`, `memories_verified`, `memories_staled`, `memories_superseded`
4. Emit `memory_reconciled` event

**`_phase_conflict_resolution()`:**
1. Query for `CONFLICTS_WITH` edges where neither node is `ARCHIVED` or `SUPERSEDED`
2. For each conflict:
   a. Compare `effective_confidence` of both nodes
   b. If confidence delta > 0.3: mark lower-confidence node `DEPRECATED`
   c. If confidence delta <= 0.3: leave both active, increment `contradiction_count` on both
3. Emit `conflict_resolved` or `conflict_persisted` event

**`_phase_archive()`:**
1. Query for memories where `epistemic_state in [SUPERSEDED, DEPRECATED, STALE]` and `updated_at < now() - 30 days`
2. For each:
   a. Create `:ArchivedMemory` node with all properties
   b. Copy relationships of types: `MENTIONS`, `ABOUT`, `SUPERSEDES`, `CONFLICTS_WITH`, `CORROBORATES`, `DERIVED_FROM`
   c. `DETACH DELETE` original `:Memory` node
   d. Remove from LanceDB vector store: `vector_store.delete(collection=Collection.MEMORIES, id=memory_id)`
3. Log count archived

### 7.3 Modified Existing Phases

**`_phase_memory_decay()`:**
- Skip memories where `protected = true`
- After computing `strength`, call `compute_effective_confidence()`
- Apply state transitions (e.g., `OBSERVED` → `CORROBORATED` if count met)

**`_phase_memory_consolidation()`:**
- Skip memories where `protected = true`
- Replace merge logic: instead of `DETACH DELETE`, mark merged node `SUPERSEDED` with `SUPERSEDES` edge
- Use native Cypher only (no APOC)

**`_phase_prune()`:**
- Skip `protected = true`
- Skip `epistemic_state in [VERIFIED, CORROBORATED]` unless `strength < 0.01`
- Target primarily `INFERRED` and `DEPRECATED` states

---

## 8. APOC Elimination

The `MemoryConsolidator._merge_pair()` method currently uses `apoc.create.relationship()`. Replace with native Cypher by enumerating known relationship types.

**Known relationship types in the graph schema:**
- `MENTIONS` (Memory → Entity)
- `ABOUT` (Memory → Person)
- `EXHIBITED` (Person → Signal)
- `DEPENDS_ON` (Agent → Subsystem)
- `CAUSED_BY`, `LED_TO`, `SUPPORTS` (Memory → Memory)
- `CONFLICTS_WITH` (Memory → Memory)
- `CORROBORATES` (Memory → Memory)
- `SUPERSEDES` (Memory → Memory)
- `DERIVED_FROM` (Memory → FileAnchor|Entity|Tool)

**Pre-existing inconsistency:** `_detect_conflicts()` checks for `MERGED_INTO` edges, but `_merge_pair()` never creates them. Fix: either (a) create `MERGED_INTO` edges during merge, or (b) remove the `MERGED_INTO` check from conflict detection. This spec chooses **(a)** — when a pair is merged, create `(keeper)-[:MERGED_INTO {merged_id: $merge_id, merged_at: datetime()}]->(merged)` before setting the merged node to `SUPERSEDED`.

**Native replacement for outgoing edges:**
```cypher
// MENTIONS
MATCH (m:Memory {id: $merge_id})-[r:MENTIONS]->(target)
MATCH (k:Memory {id: $keep_id})
MERGE (k)-[nr:MENTIONS]->(target)
ON CREATE SET nr = properties(r)
DELETE r

// ABOUT
MATCH (m:Memory {id: $merge_id})-[r:ABOUT]->(target)
MATCH (k:Memory {id: $keep_id})
MERGE (k)-[nr:ABOUT]->(target)
ON CREATE SET nr = properties(r)
DELETE r

// Repeat for CAUSED_BY, LED_TO, SUPPORTS, CONFLICTS_WITH, CORROBORATES, SUPERSEDES, DERIVED_FROM
```

**Native replacement for incoming edges:**
```cypher
// MENTIONS (incoming from Memory is impossible in current schema, but included for completeness)
MATCH (source)-[r:MENTIONS]->(m:Memory {id: $merge_id})
MATCH (k:Memory {id: $keep_id})
MERGE (source)-[nr:MENTIONS]->(k)
ON CREATE SET nr = properties(r)
DELETE r

// Repeat for ABOUT, CAUSED_BY, LED_TO, SUPPORTS, CONFLICTS_WITH, CORROBORATES, SUPERSEDES
```

**Maintenance:** When a new relationship type is added to the schema, it must be added to the consolidator's enumerated lists. A unit test should verify that the enumeration matches `_ALLOWED_CYPHER` in `client.py`.

**Approach:** Since relationship types are bounded and known, enumerate them explicitly. Add new types to the enumeration as the schema grows. This eliminates the APOC dependency entirely.

---

## 9. Testing Strategy

### 9.1 Unit Tests

| Test | Target | Assertion |
|------|--------|-----------|
| `test_store_memory_with_source` | `client.py` | Memory node has all source fields |
| `test_user_assertion_protected` | `client.py` | `protected = true`, skipped by decay |
| `test_effective_confidence_formula` | `client.py` | Correct computation for all input combos |
| `test_reconciler_file_unchanged` | `reconciler.py` | Memory stays `VERIFIED`, hash updated |
| `test_reconciler_file_changed` | `reconciler.py` | Old memory `STALE`, new memory created |
| `test_reconciler_file_deleted` | `reconciler.py` | Memory `STALE`, anchor `exists = false` |
| `test_consolidator_native_merge` | `consolidator.py` | No APOC calls, correct edge transfer |
| `test_conflict_resolution_deprecated` | `loop.py` | Lower-confidence node marked `DEPRECATED` |
| `test_archive_phase` | `loop.py` | Old `SUPERSEDED` memory becomes `:ArchivedMemory` |

### 9.2 Integration Tests

| Test | Setup | Assertion |
|------|-------|-----------|
| `test_end_to_end_file_memory_lifecycle` | Write file → store memory → modify file → reconcile → recall | Returns updated memory, old is `SUPERSEDED` |
| `test_corroboration_state_transition` | Store same fact as `file` and `tool_output` | Memory transitions `OBSERVED` → `CORROBORATED` |
| `test_recall_filters_archived` | Archive a memory, then recall | Memory not returned |

### 9.3 Performance Tests

| Test | Threshold |
|------|-----------|
| Reconcile 1000 file memories | < 30 seconds |
| Consolidate 500 memories | < 10 seconds |
| Recall with confidence sort | < 100ms |

---

## 10. Migration Plan

### 10.1 Schema Migration

Add new properties to existing `:Memory` nodes via batched Cypher:

```cypher
// Run in batches of 1000 until no rows remain
MATCH (m:Memory)
WHERE m.source_type IS NULL
WITH m LIMIT 1000
SET m.source_type = 'inference',
    m.source_reliability = coalesce(m.source_reliability, 0.5),
    m.base_confidence = coalesce(m.base_confidence, m.importance, 1.0),
    m.corroboration_count = coalesce(m.corroboration_count, 0),
    m.contradiction_count = coalesce(m.contradiction_count, 0),
    m.effective_confidence = coalesce(m.effective_confidence, m.strength, 1.0),
    m.epistemic_state = coalesce(m.epistemic_state, 'inferred'),
    m.protected = coalesce(m.protected, false),
    m.provenance = coalesce(m.provenance, [])
RETURN count(m) AS batch_size
```

Repeat until `batch_size` returns 0.

### 10.2 Data Backfill

Backfill must be performed by a Python script, not Cypher, because `metadata` is stored as a Python-stringified dict (`str(metadata)`) and is not queryable in Cypher without JSON parsing functions.

**Script: `scripts/backfill-memory-sources.py`**
```python
async def backfill():
    memories = await graph.run_query("MATCH (m:Memory) RETURN m {.*} AS mem", {})
    for mem in memories:
        updates = {}
        metadata_str = mem.get("metadata", "{}")
        try:
            metadata = eval(metadata_str) if metadata_str.startswith("{") else {}
        except Exception:
            metadata = {}

        if mem.get("session_id") and not mem.get("source_type"):
            updates["source_type"] = "conversation"
            updates["source_uri"] = mem["session_id"]
        if metadata.get("file_path") and not mem.get("source_type"):
            updates["source_type"] = "file"
            updates["source_uri"] = metadata["file_path"]

        if updates:
            await graph.run_query(
                "MATCH (m:Memory {id: $id}) SET m += $updates",
                {"id": mem["id"], "updates": updates}
            )
```

Run once after schema migration.

### 10.3 Deployment Order

1. Merge schema migration (adds properties, no breaking changes)
2. Deploy Tier 1 (source anchoring) — backfill runs automatically
3. Deploy Tier 2 (confidence refinement) — `decay_memories()` starts computing `effective_confidence`
4. Deploy Tier 3 (reconciliation) — manual trigger first, then enable in autonomy loop
5. Deploy Tier 4 (write governance) — enforced in `store_memory()`
6. Deploy Tier 5 (epistemic states) — state transitions active
7. Deploy Tier 6 (semantic versioning) — consolidator updated
8. Remove APOC dependency from deployment requirements

---

## 11. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Backfill takes too long on large graph | Medium | High | Run backfill in batches with `LIMIT`; use background job |
| Reconciliation I/O overwhelms disk | Medium | Medium | Rate-limit file reads; skip files > 10MB; use `mtime` cache |
| Confidence formula overweights recency | Low | High | Tune formula with real data; make parameters configurable |
| User assertions falsely claimed by code | Low | High | Enforce `user_assertion` only in authenticated handlers |
| APOC removal breaks other code | Medium | High | Audit all APOC usage in codebase before removal |
| Archive bloats graph size | Medium | Medium | Archive to separate LanceDB collection after 90 days |
| Conflict resolution too aggressive | Medium | Medium | Only auto-resolve if confidence delta > 0.3; default to flagging |

---

## 12. Open Questions (To Resolve Before Build)

1. Should `content_hash` be SHA-256 of full file or just the relevant section? Section-level hashing is more precise but harder to compute.
2. Should archived memories move to a separate Neo4j database or just a different label within the same database?
3. ~~What is the performance impact of `effective_confidence` computation on the daily decay pass for 10,000+ memories?~~ **Resolved:** The decay pass is already a single Cypher query. Adding `effective_confidence` to it requires extending the query with the formula's Cypher equivalent (or a second pass). This spec opts for a second pass: after `decay_memories()` updates `strength`, a follow-up query updates `effective_confidence` in batches of 1000. Benchmark target: < 5 seconds for 10,000 memories on local Neo4j.

---

## 13. Acceptance Criteria

- [ ] Every memory created after deployment has `source_type`, `source_uri`, and `effective_confidence`
- [ ] `recall()` returns no memories with `epistemic_state in [STALE, SUPERSEDED, DEPRECATED, ARCHIVED]` by default
- [ ] File-sourced memories are reconciled within 24 hours of file change
- [ ] User assertion memories never decay or get pruned
- [ ] APOC is no longer required for consolidation
- [ ] All existing memories are backfilled with default values
- [ ] Unit and integration tests cover all six tiers
- [ ] No performance regression in `recall()` (>100ms for top-10 results)
- [ ] API documentation updated for new endpoints and fields
