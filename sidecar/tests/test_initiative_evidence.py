"""Old or unclassified health snapshots cannot become new failure claims."""
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from colony_sidecar.autonomy.config import AutonomyConfig, AutonomyMode
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.initiatives.context_freshness import is_context_fresh
from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.intelligence.components.initiative_engine import InitiativeEngine, InitiativeType
from colony_sidecar.observations.store import ObservationStore
from colony_sidecar.self_model.perspective import SelfPerspective
from colony_sidecar.self_model.store import CompetenceStore, SelfModel
from colony_sidecar.turns import TurnIdempotencyLedger


@pytest.mark.asyncio
async def test_actual_observation_generation_ranking_and_durable_proposal_preserve_evidence(tmp_path):
    observations=ObservationStore(tmp_path/'observations')
    now=datetime.now(timezone.utc); stamp=now-timedelta(seconds=45)
    observations.record('system','old-failure',{'status':'down','observed_at':now.isoformat()},observed_at=now-timedelta(days=30))
    observations.record('system','unknown',{'note':'Historical completed work'})
    observations.record('system','healthy',{'status':'healthy','error_rate':0.0})
    observations.record('system','restart-outcome',{'status':'restarted'})
    observations.record('system','broken-metric',{'error_rate':'unavailable'})
    observations.record('system','future',{'status':'down'},observed_at=now+timedelta(hours=1))
    observations.record('system','current-failure',{'status':'down','entity_id':'spoofed','context_captured_at':now.isoformat()},observed_at=stamp)
    engine=InitiativeEngine(None,None,None,observation_store=observations)
    engine._load_graph_context=AsyncMock()  # No hardware/graph probes in this fixture.
    actual_generate=engine.generate
    async def only_system(**kwargs):
        return await actual_generate(types=[InitiativeType.SYSTEM],**kwargs)
    engine.generate=only_system
    state=SelfPerspective(TurnIdempotencyLedger(tmp_path/'sources.db'),owner_id='neutral-owner')
    sm=SelfModel(CompetenceStore(str(tmp_path/'competence.db')));sm.perspective=state
    store=InitiativeStore(tmp_path/'work')
    loop=AutonomyLoop(SimpleNamespace(initiative_engine=engine,initiative_store=store,self_model=sm,delivery=None),
        AutonomyConfig(mode=AutonomyMode.PROACTIVE,enabled_phases=('initiative','execute'),
            proposals_only=True,max_actions_per_hour=0,quiet_hours_start='00:00',quiet_hours_end='00:00'))
    for name in ('_feed_pending_tasks','_feed_neglected_contacts','_feed_commitment_reminders','_feed_introduction_candidates'):
        setattr(loop,name,AsyncMock())
    await loop._phase_initiative();await loop._phase_execute()
    rows=InitiativeStore(tmp_path/'work').list(status=['pending'])
    assert len(rows)==1 and rows[0].entity_id=='current-failure'
    assert rows[0].context['observed_at']==rows[0].context['context_captured_at']==stamp.isoformat()
    assert state.status()['attention']['ordered_ids']==['system-current-failure']
    assert not is_context_fresh('system',rows[0].context['context_captured_at'],now=now+timedelta(minutes=5))
    # A refresh of a stored month-old snapshot does not certify it as current.
    assert await engine.rebuild_context('system','old-failure') is None
    observations.record('system','current-failure',{'status':'healthy'})
    assert (await engine.rebuild_context('system','current-failure'))['condition_cleared'] is True


@pytest.mark.asyncio
async def test_only_explicit_failure_or_valid_high_error_rate_can_trigger():
    engine=InitiativeEngine(None,None,None)
    stamp=datetime.now(timezone.utc).isoformat()
    engine.add_context('system',[
        {'entity_id':'high-rate','error_rate':0.4,'observed_at':stamp},
        {'entity_id':'unknown-status','status':'unknown','observed_at':stamp},
        {'entity_id':'invalid-rate','error_rate':float('nan'),'observed_at':stamp},
        {'entity_id':'no-time','status':'down'},
    ])
    rows=await engine._generate_system_initiatives()
    assert len(rows)==1 and rows[0].entity_id=='high-rate'
    assert 'elevated error rate' in rows[0].description


@pytest.mark.asyncio
async def test_invalid_stored_or_supplied_time_never_becomes_fresh(tmp_path):
    store=ObservationStore(tmp_path)
    assert store.record_batch('system',[{'entity_id':'bad-input','payload':{'status':'down'},'observed_at':'invalid'}])==0
    store.record('system','old-row',{'status':'down'})
    store._db.execute("UPDATE observations SET observed_at='invalid' WHERE entity_id='old-row'");store._db.commit()
    assert store.get('system','old-row').to_dict()['observed_at'] is None
    engine=InitiativeEngine(None,None,None,observation_store=store)
    engine._load_observation_domains()
    assert await engine._generate_system_initiatives()==[]


@pytest.mark.asyncio
async def test_backup_proposal_describes_only_the_observed_legacy_file(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    backup=tmp_path/'.colony/backups/old.bak';backup.parent.mkdir(parents=True);backup.write_text('unverified checkpoint')
    stamp=(datetime.now(timezone.utc)-timedelta(days=27)).timestamp();os.utime(backup,(stamp,stamp))
    engine=InitiativeEngine(None,None,None)
    await engine._load_operational_tasks()
    rows=await engine._generate_operational_initiatives()
    assert len(rows)==1 and 'legacy .bak' in rows[0].description
    assert 'Last backup' not in rows[0].description and 'unverified' in rows[0].description
    assert rows[0].trigger_data['evidence_scope']=='legacy_bak_directory_only'
    assert rows[0].trigger_data['latest_file_modified_at']==datetime.fromtimestamp(stamp,timezone.utc).isoformat()
