"""Colony Graph Cypher Query Templates.

All Cypher queries used by the Colony intelligence layer are defined here
as module-level constants.  This keeps query text out of business logic
and makes it easy to test / review / optimise in one place.

Parameters are referenced with ``$param`` syntax for Neo4j driver binding.
"""

# ──────────────────────────────────────────────────────────────────────
# Memory operations
# ──────────────────────────────────────────────────────────────────────

STORE_MEMORY = """\
CREATE (m:Memory {
    id: randomUUID(),
    content: $content,
    type: $memory_type,
    strength: 1.0,
    created_at: datetime(),
    accessed_at: datetime(),
    embedding: $embedding,
    metadata: $metadata
})
WITH m
UNWIND $entities AS entity_name
MERGE (e:Entity {name: entity_name})
CREATE (m)-[:MENTIONS]->(e)
RETURN m.id AS id
"""

RECALL_BY_ENTITY = """\
MATCH (e:Entity {name: $entity_name})<-[:MENTIONS]-(m:Memory)
WHERE m.strength >= $min_strength
RETURN m {.*, entity: e.name} AS memory
ORDER BY m.strength DESC, m.accessed_at DESC
LIMIT $limit
"""

RECALL_BY_TYPE = """\
MATCH (m:Memory {type: $memory_type})
WHERE m.strength >= $min_strength
RETURN m {.*} AS memory
ORDER BY m.strength DESC, m.created_at DESC
LIMIT $limit
"""

# ──────────────────────────────────────────────────────────────────────
# Decay & pruning
# ──────────────────────────────────────────────────────────────────────

DECAY_ALL = """\
MATCH (m:Memory)
WITH m,
     duration.between(m.accessed_at, datetime()).days AS days_since
SET m.strength = m.strength * (0.5 ^ (toFloat(days_since) / $half_life))
"""

PRUNE_WEAK_MEMORIES = """\
MATCH (m:Memory)
WHERE m.strength < $threshold
DETACH DELETE m
RETURN count(m) AS pruned
"""

TOUCH_MEMORY = """\
MATCH (m:Memory {id: $memory_id})
SET m.accessed_at = datetime(),
    m.strength = CASE
        WHEN m.strength + 0.1 > 1.0 THEN 1.0
        ELSE m.strength + 0.1
    END
RETURN m {.*} AS memory
"""

# ──────────────────────────────────────────────────────────────────────
# Person / signal queries
# ──────────────────────────────────────────────────────────────────────

GET_PERSON_WITH_SIGNALS = """\
MATCH (p:Person {id: $person_id})
OPTIONAL MATCH (p)-[:EXHIBITED]->(s:Signal)
WHERE s.timestamp >= datetime() - duration({days: 7})
WITH p, collect(s) AS signals
OPTIONAL MATCH (p)-[:HAS_CONTEXT]->(c:Context)
WHERE c.end_date >= date() OR c.end_date IS NULL
RETURN p {.*, signals: signals, contexts: collect(c)}
"""

GET_PERSON_SIGNALS_IN_WINDOW = """\
MATCH (p:Person {id: $person_id})-[:EXHIBITED]->(s:Signal)
WHERE s.timestamp >= datetime($cutoff)
RETURN s.signal_type AS type,
       s.normalized_value AS value,
       s.timestamp AS ts
ORDER BY s.timestamp DESC
"""

STORE_SIGNAL = """\
MERGE (p:Person {id: $person_id})
CREATE (s:Signal {
    id: randomUUID(),
    signal_type: $signal_type,
    raw_value: $raw_value,
    normalized_value: $normalized_value,
    timestamp: datetime($timestamp),
    source: $source
})
CREATE (p)-[:EXHIBITED]->(s)
RETURN s.id AS id
"""

# ──────────────────────────────────────────────────────────────────────
# Relationship scoring
# ──────────────────────────────────────────────────────────────────────

COMPUTE_RELATIONSHIP_SCORES = """\
MATCH (p:Person)
WHERE p.lastInteraction >= datetime() - duration({days: 90})
MATCH (p)-[:EXHIBITED]->(s:Signal)
WHERE s.timestamp >= datetime() - duration({days: 90})
WITH p, s.signal_type AS type,
     avg(s.normalized_value) AS avg_value,
     count(s) AS signal_count
WITH p, collect({type: type, avg: avg_value, count: signal_count}) AS signals
RETURN p.id, p.name, signals
"""

