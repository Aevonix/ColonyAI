"""Real CLI and existing router/HTTP client, served by a controlled local fixture."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from pacomind.qualification.cli import run
from pacomind.qualification.cases import STANDARD
from pacomind.qualification.records import read
from pacomind.qualification.runner import evaluate, inspect_binding, router_for
from test_function_routing import endpoint, config


def test_inspect_never_queries_endpoint_or_exports_credential(tmp_path, capsys):
    with endpoint() as (url, calls):
        cfg = config(url, url)
        path = tmp_path/'config.json'
        path.write_text(json.dumps(cfg))
        assert run(SimpleNamespace(models_command='inspect', binding='interactive', config=path)) == 0
        result = json.loads(capsys.readouterr().out)
        assert calls == []
        assert result['returned_model'] is result['observed_weight_revision'] is None
        assert cfg['apiKey'] not in json.dumps(result)
        assert url not in json.dumps(result)


def test_ephemeral_case_binding_keeps_support_role_and_original_config():
    cfg = config('http://127.0.0.1:9911/v1','http://127.0.0.1:9912/v1')
    cfg['functionRoles']['judging'] = ['deliberate']
    original = deepcopy(cfg)
    candidate = router_for(cfg,'interactive',[STANDARD[1]])
    roles = candidate.routing_status()['roles']
    assert roles['extraction'] == ['interactive'] and roles['judging'] == ['deliberate']
    assert cfg == original


@pytest.mark.asyncio
async def test_task_routed_memory_uses_candidate_and_preserves_judging(tmp_path):
    from pacomind.qualification.memory_cases import CASES, CONSUMERS, EVALUATORS
    from test_source_claim_projection import claim

    def extract(payload):
        text = json.loads(payload['messages'][-1]['content'])['message']
        return json.dumps([claim(text, 'decaffeinated tea', predicate='tea_preference',
                                 memory_kind='preference')]) if text.startswith('I prefer') else '[]'

    review = json.dumps({'0': {'keep': True, 'reason': 'Controlled source-grounded admission.'}})
    with endpoint(content=extract) as (candidate_url, candidate_calls), endpoint(content=review) as (judge_url, judge_calls):
        cfg = config(candidate_url, judge_url, timeoutSeconds=10, deadlineSeconds=20)
        cfg['functionRoles']['judging'] = ['deliberate']
        # Production-shaped task remapping previously bypassed the case's
        # extraction candidate and silently dispatched to the reasoning model.
        cfg['taskRoles'] = {'source_claim_extraction': 'reasoning', 'skill_distillation': 'judging'}
        original = deepcopy(cfg)
        case = CASES[0]
        selected = router_for(cfg, 'interactive', [case])
        status = selected.routing_status()
        assert status['task_roles'] == {'source_claim_extraction': 'extraction', 'skill_distillation': 'judging'}
        assert status['roles']['reasoning'] == ['deliberate', 'interactive']
        assert status['roles']['judging'] == ['deliberate']
        await evaluate(tmp_path/'run', inspect_binding(cfg, 'interactive'), [case], CONSUMERS, EVALUATORS,
                       lambda _: selected, evidence_mode='controlled')
        result = read(tmp_path/'run/attempts'/case.id/'result.json')
        manifest = read(tmp_path/'run/run.json')
        assert result['outcome'] == result['primary_outcome'] == 'pass'
        assert len(candidate_calls) == 3 and len(judge_calls) == 1
        assert all(row['payload']['model'] == 'fast-neutral' for row in candidate_calls)
        assert judge_calls[0]['payload']['model'] == 'strong-neutral'
        calls = [row for row in result['observations'] if row['boundary'] == 'router_complete']
        assert [(row['role'], row['selected_binding']) for row in calls] == [
            ('extraction', 'interactive'), ('judging', 'deliberate'),
            ('extraction', 'interactive'), ('extraction', 'interactive')]
        assert result['qualification_routing']['target_task_role_overrides'] == {'source_claim_extraction': 'extraction'}
        assert manifest['cases'][0]['target_tasks'] == ['source_claim_extraction']
        assert manifest['recipe']['routing_snapshot']['task_roles'] == original['taskRoles']
        assert cfg == original


def test_cli_evaluate_and_resume_with_controlled_existing_http_router(tmp_path,capsys):
    with endpoint(content='{"blue":"drawer 4","silver":null}') as (url,calls):
        cfg=config(url,url)
        path=tmp_path/'config.json'; path.write_text(json.dumps(cfg)); before=path.read_bytes()
        args=SimpleNamespace(models_command='evaluate',binding='interactive',config=path,
            roles='chat',suite='standard',output=tmp_path/'run',resume=False,evidence_mode='controlled')
        assert run(args) == 0
        first=(args.output/'attempts'/'chat.grounded-note'/'result.json').read_bytes()
        row=json.loads(first)
        assert row['outcome'] == row['primary_outcome'] == 'pass'
        assert row['observations'][0]['returned_model'] == 'fast-neutral'
        assert row['observations'][0]['usage']['total_tokens'] == 12
        assert row['observations'][0]['role'] == 'chat'
        args.resume=True
        assert run(args) == 0
        assert len(calls) == 1 and path.read_bytes() == before
        assert (args.output/'attempts'/'chat.grounded-note'/'result.json').read_bytes() == first
        assert len(list(args.output.glob('report-*.json'))) == 2
        assert 'role_completion' in capsys.readouterr().out


def test_main_installs_model_commands_without_loading_runtime(monkeypatch,capsys):
    from pacomind import cli
    monkeypatch.setattr('sys.argv',['pacomind','models','--help'])
    with pytest.raises(SystemExit) as stop: cli.main()
    assert stop.value.code == 0
    assert '{inspect,evaluate,compare}' in capsys.readouterr().out
