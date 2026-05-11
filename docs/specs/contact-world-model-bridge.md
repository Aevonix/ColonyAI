# Contact-World Model Bridge

## Status
Draft — awaiting review before implementation

## Problem
Colony maintains two separate person databases that are currently disconnected at runtime:

| Store | Population | API | Current State |
|---|---|---|---|
| SQLite ContactStore | Curated imports (macOS, vCard, CSV) | `GET /v1/host/contacts` | Empty (`:memory:`, never imported) |
| Neo4j Graph (`:Person`) | Discovered from conversations | `GET /v1/host/world/entities/query` | 40 nodes |

The `/v1/host/contacts` endpoint returns `[]` because the SQLite store starts empty and has no bridge to the world model. Meanwhile, Neo4j contains 40+ Person nodes discovered from conversations, including handles (phone/email) needed for messaging. The `person_node_id` field exists in the SQLite schema but is never populated.

## Design Goal
Create a **unified contact view** where:
- The curated store remains the source of truth for *messaging* (trust tiers, handles, privacy)
- The world model remains the source of truth for *relationships* (scores, signals, memories)
- They stay linked via `person_node_id`
- Either side can seed the other

## Architecture

### 1. Person Discovery → Contact Creation (Neo4j → SQLite)

**New component:** `colony_sidecar/contacts/world_bridge.py`

**Trigger conditions** for auto-creating a shadow contact from a `:Person` node:

```
CREATE shadow contact IF:
  Person has name AND (
    Person has at least 1 handle (phone/email) on a linked :Handle node
    OR Person has 2+ :ABOUT relationships from :Memory nodes
    OR Person has 2+ :EXHIBITED relationships from :Signal nodes
  )
```

**Shadow contact defaults:**

| Field | Value |
|---|---|
| `display_name` | `Person.name` |
| `trust_tier` | `"acquaintance"` (new tier — see Schema Changes) |
| `interaction_allowed` | `False` (discovered people are not auto-messageable) |
| `import_source` | `"world_model"` |
| `person_node_id` | `Person.id` |
| `privacy_level` | `"private"` |

**Why require a threshold?** Prevents creating contacts from every name-entity extraction artifact. A Person must have *substance* (handles or 2+ interactions) before becoming a contact.

**Deduplication during creation:** Before creating, the bridge queries SQLite for existing contacts by:
- `person_node_id` (exact match)
- Handle overlap (normalized phone/email)
- Name similarity > 0.85

If a match is found, the bridge updates `person_node_id` on the existing contact instead of creating a duplicate.

### 2. Contact Import Merge (SQLite ← macOS/vCard/CSV)

**Extend `SQLiteContactImporter.import_raw()`** to check for existing *discovered* contacts before creating new ones.

When importing a curated contact:
1. Run existing dedup against other curated contacts (unchanged)
2. **NEW:** Query for discovered contacts (`import_source = "world_model"`) with overlapping handles
3. If handle match + name similarity > 0.70: **absorb the discovered contact**
   - Copy `person_node_id` to the curated contact
   - Soft-delete the discovered contact
   - Log audit: `"absorbed_discovered"`
4. If no match: create as normal (curated)

This ensures importing your phone book doesn't create duplicates for people Colony already discovered from conversations.

### 3. Unified API Response

**Extend `GET /v1/host/contacts`** with query params:

```
GET /v1/host/contacts?source=world_model&trust_tier=acquaintance
```

| Param | Values | Default |
|---|---|---|
| `source` | `curated` \| `world_model` \| `all` | `all` |
| `trust_tier` | any tier | none (all) |
| `include_discovered` | `true` \| `false` | `true` when `source=all` |

**Response ordering:**
1. Curated contacts first (by `trust_tier` rank desc, then `last_interaction_at` desc)
2. World-model contacts second (by `created_at` desc)

**Why not merge into one list?** The client (Hermes) needs to know who is message-safe. `interaction_allowed` covers this, but ordering makes it obvious.

### 4. Bidirectional Cleanup

**Neo4j Person deleted → SQLite shadow deleted**

Hook into `ColonyGraph` delete operations (or run as periodic sync):
```cypher
MATCH (p:Person)
WHERE p.id IN $known_person_node_ids
WITH collect(p.id) AS alive_ids
-- Soft-delete SQLite contacts whose person_node_id is no longer in graph
```

**SQLite contact hard-deleted → optional Neo4j archive**

On `hard_delete()` in `SQLiteContactStore`, if `person_node_id` is set:
- Option A (default): Do nothing — the world model retains historical relationship data
- Option B (configurable): Set `Person.archived_at = datetime()` — keeps node but marks it

**Soft delete in SQLite** does NOT affect Neo4j. A soft-deleted contact may still appear in relationship analysis; it just can't be messaged.

### 5. Enrichment Activation

`ContactEnricher.enrich_from_world_model()` is already implemented but never runs because `person_node_id` is always null. Once the bridge populates `person_node_id`, enrichment works automatically.

**Enrichment fields pulled from `:Person`:**
- `display_name` (if not set in SQLite)
- `organization` (from `Person.properties.company`)
- `relationship_score` (synced bidirectionally — see below)

### 6. Score Sync

`Person.score` (Neo4j) and `Contact.relationship_score` (SQLite) should stay in sync:

