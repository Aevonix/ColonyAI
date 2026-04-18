"""World Model constants: entity types, relationship types, external ID keys."""

# ── Entity Types ──────────────────────────────────────────────────────────────

ENTITY_TYPES = frozenset({
    "person",
    "company",
    "project",
    "product",
    "location",
    "event",
    "concept",
})

# ── Relationship Types (WM_ prefixed for Neo4j namespace safety) ──────────────

RELATIONSHIP_TYPES = frozenset({
    "WM_WORKS_AT",
    "WM_FOUNDED",
    "WM_OWNS",
    "WM_INVESTED_IN",
    "WM_ADVISES",
    "WM_MANAGES",
    "WM_REPORTS_TO",
    "WM_KNOWS",
    "WM_PARTNER_OF",
    "WM_ACQUIRED",
    "WM_PART_OF",
    "WM_LOCATED_IN",
    "WM_MEMBER_OF",
    "WM_ATTENDED",
    "WM_ORGANIZED",
    "WM_PRODUCED",
    "WM_USES",
    "WM_RELATED_TO",
    "WM_TAGGED_WITH",
    "WM_MENTIONS",
    "WM_PREDECESSOR_OF",
    "WM_SUCCESSOR_OF",
    "WM_DEPENDS_ON",
})

# ── External ID Keys ──────────────────────────────────────────────────────────

EXTERNAL_ID_KEYS = {
    # People
    "linkedin": "LinkedIn profile URL or ID",
    "email": "Primary email address",
    "phone": "E.164 phone number",
    "twitter": "Twitter/X handle",

    # Companies
    "crunchbase": "Crunchbase organization slug",
    "linkedin_company": "LinkedIn company ID",
    "ticker": "Stock ticker symbol",
    "domain": "Primary web domain",
    "lei": "Legal Entity Identifier (ISO 17442)",

    # Events
    "calendar_uid": "iCal UID from calendar source",
    "eventbrite": "Eventbrite event ID",

    # Products
    "app_store_id": "Apple App Store bundle ID",
    "play_store_id": "Google Play package name",

    # Locations
    "geonames_id": "GeoNames feature ID",
    "osm_id": "OpenStreetMap node/way/relation ID",
}

# ── ID Prefixes ───────────────────────────────────────────────────────────────

ENTITY_ID_PREFIX = "we"         # world entity
RELATIONSHIP_ID_PREFIX = "wr"   # world relationship
OBSERVATION_ID_PREFIX = "wo"    # world observation
MERGE_PROPOSAL_ID_PREFIX = "mp" # merge proposal
MERGE_AUDIT_ID_PREFIX = "ma"    # merge audit record
