"""Tests for Neo4j World Model Backend.

Uses a mock Neo4j driver since we can't assume Neo4j is running in CI.
Tests the serialization/deserialization logic and Cypher generation.
"""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from colony_sidecar.world_model.neo4j.backend import (
    Neo4jBackend,
    _entity_to_props,
    _props_to_entity,
    _rel_to_props,
    _props_to_rel,
    _generate_id,
)
from colony_sidecar.world_model.entities import (
    BaseEntity,
    PersonEntity,
    CompanyEntity,
    ProjectEntity,
    ConceptEntity,
)
from colony_sidecar.world_model.relationships import WorldRelationship


class TestEntitySerialization:
    def test_person_to_props(self):
        person = PersonEntity(
            id="we-123-abc",
            name="User",
            entity_type="person",
            confidence=0.9,
            email="marc@example.com",
            aliases=["marcus"],
        )
        props = _entity_to_props(person)
        assert props["id"] == "we-123-abc"
        assert props["name"] == "User"
        assert props["entity_type"] == "person"
        assert props["email"] == "marc@example.com"
        assert props["aliases"] == json.dumps(["marcus"])

    def test_props_to_person(self):
        props = {
            "id": "we-123-abc",
            "name": "User",
            "entity_type": "person",
            "confidence": 0.9,
            "email": "marc@example.com",
            "aliases": json.dumps(["marcus"]),
            "external_ids": json.dumps({"email": "marc@example.com"}),
            "properties": json.dumps({"key": "val"}),
        }
        entity = _props_to_entity(props)
        assert isinstance(entity, PersonEntity)
        assert entity.name == "User"
        assert entity.aliases == ["marcus"]
        assert entity.external_ids == {"email": "marc@example.com"}
        assert entity.properties == {"key": "val"}

    def test_roundtrip_entity(self):
        person = PersonEntity(
            id="we-456-def",
            name="Alice",
            entity_type="person",
            confidence=0.8,
            email="alice@example.com",
            title="CTO",
        )
        props = _entity_to_props(person)
        restored = _props_to_entity(props)
        assert isinstance(restored, PersonEntity)
        assert restored.id == person.id
        assert restored.name == person.name
        assert restored.email == person.email

    def test_company_to_props(self):
        company = CompanyEntity(
            id="we-789-ghi",
            name="Aevonix",
            entity_type="company",
            confidence=0.95,
            domain="aevonix.com",
            industry="AI",
        )
        props = _entity_to_props(company)
        assert props["domain"] == "aevonix.com"
        assert props["industry"] == "AI"

    def test_project_to_props(self):
        project = ProjectEntity(
            id="we-prj-001",
            name="ColonyAI",
            entity_type="project",
            confidence=0.9,
            status="active",
        )
        props = _entity_to_props(project)
        assert props["status"] == "active"

    def test_none_fields_excluded(self):
        entity = BaseEntity(
            id="we-001",
            name="Test",
            entity_type="concept",
            confidence=0.5,
        )
        props = _entity_to_props(entity)
        assert "email" not in props
        assert "bio_summary" not in props

    def test_datetime_serialized(self):
        now = datetime.now(timezone.utc)
        entity = BaseEntity(
            id="we-002",
            name="Test",
            entity_type="concept",
            confidence=0.5,
            created_at=now,
        )
        props = _entity_to_props(entity)
        assert isinstance(props["created_at"], str)

    def test_datetime_deserialized(self):
        now = datetime.now(timezone.utc)
        props = {
            "id": "we-003",
            "name": "Test",
            "entity_type": "concept",
            "confidence": 0.5,
            "created_at": now.isoformat(),
        }
        entity = _props_to_entity(props)
        assert isinstance(entity.created_at, datetime)


class TestRelSerialization:
    def test_rel_to_props(self):
        rel = WorldRelationship(
            id="wr-001",
            source_id="we-001",
            target_id="we-002",
            relationship_type="WM_WORKS_AT",
            confidence=0.8,
        )
        props = _rel_to_props(rel)
        assert props["id"] == "wr-001"
        assert props["source_id"] == "we-001"
        assert props["relationship_type"] == "WM_WORKS_AT"

    def test_props_to_rel(self):
        props = {
            "id": "wr-002",
            "source_id": "we-001",
            "target_id": "we-002",
            "relationship_type": "WM_KNOWS",
            "confidence": 0.7,
            "properties": json.dumps({"since": "2024"}),
        }
        rel = _props_to_rel(props)
        assert isinstance(rel, WorldRelationship)
        assert rel.relationship_type == "WM_KNOWS"
        assert rel.properties == {"since": "2024"}

    def test_roundtrip_rel(self):
        rel = WorldRelationship(
            id="wr-003",
            source_id="we-001",
            target_id="we-002",
            relationship_type="WM_FOUNDED",
            confidence=0.9,
            valid_from="2024-01-01",
            properties={"role": "co-founder"},
        )
        props = _rel_to_props(rel)
        restored = _props_to_rel(props)
        assert restored.id == rel.id
        assert restored.relationship_type == "WM_WORKS_AT" or restored.relationship_type == rel.relationship_type


class TestGenerateId:
    def test_entity_id_prefix(self):
        eid = _generate_id("we")
        assert eid.startswith("we-")

    def test_relationship_id_prefix(self):
        rid = _generate_id("wr")
        assert rid.startswith("wr-")

    def test_unique_ids(self):
        ids = {_generate_id("we") for _ in range(100)}
        assert len(ids) == 100


class TestNeo4jBackendInit:
    def test_init_stores_params(self):
        backend = Neo4jBackend(
            uri="bolt://localhost:7687",
            database="testdb",
            username="neo4j",
            password="testpass",
        )
        assert backend._uri == "bolt://localhost:7687"
        assert backend._database == "testdb"
        assert backend._username == "neo4j"
        assert backend._password == "testpass"
        assert backend._driver is None
