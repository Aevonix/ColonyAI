// Colony World Model — Neo4j Schema Extensions
// Version: wm-001 through wm-005
//
// All World Model nodes carry the WMEntity base label plus a type-specific label.
// All World Model relationship types carry the WM_ prefix to avoid collisions
// with existing Colony graph data.

// ── wm-001: Base entity constraints and full-text index ─────────────────────

CREATE CONSTRAINT wm_entity_id_unique
  IF NOT EXISTS
  FOR (e:WMEntity) REQUIRE e.id IS UNIQUE;

CREATE CONSTRAINT wm_entity_name_exists
  IF NOT EXISTS
  FOR (e:WMEntity) REQUIRE e.name IS NOT NULL;

CREATE CONSTRAINT wm_entity_type_exists
  IF NOT EXISTS
  FOR (e:WMEntity) REQUIRE e.entity_type IS NOT NULL;

CREATE FULLTEXT INDEX wm_entity_name_fulltext
  IF NOT EXISTS
  FOR (e:WMEntity)
  ON EACH [e.name];

// ── wm-002: Per-type node constraints ────────────────────────────────────────

CREATE CONSTRAINT wm_person_id_unique
  IF NOT EXISTS
  FOR (p:WMPerson) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT wm_company_id_unique
  IF NOT EXISTS
  FOR (c:WMCompany) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT wm_project_id_unique
  IF NOT EXISTS
  FOR (p:WMProject) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT wm_product_id_unique
  IF NOT EXISTS
  FOR (p:WMProduct) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT wm_location_id_unique
  IF NOT EXISTS
  FOR (l:WMLocation) REQUIRE l.id IS UNIQUE;

CREATE CONSTRAINT wm_event_id_unique
  IF NOT EXISTS
  FOR (e:WMEvent) REQUIRE e.id IS UNIQUE;

CREATE CONSTRAINT wm_concept_id_unique
  IF NOT EXISTS
  FOR (c:WMConcept) REQUIRE c.id IS UNIQUE;

// ── wm-003: Relationship type constraints ─────────────────────────────────

// Defined WM relationship types:
// WM_WORKS_AT, WM_FOUNDED, WM_OWNS, WM_INVESTED_IN, WM_ADVISES,
// WM_MANAGES, WM_REPORTS_TO, WM_KNOWS, WM_PARTNER_OF, WM_ACQUIRED,
// WM_PART_OF, WM_LOCATED_IN, WM_MEMBER_OF, WM_ATTENDED, WM_ORGANIZED,
// WM_PRODUCED, WM_USES, WM_RELATED_TO, WM_TAGGED_WITH, WM_MENTIONS,
// WM_PREDECESSOR_OF, WM_SUCCESSOR_OF

// ── wm-004: Range and temporal indexes ───────────────────────────────────

CREATE INDEX wm_entity_confidence_range
  IF NOT EXISTS
  FOR (e:WMEntity)
  ON (e.confidence);

CREATE INDEX wm_rel_valid_from
  IF NOT EXISTS
  FOR ()-[r:WM_WORKS_AT]-()
  ON (r.valid_from);

CREATE INDEX wm_rel_valid_to
  IF NOT EXISTS
  FOR ()-[r:WM_WORKS_AT]-()
  ON (r.valid_to);

CREATE INDEX wm_event_start_time
  IF NOT EXISTS
  FOR (e:WMEvent)
  ON (e.start_time);

// ── wm-005: External ID indexes ───────────────────────────────────────────

CREATE INDEX wm_company_domain
  IF NOT EXISTS
  FOR (c:WMCompany)
  ON (c.domain);

CREATE INDEX wm_person_email
  IF NOT EXISTS
  FOR (p:WMPerson)
  ON (p.email);

CREATE INDEX wm_company_ticker
  IF NOT EXISTS
  FOR (c:WMCompany)
  ON (c.ticker);
