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
