"""Acceptance survives restart without pretending an unbound queue executes."""
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host
from colony_sidecar.goals.config import GoalEngineConfig
from colony_sidecar.goals.engine import GoalEngine
from colony_sidecar.goals.models import Goal, GoalDAG, GoalStatus, Subtask, SubtaskStatus
from colony_sidecar.goals.queue_bridge import GoalQueueBridge, InMemoryQueueBackend


async def test_api_acceptance_survives_restart_without_fake_dispatch(tmp_path, monkeypatch):
    config = GoalEngineConfig(db_path=str(tmp_path / 'goals.db'), inference_enabled=False)
    engine = GoalEngine(config=config)
    monkeypatch.setattr(host, '_goals_store', engine)
    app = FastAPI()
    app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/host/goals', json={
            'identity': {'host_id': 'test'}, 'title': 'Compare saved project proposals',
        })
        assert response.status_code == 200
        created = response.json()
        assert created['status'] == 'accepted'
        assert created['dispatch_unavailable'] == 'queue_backend_unconfigured'
        goal_id = created['id']
        assert engine.get_dag(goal_id) is None
        assert [t.to_status for t in engine.get_audit_trail(goal_id)] == ['accepted']

        # Reopen the same durable store as a replacement process would. Reading
        # and retrying activation must not mint discarded in-memory jobs.
        replacement = GoalEngine(config=config)
        monkeypatch.setattr(host, '_goals_store', replacement)
        pending = replacement.activate_goal(goal_id)
        assert pending.status == GoalStatus.ACCEPTED
        assert pending.context['dispatch_unavailable'] == 'queue_backend_unconfigured'
        assert replacement.get_dag(goal_id) is None
        assert (await client.get('/v1/host/goals/' + goal_id)).json()['status'] == 'accepted'
        assert (await client.get('/v1/host/goals')).json()['goals'][0]['status'] == 'accepted'
        assert len(replacement.get_audit_trail(goal_id)) == 1


async def test_reported_completion_closes_accepted_goal_and_survives_replay(tmp_path, monkeypatch):
    config = GoalEngineConfig(db_path=str(tmp_path / 'goals.db'), inference_enabled=False)
    engine = GoalEngine(config=config)
    monkeypatch.setattr(host, '_goals_store', engine)
    app = FastAPI()
    app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        created = (await client.post('/v1/host/goals', json={
            'identity': {'host_id': 'test'}, 'title': 'Compare saved proposals',
        })).json()
        url = '/v1/host/goals/' + created['id']
        result = await client.patch(url, json={'identity': {'host_id': 'test'}, 'status': 'done'})
        assert result.status_code == 200
        completed = result.json()
        assert completed['status'] == 'completed'
        assert completed['progress'] == 1.0
        assert completed['dispatch_unavailable'] is None
        assert completed['completion_basis'] == 'reported_completion'
        assert engine.get_dag(created['id']) is None
        transitions = engine.get_audit_trail(created['id'])
        assert [(t.from_status, t.to_status, t.trigger) for t in transitions] == [
            ('proposed', 'accepted', 'user_accepted'),
            ('accepted', 'completed', 'reported_completion'),
        ]
        engine._store.close()
        replacement = GoalEngine(config=config)
        monkeypatch.setattr(host, '_goals_store', replacement)
        replay = await client.patch(url, json={'identity': {'host_id': 'test'}, 'status': 'completed'})
        assert replay.json() == completed
        assert (await client.get(url)).json() == completed
        assert (await client.get('/v1/host/goals')).json()['goals'] == [completed]
        assert len(replacement.get_audit_trail(created['id'])) == 2


async def test_completion_does_not_resurrect_abandoned_or_create_missing_goal(tmp_path, monkeypatch):
    engine = GoalEngine(config=GoalEngineConfig(db_path=str(tmp_path / 'goals.db')))
    monkeypatch.setattr(host, '_goals_store', engine)
    goal = engine.propose_goal('Cancelled undertaking')
    engine.abandon_goal(goal.goal_id, 'No longer wanted')
    app = FastAPI()
    app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        for goal_id, expected in ((goal.goal_id, 409), ('absent-goal', 404)):
            response = await client.patch('/v1/host/goals/' + goal_id, json={
                'identity': {'host_id': 'test'}, 'status': 'done'})
            assert response.status_code == expected
    assert engine.get_goal(goal.goal_id).status == GoalStatus.ABANDONED
    assert len(engine.get_audit_trail(goal.goal_id)) == 1


def test_reported_completion_and_transition_roll_back_together(tmp_path):
    engine = GoalEngine(config=GoalEngineConfig(db_path=str(tmp_path / 'goals.db')))
    goal = engine.propose_goal('Retain outcome if audit write fails')
    engine.accept_goal(goal.goal_id)
    conn = engine._store._get_conn()
    conn.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON goal_audit_log
        WHEN NEW.trigger='reported_completion' BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
    conn.commit()
    import pytest
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match='simulated storage failure'):
        engine.complete_task(goal.goal_id)
    retained = engine.get_goal(goal.goal_id)
    assert retained.status == GoalStatus.ACCEPTED
    assert retained.completed_at is None
    assert len(engine.get_audit_trail(goal.goal_id)) == 1


def test_unbound_bridge_leaves_subtasks_pending():
    goal = Goal(title='Local draft')
    subtask = Subtask(goal_id=goal.goal_id)
    dag = GoalDAG(goal_id=goal.goal_id)
    dag.add_subtask(subtask)
    bridge = GoalQueueBridge()
    assert not bridge.available
    assert bridge.dispatch_ready_subtasks(goal, dag) == 0
    assert subtask.status == SubtaskStatus.PENDING
    assert subtask.job_id is None


def test_explicit_test_queue_can_activate_retained_goal(tmp_path):
    config = GoalEngineConfig(db_path=str(tmp_path / 'goals.db'), inference_enabled=False)
    pending = GoalEngine(config=config)
    goal = pending.propose_goal(title='Research a comparison')
    pending.accept_goal(goal.goal_id)
    pending.activate_goal(goal.goal_id)
    queue = InMemoryQueueBackend()
    configured = GoalEngine(config=config, queue_bridge=GoalQueueBridge(queue))
    active = configured.activate_goal(goal.goal_id)
    assert active.status == GoalStatus.ACTIVE
    assert 'dispatch_unavailable' not in active.context
    assert queue.list_jobs()
    dag = configured.get_dag(goal.goal_id)
    assert {s.job_id for s in dag.subtasks.values() if s.status == SubtaskStatus.DISPATCHED} == {
        j.job_id for j in queue.list_jobs()
    }
