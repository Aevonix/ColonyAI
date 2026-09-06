"""Finite neutral comparison, using actual current Colony retrieval paths.

Canonical SQLite/Lance and model calls are real. Neo4j reads are replaced with
an explicit scoped fixture adapter, never an expected-answer oracle.
"""
import argparse
import asyncio
import base64
from dataclasses import asdict
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time

# Also works from a checkout without installing the package itself.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'sidecar'))
from assessment import assess
from colony_sidecar.router.router import LLMRouter
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.source_vectors import SourceVectors, merge_source_hits
from colony_sidecar.turns.media import SourceMedia
from colony_sidecar.vector.config import EmbeddingConfig
from colony_sidecar.vector.embedder import EmbeddingPipeline
from colony_sidecar.vector.openai_provider import OpenAIAPIEmbeddingProvider
from colony_sidecar.vector.reranker import OpenAIAPIRerankerProvider
from colony_sidecar.vector.indexes import IndexCatalog
from colony_sidecar.vector.store import VectorStore
from colony_sidecar.vector.collections import Collection
from colony_sidecar.vector.query import VectorItem
from colony_sidecar.intelligence.graph.client import ColonyGraph
from colony_sidecar.intelligence.graph.selection import RecallSelector
from colony_sidecar.intelligence.graph.recall import calibration_fingerprint
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.beliefs.source_time import interpret_time_query

class Result:
    def __init__(self, rows): self.rows = iter(rows)
    def __aiter__(self): return self
    async def __anext__(self):
        try: return next(self.rows)
        except StopIteration: raise StopAsyncIteration