| Direction | Trigger | Action |
|---|---|---|
| Neo4j → SQLite | `RECORD_SCORE_CHANGE` query runs | Update `contact.relationship_score` if `person_node_id` linked |
| SQLite → Neo4j | `update_relationship_score()` called | Update `Person.score` via `graph.update_person()` |

This ensures relationship analysis (neglected contacts, tier changes) reflects the unified view.

## Schema Changes

### New Trust Tier: `acquaintance`

Insert between `unknown` and `peripheral`:

```python
TRUST_TIERS = (
    "inner_circle", "trusted", "regular",
    "peripheral", "acquaintance", "silenced", "unknown"
)

TIER_DEFAULT_INTERACTION["acquaintance"] = False
```

**Rationale:** Discovered contacts should not be messageable by default. They need curation (import or explicit tier promotion) before `interaction_allowed = True`.

### Migration: `002_add_acquaintance_tier.sql`

```sql
-- No schema change needed — trust_tier is TEXT with CHECK constraint.
-- Application-level update only. Existing "unknown" discovered contacts
-- can be batch-upgraded to "acquaintance" if desired.
```

### (Optional) Neo4j Schema Addition

Add `Person.source` property to distinguish curated vs. discovered:
```cypher
MATCH (p:Person) WHERE p.source IS NULL SET p.source = "discovered"
```

When a curated contact is linked, optionally set `Person.source = "curated"`.

## Files to Create / Modify

### New Files

| File | Purpose |
|---|---|
| `sidecar/colony_sidecar/contacts/world_bridge.py` | `WorldModelContactBridge` class |
| `sidecar/colony_sidecar/contacts/world_bridge.py` (tests) | Unit tests for dedup, thresholds, sync |
| `docs/specs/contact-world-model-bridge.md` | This spec |

### Modified Files

| File | Change |
|---|---|
| `sidecar/colony_sidecar/contacts/models.py` | Add `"acquaintance"` to `TRUST_TIERS` and `TIER_DEFAULT_INTERACTION` |
| `sidecar/colony_sidecar/contacts/importer.py` | Add discovered-contact absorption in `import_raw()` |
| `sidecar/colony_sidecar/contacts/store.py` | Add `find_by_person_node_id()`, `find_discovered_by_handles()` helper methods |
| `sidecar/colony_sidecar/api/routers/host.py` | Add query params to `list_contacts()`; join world-model contacts when `source=all` |
| `sidecar/colony_sidecar/server.py` | Initialize `WorldModelContactBridge` with graph + contact store; wire into startup |
| `sidecar/colony_sidecar/intelligence/graph/client.py` | Add `delete_person()`, `list_person_ids()` methods |
| `sidecar/colony_sidecar/intelligence/graph/queries.py` | Add `DELETE_PERSON`, `LIST_PERSON_IDS` queries |
| `sidecar/colony_sidecar/intelligence/mind_model/graph_baseline.py` | Add score-sync hook after `update_baseline()` |

## Implementation Order

1. **Schema** — Add `acquaintance` tier
2. **Graph client extensions** — `delete_person()`, `list_person_ids()`
3. **Store helpers** — `find_by_person_node_id()`, `find_discovered_by_handles()`
4. **Bridge core** — `WorldModelContactBridge.sync_person_to_contact()`
5. **Import merge** — Absorb discovered contacts during curated import
6. **API** — Query params + unified list
7. **Score sync** — Bidirectional `relationship_score` updates
8. **Cleanup cron** — Periodic `prune_orphaned_shadows()`
9. **Tests** — Full coverage

## Open Questions

1. **Should we auto-create shadow contacts on startup (one-time backfill)?**
   - Yes — run `bridge.backfill_all_people()` on server start if `_contacts_store` is empty.

2. **Should discovered contacts be visible to the autonomy engine?**
   - Yes — the world model already sees them. The SQLite store is primarily for the API / messaging layer.

3. **What if a Person node has no name?**
   - Skip. A contact without a display name is useless for messaging. The Person node stays in Neo4j for relationship analysis.

4. **Should `acquaintance` be messageable if a handle is present?**
   - No. Default `interaction_allowed = False`. The user must explicitly promote to `regular` or higher, or import them via macOS contacts.

## Appendix: Data Flow Diagram

```
┌──────────────────┐     conversation      ┌──────────────┐
│  WhatsApp / SMS  │ ────────────────────▶ │   Neo4j      │
│     Messages     │    extraction         │  :Person     │
└──────────────────┘                       └──────┬───────┘
                                                  │
                              bridge.sync_person_to_contact()
                                                  ▼
                                          ┌──────────────┐
                                          │   SQLite     │
                                          │  contacts    │
                                          │ (shadow)     │
                                          └──────┬───────┘
                                                 │
                              import_from_macos_contacts()
                              (absorbs shadow into curated)
                                                 ▼
                                          ┌──────────────┐
                                          │   SQLite     │
                                          │  contacts    │
                                          │  (curated)   │
                                          └──────┬───────┘
                                                 │
                              GET /v1/host/contacts?source=all
                                                 ▼
                                          ┌──────────────┐
                                          │    Hermes    │
                                          │   Agent      │
                                          └──────────────┘
```
