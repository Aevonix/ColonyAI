"""Initiative pipeline v0.16.0 — Phase 1 bug fixes + Phase 2 foundations.

Covers:
- Bug 1/2/3: serializer returns entity_id, persisted context, action title
- Bug 4: owner exclusion via IdentityResolver, scoped to relationship
  generators, fail-closed when the owner is unresolvable
- Context persistence migration (old rows return {} without erroring)
- Dedup regression (two neglected contacts → two distinct keys)
- COMMITMENT generator (owner is a valid subject)
- Action registry gating (unregistered never executes; mutating/outbound
  blocks on human approval; read_only auto-runs)
- Context durability/freshness rules
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from colony_sidecar.identity.resolver import (
    IdentityResolver,
    OwnerIdentityError,
    get_identity_resolver,
    get_owner_contact_id,
    reset_identity_resolver,
)
from colony_sidecar.initiatives.action_registry import (
    RiskTier,
    classify_agent_action,
    get_action,
    requires_owner_approval,
)
from colony_sidecar.initiatives.context_freshness import (
    durability_for,
    freshness_ttl_for,
    is_context_fresh,
)
from colony_sidecar.initiatives.models import StoredInitiative
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.intelligence.components.initiative_engine import (
    InitiativeConfig,
    InitiativeEngine,
    InitiativeType,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeContact(SimpleNamespace):
    pass


def _contact(
    cid: str,
    display_name: str = None,
    person_node_id: str = None,
    given_name: str = None,
    family_name: str = None,
) -> FakeContact:
    return FakeContact(
        contact_id=cid,
        display_name=display_name,
        given_name=given_name,
        family_name=family_name,
        person_node_id=person_node_id,
    )


class FakeContactStore:
    """Async contact store with the lookup surface the resolver uses."""

    def __init__(self, contacts=None, handles=None):
        self._contacts = contacts or []
        self._handles = handles or {}  # contact_id -> [(gateway, address)]

    async def get(self, contact_id):
        for c in self._contacts:
            if c.contact_id == contact_id:
                return c
        return None

    async def resolve_handle(self, gateway, address):
        for cid, pairs in self._handles.items():
            for gw, addr in pairs:
                if gw == gateway and addr == address:
                    return await self.get(cid)
        return None

    async def find_by_person_node_id(self, person_node_id):
        for c in self._contacts:
            if c.person_node_id == person_node_id:
                return c
        return None

    async def find_by_name(self, name, threshold=0.5):
        wanted = name.strip().lower()
        return [
            c for c in self._contacts
            if c.display_name and wanted in c.display_name.lower()
        ]

    async def get_handles(self, contact_id):
        return [
            SimpleNamespace(gateway=gw, address=addr)
            for gw, addr in self._handles.get(contact_id, [])
        ]


class FakeResult:
    def __init__(self, single=None):
        self._single = single

    async def single(self):
        return self._single


class FakeGraphSession:
    """Async session that answers the MANAGES-gate count query."""

    def __init__(self, manages_count=1):
        self._manages_count = manages_count

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def run(self, query, **params):
        return FakeResult(single={"cnt": self._manages_count})


class FakeGraphClient:
    def __init__(self, manages_count=1):
        self.database = "neo4j"
        self.driver = MagicMock()
        self.driver.session = lambda **kw: FakeGraphSession(manages_count)


@pytest.fixture
def owner_resolver(monkeypatch):
    """Resolver singleton with the owner resolvable in three formats."""
    reset_identity_resolver()
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-1")
    monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
    store = FakeContactStore(
        contacts=[
            _contact(
                "cid-owner-1",
                display_name="Jane Doe",
                person_node_id="uuid-owner",
                given_name="Marc",
                family_name="Seal",
            ),
            _contact(
                "cid-bob-2",
                display_name="Bob Jones",
                person_node_id="uuid-bob",
            ),
        ],
        handles={"cid-owner-1": [("email", "marc@example.com")]},
    )
    resolver = get_identity_resolver(contact_store=store)
    yield resolver
    reset_identity_resolver()


@pytest.fixture
def store(tmp_path: Path) -> InitiativeStore:
    return InitiativeStore(state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Bug 2/3: context persistence
# ---------------------------------------------------------------------------

class TestContextPersistence:
    def test_context_round_trips_through_store(self, store):
        ctx = {
            "neglected_contact": {
                "contact_name": "Jordan Example",
                "days_since_contact": 14,
            },
            "rationale": "No contact for 14 days",
        }
        created = store.create(
            type="relationship",
            description="Check in with Jordan Example",
            priority=0.7,
            entity_id="uuid-bradley",
            context=ctx,
        )
        fetched = store.get(created.id)
        assert fetched.context == ctx
        assert fetched.entity_id == "uuid-bradley"

    def test_context_updatable(self, store):
        created = store.create(type="coding", description="CI check", context={"a": 1})
        updated = store.update(created.id, context={"b": 2})
        assert updated.context == {"b": 2}

    def test_old_rows_without_context_column_migrate(self, tmp_path):
        # Simulate a pre-v0.16.0 database: table without the context column.
        db_path = tmp_path / "initiatives.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE initiatives (
                id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE, type TEXT NOT NULL,
                description TEXT NOT NULL, priority REAL DEFAULT 0.5,
                rationale TEXT, action_hint TEXT, entity_id TEXT,
                source_type TEXT, source_id TEXT, created_by TEXT,
                status TEXT DEFAULT 'pending', assigned_agent_id TEXT,
                assigned_agent_name TEXT, assigned_at TIMESTAMP,
                acknowledged_at TIMESTAMP, completed_at TIMESTAMP,
                cancelled_at TIMESTAMP, cancelled_by TEXT, cancelled_reason TEXT,
                failed_at TIMESTAMP, failed_reason TEXT,
                attempt_count INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
                timeout_seconds INTEGER DEFAULT 300, last_attempt_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP,
                delivery_mode TEXT DEFAULT 'websocket',
                delivery_attempts INTEGER DEFAULT 0, last_delivery_at TIMESTAMP,
                delivery_failed_at TIMESTAMP, delivery_failed_reason TEXT,
                result TEXT, result_metadata TEXT DEFAULT '{}',
                preferred_agent_id TEXT, stale_reason TEXT, recovery_reason TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO initiatives (id, type, description) VALUES (?, ?, ?)",
            ["old-row-1", "relationship", "Check in with someone"],
        )
        conn.commit()
        conn.close()

        migrated = InitiativeStore(state_dir=tmp_path)
        old = migrated.get("old-row-1")
        assert old is not None
        assert old.context is None  # NULL pre-migration row

        # New rows can persist context in the migrated table
        new = migrated.create(type="relationship", description="x", context={"k": "v"})
        assert migrated.get(new.id).context == {"k": "v"}


# ---------------------------------------------------------------------------
# Bug 1/3: API serializer
# ---------------------------------------------------------------------------

class TestInitiativeSerializer:
    def _stored(self, **overrides):
        base = dict(
            id="rel-uuid-bradley",
            type="relationship",
            description="Check in with Jordan Example",
            priority=0.72,
            rationale="No contact for 14 days",
            entity_id="uuid-bradley",
            context={
                "neglected_contact": {"days_since_contact": 14},
                "rationale": "No contact for 14 days",
            },
        )
        base.update(overrides)
        return StoredInitiative(**base)

    def test_title_is_action_not_reason(self):
        from colony_sidecar.api.routers.host import _initiative_to_response

        resp = _initiative_to_response(self._stored())
        assert resp.title == "Check in with Jordan Example"
        assert resp.title != "No contact for 14 days"

    def test_entity_id_and_context_returned(self):
        from colony_sidecar.api.routers.host import _initiative_to_response

        resp = _initiative_to_response(self._stored())
        assert resp.entity_id == "uuid-bradley"
        assert resp.context["neglected_contact"]["days_since_contact"] == 14
        assert resp.context["rationale"] == "No contact for 14 days"
        assert resp.context_durability == "durable"

    def test_null_context_returns_empty_dict(self):
        from colony_sidecar.api.routers.host import _initiative_to_response

        resp = _initiative_to_response(self._stored(context=None))
        assert resp.context == {}

    def test_target_agent_id_populated(self):
        from colony_sidecar.api.routers.host import _initiative_to_response

        assigned = self._stored(assigned_agent_id="aeva", status="assigned")
        assert _initiative_to_response(assigned).target_agent_id == "aeva"

        preferred = self._stored(preferred_agent_id="macmini")
        assert _initiative_to_response(preferred).target_agent_id == "macmini"

        unset = self._stored()
        assert _initiative_to_response(unset).target_agent_id is None

    def test_assigned_status_serializes(self):
        # The status Literal was missing "assigned" — a store status the
        # loop sets when linking initiatives to queue jobs.
        from colony_sidecar.api.routers.host import _initiative_to_response

        resp = _initiative_to_response(self._stored(status="assigned"))
        assert resp.status == "assigned"


# ---------------------------------------------------------------------------
# Bug 4: IdentityResolver + owner exclusion
# ---------------------------------------------------------------------------

class TestIdentityResolver:
    @pytest.mark.asyncio
    async def test_resolves_all_formats_to_same_identity(self, owner_resolver):
        by_cid = await owner_resolver.resolve("cid-owner-1")
        assert {"cid-owner-1", "Jane Doe", "uuid-owner", "marc@example.com"} <= by_cid

        by_node = await owner_resolver.resolve("uuid-owner")
        assert "cid-owner-1" in by_node

        by_name = await owner_resolver.resolve("Jane Doe")
        assert "cid-owner-1" in by_name

        by_email = await owner_resolver.resolve("marc@example.com")
        assert "cid-owner-1" in by_email

    @pytest.mark.asyncio
    async def test_is_owner_across_formats(self, owner_resolver):
        assert await owner_resolver.is_owner("cid-owner-1")
        assert await owner_resolver.is_owner("uuid-owner")
        assert await owner_resolver.is_owner("Jane Doe")
        assert await owner_resolver.is_owner("jane doe")
        assert await owner_resolver.is_owner("marc@example.com")
        assert not await owner_resolver.is_owner("uuid-bob")
        assert not await owner_resolver.is_owner("Bob Jones")
        assert not await owner_resolver.is_owner(None)

    @pytest.mark.asyncio
    async def test_ambiguous_name_returns_empty_set(self):
        store = FakeContactStore(contacts=[
            _contact("cid-a", display_name="John Smith"),
            _contact("cid-b", display_name="John Smith"),
        ])
        resolver = IdentityResolver(contact_store=store, owner_id="cid-a")
        assert await resolver.resolve("John Smith") == set()

    @pytest.mark.asyncio
    async def test_missing_owner_config_raises(self, monkeypatch):
        monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
        monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
        resolver = IdentityResolver(contact_store=FakeContactStore())
        with pytest.raises(OwnerIdentityError):
            await resolver.owner_identities()

    @pytest.mark.asyncio
    async def test_unresolvable_owner_raises_no_silent_default(self):
        # The old code defaulted to the string "owner", which never matched
        # anything — the owner passed the filter every time. Never again.
        resolver = IdentityResolver(
            contact_store=FakeContactStore(), owner_id="cid-ghost",
        )
        with pytest.raises(OwnerIdentityError):
            await resolver.owner_identities()

    @pytest.mark.asyncio
    async def test_no_contact_store_falls_back_to_exact_match(self):
        resolver = IdentityResolver(contact_store=None, owner_id="cid-owner-1")
        owners = await resolver.owner_identities()
        assert "cid-owner-1" in owners
        assert await resolver.is_owner("cid-owner-1")
        assert not await resolver.is_owner("somebody-else")

    def test_legacy_env_var_shim(self, monkeypatch):
        monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
        monkeypatch.setenv("COLONY_HOST_CONTACT_ID", "cid-legacy")
        assert get_owner_contact_id() == "cid-legacy"

        monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-canonical")
        assert get_owner_contact_id() == "cid-canonical"


class TestOwnerExclusion:
    @pytest.fixture
    def engine(self, owner_resolver):
        return InitiativeEngine(
            graph_client=FakeGraphClient(manages_count=1),
            event_bus=None,
            mind_model=None,
            config=InitiativeConfig(),
        )

    @pytest.mark.asyncio
    async def test_owner_excluded_from_relationship_initiatives(self, engine):
        engine.add_context("neglected_contacts", [
            {"entity_id": "uuid-owner", "name": "Jane Doe", "days_since_contact": 30},
            {"entity_id": "uuid-bob", "name": "Bob Jones", "days_since_contact": 30},
        ])
        initiatives = await engine._generate_relationship_suggestions()
        subjects = {i.entity_id for i in initiatives}
        assert "uuid-owner" not in subjects
        assert "uuid-bob" in subjects  # non-owner neglected contact survives

    @pytest.mark.asyncio
    async def test_owner_excluded_by_display_name(self, engine):
        engine.add_context("neglected_contacts", [
            {"entity_id": "some-node", "name": "Jane Doe", "days_since_contact": 30},
        ])
        initiatives = await engine._generate_relationship_suggestions()
        assert initiatives == []

    @pytest.mark.asyncio
    async def test_unresolvable_owner_fails_closed(self, monkeypatch):
        # No owner identity → generate NOTHING rather than risk targeting
        # the owner (the old behavior generated everything).
        reset_identity_resolver()
        monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
        monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
        engine = InitiativeEngine(
            graph_client=FakeGraphClient(manages_count=1),
            event_bus=None,
            mind_model=None,
            config=InitiativeConfig(),
        )
        engine.add_context("neglected_contacts", [
            {"entity_id": "uuid-bob", "name": "Bob Jones", "days_since_contact": 30},
        ])
        initiatives = await engine._generate_relationship_suggestions()
        assert initiatives == []
        reset_identity_resolver()

    @pytest.mark.asyncio
    async def test_owner_is_valid_subject_for_commitments(self, engine):
        # Owner exclusion is a relationship-domain policy, NOT a global
        # gate: "follow up on what you promised Marc" must survive.
        engine.add_context("upcoming_commitments", [
            {
                "commitment_id": "cmt-1",
                "description": "Send Marc the report",
                "due_at": "2026-06-10T12:00:00+00:00",
                "hours_until_due": 20.0,
                "overdue": False,
                "status": "pending",
                "person_id": "cid-owner-1",
            },
        ])
        initiatives = await engine._generate_commitment_initiatives()
        assert len(initiatives) == 1
        assert initiatives[0].trigger_data["person_id"] == "cid-owner-1"


# ---------------------------------------------------------------------------
# Dedup regression
# ---------------------------------------------------------------------------

class TestDedupKeying:
    @pytest.mark.asyncio
    async def test_two_neglected_contacts_two_dedup_keys(self, owner_resolver):
        engine = InitiativeEngine(
            graph_client=FakeGraphClient(manages_count=1),
            event_bus=None,
            mind_model=None,
            config=InitiativeConfig(),
        )
        engine.add_context("neglected_contacts", [
            {"entity_id": "uuid-bob", "name": "Bob Jones", "days_since_contact": 14},
            {"entity_id": "uuid-carol", "name": "Carol White", "days_since_contact": 21},
        ])
        initiatives = await engine._generate_relationship_suggestions()
        keys = {i.dedup_key for i in initiatives}
        assert keys == {"relationship:uuid-bob", "relationship:uuid-carol"}


# ---------------------------------------------------------------------------
# COMMITMENT generator (Phase 2, Task 5)
# ---------------------------------------------------------------------------

class TestCommitmentGenerator:
    @pytest.fixture
    def engine(self):
        return InitiativeEngine(
            graph_client=None,
            event_bus=None,
            mind_model=None,
            config=InitiativeConfig(),
        )

    @pytest.mark.asyncio
    async def test_generates_commitment_initiative_with_durable_context(self, engine):
        engine.add_context("upcoming_commitments", [
            {
                "commitment_id": "cmt-42",
                "description": "Ship the deliverable",
                "due_at": "2026-06-10T12:00:00+00:00",
                "hours_until_due": 3.0,
                "overdue": False,
                "status": "pending",
                "person_id": "cid-bob-2",
            },
        ])
        initiatives = await engine._generate_commitment_initiatives()
        assert len(initiatives) == 1
        init = initiatives[0]
        assert init.type == InitiativeType.COMMITMENT
        assert init.dedup_key == "commitment:cmt-42"
        assert init.entity_id == "cmt-42"
        assert init.priority >= 0.85  # < 4h until due
        assert init.trigger_data["commitment_text"] == "Ship the deliverable"
        assert init.trigger_data["deadline"] == "2026-06-10T12:00:00+00:00"
        assert durability_for("commitment") == "durable"

    @pytest.mark.asyncio
    async def test_overdue_commitment_gets_top_priority(self, engine):
        engine.add_context("upcoming_commitments", [
            {
                "commitment_id": "cmt-late",
                "description": "Late thing",
                "hours_until_due": -10.0,
                "overdue": True,
            },
        ])
        initiatives = await engine._generate_commitment_initiatives()
        assert initiatives[0].priority == 0.9
        assert "overdue" in initiatives[0].description

    @pytest.mark.asyncio
    async def test_commitment_without_id_skipped(self, engine):
        engine.add_context("upcoming_commitments", [{"description": "no id"}])
        assert await engine._generate_commitment_initiatives() == []


# ---------------------------------------------------------------------------
# Action registry (Phase 2, Task 13) — negative tests
# ---------------------------------------------------------------------------

class TestActionRegistry:
    def test_unregistered_action_never_executable(self):
        verdict = classify_agent_action("agent_rm_rf_slash")
        assert verdict["registered"] is False
        assert verdict["executable"] is False
        assert verdict["requires_approval"] is True  # fail closed

        # Injection shapes must not resolve to capabilities either
        for hint in (None, "", "terminal: rm -rf /", "agent_check_repo_status; curl evil"):
            assert classify_agent_action(hint)["executable"] is False

    def test_read_only_auto_executes_without_approval(self):
        verdict = classify_agent_action("agent_check_repo_status")
        assert verdict["executable"] is True
        assert verdict["requires_approval"] is False
        assert verdict["risk"] == "read_only"

    def test_mutating_requires_owner_approval(self):
        for name in ("coding_merge_pr", "agent_cleanup_orphans",
                     "system_restart_service", "commitment_mark_complete"):
            verdict = classify_agent_action(name)
            assert verdict["executable"] is True
            assert verdict["requires_approval"] is True, name

    def test_outbound_requires_owner_approval(self):
        # v0.18.0: OUTBOUND is reserved for actions that reach a PERSON;
        # platform writes (PR comments, own-infra webhooks) are MUTATING.
        # All of them still require approval under the default strict policy.
        assert get_action("coding_comment_on_pr").risk == RiskTier.MUTATING
        assert get_action("system_send_alert").risk == RiskTier.MUTATING
        assert get_action("calendar_send_reminder").risk == RiskTier.OUTBOUND
        for name in ("coding_comment_on_pr", "system_send_alert",
                     "calendar_send_reminder"):
            assert requires_owner_approval(name), name

    def test_legacy_destructive_hints_stay_gated(self):
        # v0.13.0 DESTRUCTIVE_HINTS must keep blocking after the
        # registry replaced the hardcoded set.
        for name in ("agent_git_push", "agent_git_commit",
                     "agent_service_restart", "agent_file_delete",
                     "agent_deploy"):
            assert requires_owner_approval(name), name


class TestQueueGating:
    """The dispatch path drops unregistered hints before the queue."""

    def _loop(self, task_queue):
        from colony_sidecar.autonomy.config import AutonomyConfig
        from colony_sidecar.autonomy.loop import AutonomyLoop

        registry = MagicMock()
        registry.task_queue = task_queue
        registry.initiative_store = None
        return AutonomyLoop(registry=registry, config=AutonomyConfig())

    @pytest.mark.asyncio
    async def test_unregistered_hint_never_reaches_queue(self):
        task_queue = MagicMock()
        loop = self._loop(task_queue)
        initiative = SimpleNamespace(
            description="do something", priority=0.5, entity_id="x",
            rationale="", trigger_data=None,
        )
        await loop._post_agent_action_to_queue(
            initiative, "init-1", "agent_action", "agent_not_a_real_action",
        )
        task_queue.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Context durability / freshness
# ---------------------------------------------------------------------------

class TestContextFreshness:
    def test_durability_declarations(self):
        assert durability_for("relationship") == "durable"
        assert durability_for("commitment") == "durable"
        assert durability_for("task") == "durable"
        assert durability_for("calendar") == "volatile"
        assert durability_for("coding") == "volatile"
        assert durability_for("system") == "volatile"
        assert durability_for("agent_action") == "volatile"

    def test_durable_context_always_fresh(self):
        assert is_context_fresh("relationship", None)
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert is_context_fresh("commitment", old)
        assert freshness_ttl_for("relationship") is None

    def test_volatile_context_expires(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(seconds=60)).isoformat()
        stale = (now - timedelta(seconds=3600)).isoformat()
        assert is_context_fresh("calendar", recent)
        assert not is_context_fresh("calendar", stale)
        # No capture stamp → stale (fail closed)
        assert not is_context_fresh("system", None)
        assert not is_context_fresh("system", "not-a-date")