def image_message():
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (320, 160), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 35, 100, 120), fill='red')
    draw.ellipse((205, 40, 280, 115), fill='blue')
    output = io.BytesIO(); image.save(output, format='PNG')
    return {'role': 'user', 'content': [
        {'type': 'text', 'text': 'Please retain this reference image.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()}},
    ]}


def environment():
    # Only explicit benchmark variables are read; no deployment config loader.
    required = ('EMBED_BASE_URL', 'EMBED_MODEL', 'EMBED_DIMS',
                'RERANKER_BASE_URL', 'RERANKER_MODEL', 'CHAT_BASE_URL', 'CHAT_MODEL')
    missing = [name for name in required if not os.environ.get('COLONY_BENCH_' + name)]
    if missing:
        raise ValueError('Missing benchmark variables: ' + ', '.join('COLONY_BENCH_' + name for name in missing))
    names = (*required, 'EMBED_API_KEY', 'RERANKER_API_KEY', 'CHAT_API_KEY',
             'RERANKER_PROMPT_STYLE', 'EMBED_QUERY_INSTRUCTION', 'CHAT_WEIGHT_REVISION')
    return {'COLONY_' + name: os.environ['COLONY_BENCH_' + name]
            for name in names if 'COLONY_BENCH_' + name in os.environ}


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


class GraphReadAdapter:
    def __init__(self, ledger, records):
        self.ledger, self.records = ledger, records
    def session(self, database=None): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def run(self, query, **params):
        contact = params.get('person_id') or 'owner'
        with self.ledger._connect() as conn:
            sources = {row['turn_id']:dict(row) for row in conn.execute('SELECT * FROM turn_sources WHERE contact_id=?', (contact,))}
        if 'ids' in params:
            ids = params['ids']
        elif 'index_name' in params:
            # Actual SQLite FTS input, same literal terms, not graph truth logic.
            words = ' '.join(re.findall(r'"([^"\\]+)"', params['search_text']))
            ids = [row['turn_id'] for row in self.ledger.search_sources(words, contact_id=contact, session_id='later', limit=10)]
        else:
            raise AssertionError('Unexpected graph read shape')
        rows = []
        for mid in ids:
            if mid not in sources or mid not in self.records or not self.records[mid].get('indexed', True): continue
            record = self.records[mid]
            rows.append({'memory': {'id':mid, 'content':record['content'], 'source_uri':'turn:'+mid,
                'person_id':contact, 'strength':1., 'effective_confidence':.95, 'epistemic_state':'inferred',
                'created_at':record['at'], 'entities':[]}, 'lexical_score':1/(len(rows)+1)})
        return Result(rows)


async def run(config, args):
    fixture_path = args.fixture
    fixture = json.loads(fixture_path.read_text())
    if fixture.get('distractors', {}).get('count', 0):
        raise ValueError('This bounded harness requires explicit records, not generated distractors')
    if len(fixture['records']) > 120 or len(fixture['queries']) > 96:
        raise ValueError('At most 120 sources and 96 queries per run')
    tmp = args.state_dir.resolve()
    manifest_path = tmp / 'benchmark-state.json'
    resumed = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    # Never repurpose an existing deployment or an unmarked database directory.
    if resumed is None and tmp.exists() and any(tmp.iterdir()):
        raise ValueError('Use a new empty disposable state directory')
    tmp.mkdir(parents=True, exist_ok=True)
    identity = {'fixture_sha256': hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                'extraction_model': config['COLONY_CHAT_MODEL'],
                'extraction_weight_revision': config.get('COLONY_CHAT_WEIGHT_REVISION', 'unknown'),
                'query_instruction': config.get('COLONY_EMBED_QUERY_INSTRUCTION',
                    'Instruct: Given a search query, retrieve relevant memories that answer it\nQuery: ')}
    if resumed and resumed.get('identity') != identity:
        raise ValueError('Fixture or extraction declaration changed; use a new state directory')
    if resumed is None:
        resumed = {'identity': identity, 'prepared': False, 'captures': []}
        save(manifest_path, resumed)
    os.environ.update(COLONY_STATE_DIR=str(tmp), COLONY_RECALL_HYBRID='on', COLONY_RECALL_RERANK='on',
        COLONY_RECALL_RERANK_TIMEOUT_MS='1200', COLONY_RECALL_RERANK_MIN_SCORE=str(args.threshold),
        COLONY_EMBED_QUERY_INSTRUCTION=identity['query_instruction'])
    router = LLMRouter()
    host = {'provider': 'local', 'models': {}, 'modelPool': {'bench': {
        'model': config['COLONY_CHAT_MODEL'], 'baseUrl': config['COLONY_CHAT_BASE_URL'],
        'apiKey': config.get('COLONY_CHAT_API_KEY', ''),
        'weightRevision': config.get('COLONY_CHAT_WEIGHT_REVISION', 'unknown'),
        'maxTokens': 1400}}, 'functionRoles': {'extraction': ['bench']}}
    if os.environ.get('COLONY_BENCH_LOCAL_HOSTS'):
        host['localHosts'] = os.environ['COLONY_BENCH_LOCAL_HOSTS'].split(',')
    router.configure(host)
    model = config['COLONY_CHAT_MODEL']
    provider=OpenAIAPIEmbeddingProvider(EmbeddingConfig(provider='openai_api', model_id=config['COLONY_EMBED_MODEL'], dimensions=int(config['COLONY_EMBED_DIMS'])))
    provider.configure(config['COLONY_EMBED_BASE_URL'],config.get('COLONY_EMBED_API_KEY',''))
    pipeline=EmbeddingPipeline(provider); await pipeline.warmup()
    embedding_identity = asdict(pipeline.index_identity)
    if resumed.get('embedding_identity') not in (None, embedding_identity):
        raise ValueError('Embedding identity changed; use a new state directory')
    resumed['embedding_identity'] = embedding_identity
    save(manifest_path, resumed)
    reranker=OpenAIAPIRerankerProvider(config['COLONY_RERANKER_MODEL'])
    reranker.configure(config['COLONY_RERANKER_BASE_URL'], config.get('COLONY_RERANKER_API_KEY',''), config.get('COLONY_RERANKER_PROMPT_STYLE',''))
    calibration={**reranker.calibration_metadata(), 'weights_revision':'unverified', 'embedding_identity':pipeline.index_identity.fingerprint,
                 'candidate_format':args.ranking_format}
    os.environ['COLONY_RECALL_RERANK_CALIBRATION']=calibration_fingerprint(calibration)
    calls=[]
    async def rerank(query, documents, top_k):
        start=time.perf_counter()
        result=await reranker.rerank(query,documents,top_k=top_k)
        calls.append({'kind':'rerank','ms':(time.perf_counter()-start)*1000,'documents':len(documents)})
        return [asdict(row) for row in result]
    selector=RecallSelector(rerank,calibration_metadata=lambda:calibration)
    ledger=TurnIdempotencyLedger(tmp/'turn-idempotency.db')
    claims=SourceClaimProjection(ledger)
    store=VectorStore(str(tmp/'lancedb'),identity=pipeline.index_identity,catalog=IndexCatalog(ledger))
    await store.connect(pipeline.dimensions); await store.ensure_collections(pipeline.dimensions)
    projections=SourceVectors(ledger,store,pipeline)
    captures=resumed['captures']; original_complete=router.complete
    async def captured(**kwargs):
        start=time.perf_counter(); response=await original_complete(**kwargs)
        captures.append({'input':kwargs['messages'][-1]['content'],'output':response.content,
            'model':response.model_id,'ms':(time.perf_counter()-start)*1000})
        return response
    router.complete=captured
    records={row['id']:row for row in fixture['records']}
    # Corpus event times are input evidence. No expected labels, supersession,
    # confidence, or contradiction flags enter extraction or retrieval.
    for index, record in enumerate([] if resumed['prepared'] else fixture['records']):
        ledger.record_source(record['id'],contact_id='owner',session_id='neutral-corpus',
            messages=[{'role':'user','content':record['content']}], occurred_at=record['at']+'T12:00:00+00:00')
        await claims.process_one(router)
        if (index+1)%12==0: print(f"Actual extraction {index+1}/{len(fixture['records'])}",flush=True)
    resumed.update(prepared=True, captures=captures)
    save(manifest_path, resumed)
    # Explicit fixture forget requests use the actual canonical erasure API.
    # Unlinked derived-summary fixture rows remain independent, intentionally.
    deleted=[row['id'] for row in fixture['records'] if row.get('deleted')]
    if deleted: ledger.erase_sources(contact_id='owner',turn_ids=deleted)
    while await projections.process_one(): pass
    # Disposable benchmark generation only, remove prior attempt's fixture
    # graph rows before recreating this comparison arm. No source mutation.
    graph_table=await store._table(Collection.MEMORIES,write=True)
    await graph_table.delete('true')
    for start in range(0,len(fixture['records']),16):
        batch=[row for row in fixture['records'][start:start+16] if row.get('indexed',True)]
        vectors=await pipeline.embed_batch([row['content'] for row in batch])
        await store.add_batch(Collection.MEMORIES,[VectorItem(id=row['id'],text=row['content'],vector=vec,
            metadata={'source_uri':'turn:'+row['id'],'person_id':'owner'}) for row,vec in zip(batch,vectors)])
    graph=ColonyGraph.__new__(ColonyGraph); graph.database='fixture'; graph.driver=GraphReadAdapter(ledger,records)
    graph._vector_store=store; graph.set_embed_fn(pipeline.embed)
    results=[]
    for index,q in enumerate(fixture['queries']):
        contact=q['principal']; time_query=interpret_time_query(q['query'],now=datetime.fromisoformat(q['as_of']+'T18:00:00+00:00'))
        lexical=ledger.search_sources(q['query'],contact_id=contact,session_id='later',limit=10)
        # One shared query embedding; report its measured cost separately.
        start=time.perf_counter(); await pipeline.embed_query(q['query']); query_ms=(time.perf_counter()-start)*1000
        start=time.perf_counter(); graph_rows=await graph.recall_candidates(query=q['query'],person_id=contact,limit=25); graph_ms=(time.perf_counter()-start)*1000
        start=time.perf_counter(); semantic,media=await projections.search(q['query'],contact_id=contact,session_id='later',limit=15); source_ms=(time.perf_counter()-start)*1000
        for arm in ('lexical_only','existing_hybrid','source_semantic'):
            start=time.perf_counter()
            hits=merge_source_hits(lexical,semantic) if arm=='source_semantic' else lexical
            beliefs,quotes=claims.prepare_context(graph_rows if arm!='lexical_only' else [],hits,
                contact_id=contact,session_id='later',time_query=time_query)
            if args.ranking_format == 'verbose-claim-json':
                beliefs = [dict(row, ranking_text=row['content']) for row in beliefs]
            selected,context=await selector.select_context(q['query'],beliefs,quotes,limit=5,max_chars=6000)
            results.append({'query_id':q['id'],'split':q['split'],'tags':q['tags'],'arm':arm,
                'assessment':assess(q,selected,fixture['records']),'context':context,
                'selection_ms':(time.perf_counter()-start)*1000,'query_embedding_ms':query_ms,
                'graph_ms':graph_ms,'source_semantic_ms':source_ms})
        if (index+1)%12==0: print(f"Actual retrieval {index+1}/{len(fixture['queries'])}",flush=True)
    summaries={}
    for arm in ('lexical_only','existing_hybrid','source_semantic'):
        summaries[arm]={}
        for split in ('development','holdout'):
            rows=[row for row in results if row['arm']==arm and row['split']==split]
            if not rows: continue
            assessments=[row['assessment'] for row in rows]
            summaries[arm][split]={'cases':len(rows),'strict_pass':sum(a['strict_pass'] for a in assessments),
                'expected_found':sum(a['expected_found'] for a in assessments),
                'mean_expected_recall':statistics.mean(a['recall'] for a in assessments if a['recall'] is not None)
                    if any(a['recall'] is not None for a in assessments) else None,
                'abstention_cases':sum(a['abstained'] is not None for a in assessments),
                'abstention_pass':sum(a['abstained'] is True for a in assessments),
                'forbidden_hits':sum(bool(a['forbidden']) for a in assessments),
                'conflicts_marked':sum(a['conflict_marked'] is True for a in assessments),
                'selection_p50_ms':statistics.median(row['selection_ms'] for row in rows),
                'missing_expected':[row['query_id'] for row in rows if not row['assessment']['expected_found']]}
    # Fresh caption paraphrases, separate from the frozen corpus and its scores.
    ledger.record_source('fresh-image',contact_id='image-owner',session_id='image-session',messages=[image_message()])
    media_store=SourceMedia(ledger); job=media_store.claim_job()
    # This description is the output of the already-qualified image loop. This
    # benchmark measures caption retrieval, not new visual recognition quality.
    if job is not None:
        media_store.finish(job,description='A red rectangle is on the left and a blue circle on the right, on white.',model='previously-qualified-neutral-image-description')
    while await projections.process_one(): pass
    media_results=[]
    for query in ('Azure disc beside crimson quadrilateral', 'Circular object adjacent scarlet polygon', 'What was the pictured shape on the right?'):
        lexical=media_store.search(query,contact_id='image-owner',session_id='different')
        _,semantic=await projections.search(query,contact_id='image-owner',session_id='different')
        for arm, candidates in [('lexical',lexical),('hybrid',list({row['id']:row for row in lexical+semantic}.values()))]:
            selected,context=await selector.select_context(query,[],candidates,limit=5,max_chars=6000)
            media_results.append({'query':query,'arm':arm,'candidates':len(candidates),'returned':bool(selected),'context':context})
    with ledger._connect() as conn:
        job_status={row[0]:row[1] for row in conn.execute('SELECT status,count(*) FROM source_claim_jobs GROUP BY status')}
        claim_count=conn.execute('SELECT count(*) FROM source_claims').fetchone()[0]
    output={'summary':summaries,'results':results,'caption_results':media_results,'model_identity':asdict(pipeline.index_identity),
        'source_claim_job_status':job_status,'source_claim_count':claim_count,
        'extraction_model':model,'calibration':{k:v for k,v in calibration.items() if k != 'endpoint'},
        'fixed_threshold':args.threshold,'state_dir':str(tmp),'ranking_format':args.ranking_format,
        'fixture_sha256':hashlib.sha256(fixture_path.read_bytes()).hexdigest(),'calls':calls,
        'limits':['Default corpus: 120 frozen neutral sources, 96 queries, 24 holdout. A supplied smaller fixture is a smoke test.',
            'Actual local extraction/embeddings/reranker and canonical SQLite/Lance. Graph query reads are scoped SQLite fixture adapter, not Neo4j.',
            'Public/team fixture annotations do not invent shared authority: sources belong to fixture owner; six guest privacy queries expect abstention.',
            'Synthetic query_generation labels cannot substitute for a real embedding swap. Equal-dimension incompatibility is covered by real-Lance controlled tests separately.',
            'Unlinked historical derived-summary fixture records remain independent sources, not retroactively invented lineage.',
            'Threshold is an explicit operator input, not a universal score; immutable remote weights remain unknown unless separately declared.',
            'One shared query embedding measured separately; arm timings include actual selection but cached embedding. No production-scale latency or ANN benchmark.',
            'Fresh caption paraphrases use the prior neutral qualified description; they are text-caption search, not image embeddings or new visual understanding.']}
    path=args.output; save(path,output)
    await provider.close()
    print(json.dumps({'summary':summaries,'captions':[{k:r[k] for k in ('query','arm','returned')} for r in media_results],'artifact':str(path)},indent=2),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=ROOT / 'fixtures.json')
    parser.add_argument('--state-dir', type=Path, required=True, help='New disposable directory, or its marked extraction state to reuse')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--threshold', type=float, required=True, help='Run-specific declared reranker cutoff, never selected from holdout answers')
    parser.add_argument('--ranking-format', choices=('grounded-quotation-bundles-v1', 'verbose-claim-json'), default='grounded-quotation-bundles-v1')
    args = parser.parse_args()
    if not __import__('math').isfinite(args.threshold):
        parser.error('threshold must be finite')
    asyncio.run(run(environment(), args))


if __name__ == '__main__':
    main()