RECORD_SCORE_CHANGE = """\
MATCH (p:Person {id: $person_id})
SET p.score = $new_score,
    p.tier = $new_tier,
    p.lastInteraction = datetime()
WITH p
CREATE (se:ScoreEvent {
    id: randomUUID(),
    score: $new_score,
    tier: $new_tier,
    delta: $delta,
    reason: $reason,
    createdAt: datetime()
})
CREATE (p)-[:SCORE_CHANGED]->(se)
RETURN se.id AS id
"""

# ──────────────────────────────────────────────────────────────────────
# Relationship maintenance
# ──────────────────────────────────────────────────────────────────────

FIND_NEGLECTED_RELATIONSHIPS = """\
MATCH (p:Person)
WHERE p.tier IN ['inner_circle', 'trusted']
AND p.lastInteraction < datetime() - duration({days: 14})
RETURN p.id, p.name, p.tier, p.lastInteraction,
       duration.between(p.lastInteraction, datetime()).days AS days_since
ORDER BY days_since DESC
LIMIT $limit
"""

FIND_NEGLECTED_BY_TIER = """\
MATCH (p:Person)
WHERE p.tier = $tier
AND p.lastInteraction < datetime() - duration({days: $days_threshold})
RETURN p.id AS id, p.name AS name, p.tier AS tier,
       p.lastInteraction AS last_interaction,
       duration.between(p.lastInteraction, datetime()).days AS days_since
ORDER BY days_since DESC
"""

# ──────────────────────────────────────────────────────────────────────
# Traversal / graph exploration
# ──────────────────────────────────────────────────────────────────────

TRAVERSE_MEMORY_CONNECTIONS = """\
MATCH path = (m1:Memory)-[:CAUSED_BY|LED_TO|SUPPORTS*1..3]->(m2:Memory)
WHERE m1.id = $memory_id
AND all(node IN nodes(path) WHERE node.strength >= $min_strength)
RETURN m2 {.*} AS memory,
       length(path) AS distance,
       reduce(w = 1.0, r IN relationships(path) | w * r.weight) AS path_weight
ORDER BY path_weight DESC
LIMIT $limit
"""

FIND_SHARED_ENTITIES = """\
MATCH (m1:Memory {id: $memory_id})-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(m2:Memory)
WHERE m1 <> m2
AND m2.strength >= $min_strength
RETURN m2 {.*, shared_entity: e.name} AS memory
ORDER BY m2.strength DESC
LIMIT $limit
"""

# ──────────────────────────────────────────────────────────────────────
# Owner / setup
# ──────────────────────────────────────────────────────────────────────

ENSURE_OWNER = """\
MERGE (o:Owner {id: $owner_id})
ON CREATE SET o.name = $name,
              o.timezone = $timezone,
              o.created_at = datetime()
RETURN o {.*} AS owner
"""

LINK_PERSON_TO_OWNER = """\
MATCH (o:Owner {id: $owner_id})
MERGE (p:Person {id: $person_id})
ON CREATE SET p.name = $name,
              p.tier = $tier,
              p.score = 0.0,
              p.created_at = datetime()
MERGE (o)-[:KNOWS]->(p)
RETURN p {.*} AS person
"""


# ──────────────────────────────────────────────────────────────────────
# Baseline queries (GraphBaselineStore)
# ──────────────────────────────────────────────────────────────────────

GET_BASELINE = """\
MATCH (p:Person {id: $person_id})
RETURN p.baseline_msg_count AS msg_count,
       p.baseline_length_mean AS length_mean,
       p.baseline_length_m2 AS length_m2,
       p.baseline_length_std AS length_std,
       p.baseline_hour_histogram AS hour_histogram
"""

UPDATE_BASELINE = """\
MERGE (p:Person {id: $person_id})
ON CREATE SET p.baseline_msg_count = 0,
              p.baseline_length_mean = 0.0,
              p.baseline_length_m2 = 0.0,
              p.baseline_length_std = 0.0,
              p.baseline_hour_histogram = '[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]',
              p.baseline_updated_at = datetime(),
              p.created_at = datetime()
SET p.baseline_msg_count = $msg_count,
    p.baseline_length_mean = $length_mean,
    p.baseline_length_m2 = $length_m2,
    p.baseline_length_std = $length_std,
    p.baseline_hour_histogram = $hour_histogram,
    p.baseline_updated_at = datetime()
RETURN p.id AS id
"""
