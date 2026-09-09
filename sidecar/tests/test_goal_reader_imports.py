"""Real API/server imports keep legacy records without loading their planner."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


SIDECAR = Path(__file__).resolve().parents[1]
PLANNERS = ('engine', 'decomposer', 'inference', 'priority', 'queue_bridge', 'replan')


def isolated(tmp_path, code):
    environment = {**os.environ, 'COLONY_STATE_DIR': str(tmp_path / 'state'),
                   'COLONY_SKIP_DOTENV': '1', 'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
                   'HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-I', '-B', '-c',
        'import sys\nsys.path.insert(0, sys.argv[1])\n' + code,
        str(SIDECAR)], cwd=tmp_path, env=environment, text=True,
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize('entry', ('colony_sidecar.api.routers.host', 'colony_sidecar.server'))
def test_actual_api_and_server_import_do_not_load_legacy_planner(tmp_path, entry):
    isolated(tmp_path, f'''
import importlib
entry = importlib.import_module({entry!r})
assert entry is not None
from colony_sidecar.goals import Goal, GoalStore, GoalNotFoundError
assert Goal.__module__ == 'colony_sidecar.goals.models'
assert GoalStore.__module__ == 'colony_sidecar.goals.store'
assert GoalNotFoundError.__module__ == 'colony_sidecar.goals.store'
for name in {PLANNERS!r}:
    assert 'colony_sidecar.goals.' + name not in sys.modules, name
''')
    assert not (tmp_path / 'state' / 'colony-goals.db').exists()


def test_explicit_legacy_exports_retain_identity_and_durable_behavior(tmp_path):
    isolated(tmp_path, '''
import importlib
from pathlib import Path
import colony_sidecar.goals as goals
assert set(goals.__all__) <= set(dir(goals))
for name, module in goals._PLANNER_EXPORTS.items():
    assert getattr(goals, name) is getattr(importlib.import_module('colony_sidecar.goals.' + module), name)
try:
    goals.NotARealGoalExport
except AttributeError:
    pass
else:
    raise AssertionError('unknown export must fail')
engine = goals.GoalEngine(config=goals.GoalEngineConfig(db_path='retained-goals.db'))
goal = engine.propose_goal('Review the saved workshop report')
engine.accept_goal(goal.goal_id)
accepted = engine.activate_goal(goal.goal_id)
assert accepted.status == goals.GoalStatus.ACCEPTED
assert accepted.context['dispatch_unavailable'] == 'queue_backend_unconfigured'
assert engine.get_dag(goal.goal_id) is None
assert len(engine.get_audit_trail(goal.goal_id)) == 1
engine._store.close()
reopened = goals.GoalEngine(config=goals.GoalEngineConfig(db_path='retained-goals.db'))
assert reopened.get_goal(goal.goal_id).goal_id == goal.goal_id
assert reopened.complete_task(goal.goal_id)
assert reopened.complete_task(goal.goal_id)
assert reopened.get_goal(goal.goal_id).context['completion_basis'] == 'reported_completion'
assert len(reopened.get_audit_trail(goal.goal_id)) == 2
reopened._store.close()
''')
