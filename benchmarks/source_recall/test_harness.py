"""Transport/storage smoke test, not a semantic-quality result."""
import json
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from assessment import assess
import pytest
from run import SelectionCapture, prepare_sources, environment
from apsimo.memory.recall import calibration_fingerprint
from apsimo.memory.selection import RecallSelector

ROOT = Path(__file__).resolve().parent


def test_http_run_reuses_extraction_and_rejects_unmarked_state(tmp_path):
    calls = []
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(self.path)
            if self.path.endswith('/embeddings'):
                data = {'model': 'smoke-embed', 'data': [{'index': i, 'embedding': [1., 0., 0., 0.]}
                    for i, _ in enumerate(body['input'])]}
            elif self.path.endswith('/rerank'):
                data = {'results': [{'index': i, 'relevance_score': .99}
                    for i, _ in enumerate(body['documents'])]}
            elif self.path.endswith('/chat/completions'):
                data = {'model': 'smoke-chat', 'choices': [{'message': {'role': 'assistant', 'content': '[]'},
                        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
            else:
                self.send_error(404); return
            encoded = json.dumps(data).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    fixture = json.loads((ROOT / 'fixtures.json').read_text())
    fixture['records'] = fixture['records'][:1]
    fixture['queries'] = fixture['queries'][:1]
    fixture['queries'][0]['expected'] = [fixture['records'][0]['id']]
    source = tmp_path / 'fixture.json'; source.write_text(json.dumps(fixture))
    state = tmp_path / 'state'; output = tmp_path / 'result.json'
    endpoint = f'http://127.0.0.1:{server.server_port}'
    env = {k:v for k,v in os.environ.items() if not k.startswith(('COLONY_', 'OPENAI_', 'ANTHROPIC_'))}
    for role, model in [('CHAT', 'smoke-chat'), ('EMBED', 'smoke-embed'), ('RERANKER', 'smoke-rerank')]:
        env.update({f'COLONY_BENCH_{role}_BASE_URL': endpoint + ('/v1' if role == 'CHAT' else ''),
                    f'COLONY_BENCH_{role}_MODEL': model, f'COLONY_BENCH_{role}_API_KEY': 'smoke-key-never-in-artifact'})
    env['COLONY_BENCH_EMBED_DIMS'] = '4'
    command = [sys.executable, str(ROOT / 'run.py'), '--fixture', str(source), '--state-dir', str(state),
               '--output', str(output), '--threshold', '.95']
    try:
        for ranking in ('verbose-claim-json', 'grounded-quotation-bundles-v1'):
            completed = subprocess.run(command + ['--ranking-format', ranking], env=env,
                                       capture_output=True, text=True, timeout=60)
            assert completed.returncode == 0, completed.stderr
            result = json.loads(output.read_text())
            assert len(result['results']) == 3 and len(result['caption_results']) == 6
            assert result['ranking_format'] == ranking
            assert endpoint not in output.read_text() and 'smoke-key-never-in-artifact' not in output.read_text()
            assert len(result['selection_sources']) == 3
            for row in result['results'] + result['caption_results']:
                assert row['replay']['parameters']['max_chars'] == 6000
                assert row['replay']['query']
                assert 'selected' in row['replay'] and 'rerank_calls' in row['replay']
        assert calls.count('/v1/chat/completions') == 1
        # The new boundary uses the same real SQLite/Lance/selector pipeline,
        # but no extraction, graph embedding or caption extras. This local
        # fixture HTTP server is a transport stub, never a real model.
        fixture['records'][0]['role'] = 'tool'
        fixture['records'][0]['content'] = json.dumps({'output': '1: Carton limit: 12'})
        fixture['annotations'] = [{'id':'correction', 'target':fixture['records'][0]['id'],
            'excerpt':'Carton limit: 12', 'correction':'The carton limit is 9.', 'author_principal':'owner'}]
        fixture['queries'][0].update(query='What is the carton limit?', split='holdout')
        source_only_fixture = tmp_path / 'source-only.json'
        source_only_fixture.write_text(json.dumps(fixture))
        source_only_output = tmp_path / 'source-only-result.json'
        source_only_env = {key:value for key,value in env.items() if not key.startswith('COLONY_BENCH_CHAT_')}
        completed = subprocess.run([sys.executable, str(ROOT / 'run.py'), '--source-only', '--split','holdout',
            '--fixture', str(source_only_fixture), '--state-dir', str(tmp_path / 'source-only-state'),
            '--output', str(source_only_output), '--threshold','.95'], env=source_only_env,
            capture_output=True, text=True, timeout=60)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(source_only_output.read_text())
        assert len(result['results']) == 1 and result['caption_results'] == []
        assert result['source_claim_job_status'] == {} and result['extraction_model'] is None
        assert result['source_only'] and result['split'] == 'holdout'
        assert result['calibration']['candidate_format'] == 'grounded-quotation-bundles-v2-corrections-first'
        assert 'The carton limit is 9.' in result['results'][0]['context']
        assert calls.count('/v1/chat/completions') == 1
        # Prevent the harness's disposable graph-table reset from ever opening
        # an unmarked existing state directory, even with valid model settings.
        (state / 'benchmark-state.json').unlink()
        before = len(calls)
        failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        assert failed.returncode != 0 and len(calls) == before
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def test_assessment_rejects_missing_conflict_and_erased_derived_evidence():
    records = [dict(id='old', scope='private', at='2026-09-01', deleted=True),
               dict(id='derived', scope='private', at='2026-09-02', parents=['old'])]
    query = dict(principal='owner', as_of='2026-09-05', expected=[], abstain=True)
    assert assess(query, [], records)['strict_pass']
    assert not assess(query, [{'source_uri': 'turn:derived'}], records)['strict_pass']
    query.update(expected=['derived'], abstain=False, conflict=True)
    assert not assess(query, [{'source_uri': 'turn:derived'}], records)['strict_pass']


def test_source_only_preparation_keeps_tool_role_and_owner_correction(tmp_path):
    from apsimo.turns import TurnIdempotencyLedger
    from apsimo.beliefs.source_projection import SourceClaimProjection
    from apsimo.beliefs.source_time import MemoryTimeQuery
    from apsimo.turns.source_annotations import expand
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    raw = json.dumps({'output': '1: Carton limit: 12\n2: Applies to the narrow rack.'})
    fixture = {'records': [{'id': 'original', 'role': 'tool', 'at': '2026-01-01', 'content': raw}],
               'annotations': [{'id': 'limit-correction', 'target': 'original',
                   'excerpt': 'Carton limit: 12', 'correction': 'The narrow rack limit is 9 cartons.',
                   'author_principal': 'owner'}]}
    prepare_sources(ledger, fixture)
    prepare_sources(ledger, fixture)  # An exact marked-state resume adds no second note.
    with ledger._connect() as db:
        source = db.execute("SELECT messages_json FROM turn_sources WHERE turn_id='original'").fetchone()[0]
        assert json.loads(source) == [{'role': 'tool', 'content': raw}]
        assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM source_annotations').fetchone()[0] == 1
    hits = ledger.search_sources('Carton limit', contact_id='owner', session_id='later')
    _, rows = SourceClaimProjection(ledger).prepare_context([], hits,
        contact_id='owner', session_id='later', time_query=MemoryTimeQuery())
    rows = expand(ledger, rows, contact_id='owner', session_id='later')
    assert any('The narrow rack limit is 9 cartons.' in row['content'] for row in rows)
    assert any('attributed_correction' in row['content'] for row in rows)
    references = {ref['source_id']:ref for row in rows for ref in row['_annotation_source_refs']}
    assert 'original' in references and len(references) == 2
    assert set(references) == {ref['source_id'] for ref in ledger.source_references(
        list(references), contact_id='owner', session_id='later')}
    result = assess({'principal':'owner', 'as_of':'2026-12-01', 'expected':['original'],
        'relevance':{'original':'answer_useful'}, 'required_evidence':['limit is 9 cartons']}, rows,
        [dict(fixture['records'][0], scope='private')])
    assert result['useful_packet_pass']


def test_usefulness_labels_distinguish_eligible_junk_and_missing_condition():
    records = [dict(id=sid, scope='private', at='2026-01-01') for sid in ('result','request','scratch')]
    query = dict(principal='owner', as_of='2026-01-02', expected=['result'],
        relevance={'result':'answer_useful','request':'context_only','scratch':'irrelevant'},
        required_evidence=['only when dry'])
    answer = {'source_uri':'turn:result','content':'Use six clips only when dry.'}
    request = {'source_uri':'turn:request','content':'Find the clip count.'}
    scratch = {'source_uri':'turn:scratch','content':'Progress 20 percent.'}
    assert assess(query, [answer,request], records)['useful_packet_pass']
    junk = assess(query, [answer,scratch], records)
    assert junk['strict_pass'] and not junk['useful_packet_pass']
    assert junk['relevance']['irrelevant'] == ['scratch']
    assert not assess(query, [dict(answer, content='Use six clips.')], records)['required_evidence_present']


def test_source_only_environment_does_not_require_extraction(monkeypatch):
    for key in list(os.environ):
        if key.startswith('COLONY_BENCH_'):
            monkeypatch.delenv(key)
    for key, value in {'EMBED_BASE_URL':'http://fixture', 'EMBED_MODEL':'embed', 'EMBED_DIMS':'4',
                       'RERANKER_BASE_URL':'http://fixture', 'RERANKER_MODEL':'rerank'}.items():
        monkeypatch.setenv('COLONY_BENCH_' + key, value)
    assert 'COLONY_CHAT_MODEL' not in environment(source_only=True)
    with pytest.raises(ValueError, match='CHAT_BASE_URL'):
        environment()


def test_unanswered_question_can_retain_context_without_claiming_an_outcome():
    records = [dict(id=sid, scope='private', at='2026-01-01')
               for sid in ('request', 'scratch', 'missing-label')]
    query = dict(principal='owner', as_of='2026-01-02', expected=[], abstain=True,
        relevance={'request':'context_only', 'scratch':'irrelevant'})
    request = {'source_uri':'turn:request', 'content':'Inspect the checksum before using the archive.'}
    context = assess(query, [request], records)
    assert context['source_utility_pass']
    assert not context['strict_pass'] and not context['useful_packet_pass']
    assert not context['abstained'] and context['relevance']['answer_useful_selected'] == 0
    assert assess(query, [], records)['source_utility_pass']
    for sid in ('scratch', 'missing-label'):
        assert not assess(query, [dict(request, source_uri='turn:'+sid)], records)['source_utility_pass']
    assert not assess(dict(query, forbidden=['request']), [request], records)['source_utility_pass']
    assert not assess(dict(query, required_evidence=['inspection completed']), [request], records)['source_utility_pass']
    assert not assess(dict(query, relevance={'request':'answer_useful'}), [request], records)['source_utility_pass']


async def replay_observation(capture, monkeypatch, *, limit=None):
    """Example offline consumer: reject uncaptured requests after selection.

    RecallSelector intentionally catches provider exceptions and falls back.
    Therefore raising an assertion inside its callback alone cannot enforce a
    faithful replay. Validate every request outside that exception boundary.
    """
    for key, value in capture['environment'].items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # The endpoint was omitted. Reproduce its observed equality decision using
    # only the safe metadata, without pretending to recover that endpoint.
    expected = capture['environment']['COLONY_RECALL_RERANK_CALIBRATION']
    if expected == capture['calibration_fingerprint']:
        monkeypatch.setenv('COLONY_RECALL_RERANK_CALIBRATION', calibration_fingerprint(capture['calibration']))
    requests = []
    async def replay(query, documents, top_k):
        index = len(requests)
        requests.append({'query': query, 'documents': documents, 'top_k': top_k})
        observation = capture['rerank_calls'][index]
        if observation['outcome'] == 'returned':
            return deepcopy(observation['results'])
        raise RuntimeError('Captured unavailable reranker')
    parameters = dict(capture['parameters'])
    if limit is not None:
        parameters['limit'] = limit
    selector = RecallSelector(replay, calibration_metadata=lambda: capture['calibration'])
    result = await selector.select_context(capture['query'], deepcopy(capture['beliefs']),
                                          deepcopy(capture['quotations']), **parameters)
    expected_requests = [{key: call[key] for key in ('query', 'documents', 'top_k')}
                         for call in capture['rerank_calls']]
    if requests != expected_requests:
        raise ValueError('Unobserved reranker input; scores cannot be reused')
    return result


def test_selection_capture_roundtrip_and_unscored_input_rejection(monkeypatch):
    calibration = {'provider': 'synthetic', 'model': 'fixture', 'weights_revision': 'fixture-v1',
                   'endpoint': 'https://not-recorded.example/secret', 'api_key': 'not-recorded-key'}
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'on')
    monkeypatch.setenv('COLONY_RECALL_RERANK_MIN_SCORE', '.95')
    monkeypatch.setenv('COLONY_RECALL_RERANK_TIMEOUT_MS', '1200')
    monkeypatch.setenv('COLONY_RECALL_RERANK_CALIBRATION', calibration_fingerprint(calibration))
    monkeypatch.setenv('UNRELATED_SECRET', 'not-recorded-environment')
    beliefs = [{'id': f'row-{i}', 'content': f'Observation {i}', 'source_uri': f'turn:source-{i}'}
               for i in range(25)]
    async def scores(query, documents, top_k):
        return [{'index': i, 'score': .99 if i % 2 else .4} for i in range(top_k)]
    recorder = SelectionCapture(scores, calibration, [])
    selected, context, capture = asyncio.run(recorder.select('Which observation?', beliefs, []))
    encoded = json.dumps(capture)
    assert not any(secret in encoded for secret in ('https://', 'not-recorded', 'UNRELATED_SECRET', 'api_key'))
    capture = json.loads(encoded)
    assert len(capture['beliefs']) == 25
    assert len(capture['rerank_calls'][0]['documents']) == 20
    assert len(capture['rerank_calls'][0]['results']) == 20
    assert all(row['rerank_score'] >= .95 for row in selected)
    assert 'rerank_score' not in capture['beliefs'][0]
    assert asyncio.run(replay_observation(capture, monkeypatch)) == (selected, context)
    # Eight records would submit 25 documents. The captured top20 scores cannot
    # qualify this wider request, even though the selector itself fails open.
    with pytest.raises(ValueError, match='Unobserved reranker input'):
        asyncio.run(replay_observation(capture, monkeypatch, limit=8))
    changed = deepcopy(capture)
    changed['beliefs'][0]['content'] = 'A changed observation'
    with pytest.raises(ValueError, match='Unobserved reranker input'):
        asyncio.run(replay_observation(changed, monkeypatch))


@pytest.mark.parametrize('failure', ['error', 'cancelled'])
def test_selection_capture_retains_unavailable_without_error_body(monkeypatch, failure):
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'on')
    monkeypatch.setenv('COLONY_RECALL_RERANK_MIN_SCORE', '')
    monkeypatch.setenv('COLONY_RECALL_RERANK_TIMEOUT_MS', '1')
    monkeypatch.delenv('COLONY_RECALL_RERANK_CALIBRATION', raising=False)
    async def unavailable(query, documents, top_k):
        if failure == 'error':
            raise RuntimeError('https://not-recorded.example/private?key=not-recorded')
        await asyncio.Event().wait()
    beliefs = [{'id': f'row-{i}', 'content': f'Observation {i}'} for i in range(6)]
    recorder = SelectionCapture(unavailable, {'model': 'synthetic'}, [])
    selected, context, capture = asyncio.run(recorder.select('Which observation?', beliefs, []))
    assert capture['rerank_calls'][0]['outcome'] == failure
    assert 'results' not in capture['rerank_calls'][0]
    assert 'not-recorded' not in json.dumps(capture)
    assert all(row['rerank_status'] == 'unavailable' for row in selected)
    assert asyncio.run(replay_observation(capture, monkeypatch)) == (selected, context)
