"""Real CLI and existing router/HTTP client, served by a controlled local fixture."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from colony_sidecar.qualification.cli import run
from colony_sidecar.qualification.cases import STANDARD
from colony_sidecar.qualification.records import read
from colony_sidecar.qualification.runner import inspect_binding, router_for
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
    from colony_sidecar import cli
    monkeypatch.setattr('sys.argv',['colony','models','--help'])
    with pytest.raises(SystemExit) as stop: cli.main()
    assert stop.value.code == 0
    assert '{inspect,evaluate,compare}' in capsys.readouterr().out
