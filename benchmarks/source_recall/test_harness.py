"""Transport/storage smoke test, not a semantic-quality result."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from assessment import assess

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
