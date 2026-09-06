"""The existing timer ranks and persists work without waking legacy effects."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from colony_sidecar.autonomy.config import AutonomyConfig, AutonomyMode
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.intelligence.components.initiative_engine import Initiative, InitiativeType
from colony_sidecar.self_model.perspective import SelfPerspective
from colony_sidecar.self_model.store import CompetenceStore, SelfModel
from colony_sidecar.turns import TurnIdempotencyLedger


def test_selected_configuration_is_explicit_and_rejects_misspellings(monkeypatch):
    monkeypatch.setenv('COLONY_AUTONOMY_PHASES', 'initiative, execute,telemetry')
    monkeypatch.setenv('COLONY_AUTONOMY_PROPOSALS_ONLY', 'true')
    config = AutonomyConfig.from_env()
    assert config.enabled_phases == ('initiative', 'execute', 'telemetry')
    assert config.proposals_only is True
    same = AutonomyConfig.from_colony_config({'autonomy': {
        'enabled_phases': ['initiative', 'execute'], 'proposals_only': True}})
    assert same.enabled_phases == ('initiative', 'execute') and same.proposals_only
    for invalid in ('', 'initiative,,execute', 'intitiative'):
        monkeypatch.setenv('COLONY_AUTONOMY_PHASES', invalid)
        with pytest.raises(ValueError):
            AutonomyLoop(SimpleNamespace(), AutonomyConfig.from_env())


@pytest.mark.asyncio
async def test_real_timer_persists_ranked_proposals_and_restarts_without_dispatch(tmp_path, monkeypatch):
    from colony_sidecar.identity import resolver
    monkeypatch.setattr(resolver, 'get_identity_resolver', lambda: SimpleNamespace(
        owner_identities=AsyncMock(return_value=['fixture-owner'])))
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    state = SelfPerspective(ledger, owner_id='fixture-owner')
    sm = SelfModel(CompetenceStore(str(tmp_path / 'competence.db')))
    sm.perspective = state
    store = InitiativeStore(tmp_path / 'work')
    items = [Initiative(id='candidate-' + kind, type=InitiativeType(kind),
        description='Neutral ' + kind + ' work', priority=priority,
        rationale='Current observed task', dedup_key='selected-' + kind)
        for kind, priority in [('operational', .95), ('agent_action', .9), ('research', .8)]]
    engine = SimpleNamespace(clear_context=lambda: None, _context={},
        generate=AsyncMock(return_value=items), execute_initiative=AsyncMock())
    delivery = SimpleNamespace(push_initiative=AsyncMock())
    registry = SimpleNamespace(initiative_engine=engine, initiative_store=store,
                               self_model=sm, delivery=delivery)
    config = AutonomyConfig(mode=AutonomyMode.PROACTIVE, enabled_phases=('initiative', 'execute'),
        proposals_only=True, tick_interval_secs=.01, quiet_hours_start='00:00', quiet_hours_end='00:00')

    async def run_two_ticks():
        loop = AutonomyLoop(registry, config)
        for name in ('_feed_pending_tasks', '_feed_neglected_contacts',
                     '_feed_commitment_reminders', '_feed_introduction_candidates'):
            setattr(loop, name, AsyncMock())
        loop._phase_scheduled = AsyncMock(side_effect=AssertionError('Unselected maintenance ran'))
        loop._post_agent_action_to_queue = AsyncMock(side_effect=AssertionError('Queued proposal'))
        loop._route_reachout_delivery = AsyncMock(side_effect=AssertionError('Delivered proposal'))
        loop._check_phase_capabilities = lambda: None
        actual_tick = loop._tick
        async def tick():
            await actual_tick()
            if loop.stats.ticks == 2:
                await loop.stop()
        loop._tick = tick
        await asyncio.wait_for(loop.start(), timeout=5)
        assert loop.status()['config']['proposals_only']
        assert loop.stats.ticks == 2 and loop.stats.actions_executed == 0
        assert set(loop.phase_timings()['seconds_last']) == {'initiative', 'execute'}
        loop._phase_scheduled.assert_not_awaited()
        loop._post_agent_action_to_queue.assert_not_awaited()
        return loop

    await run_two_ticks()
    engine.execute_initiative.assert_not_awaited()
    rows = store.list(status=['pending'])
    assert len(rows) == 3
    assert {row.context['candidate_id'] for row in rows} == {item.id for item in items}
    snapshot = SelfPerspective(TurnIdempotencyLedger(ledger.db_path), owner_id='fixture-owner').status()['attention']
    assert snapshot['ordered_ids'] == [item.id for item in items]
    assert all(row['description'].startswith('Neutral ') for row in snapshot['decisions'])
    assert snapshot['authority_changed'] is False
    before = {row.id for row in rows}
    registry.initiative_store = InitiativeStore(tmp_path / 'work')
    await run_two_ticks()
    assert {row.id for row in registry.initiative_store.list(status=['pending'])} == before
    engine.execute_initiative.assert_not_awaited()
