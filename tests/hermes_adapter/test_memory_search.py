"""Canonical search discovery and invalidation through actual native dispatch."""
import test_source_annotate as annotation


def test_native_search_supplies_excerpts_then_opens_sources_and_rechecks_changes(artifacts, tmp_path, monkeypatch):
    probe = annotation.PROBE
    old = "def dispatch(args,*,session='later',task='review-task',turn='review-turn',call='operator-call'):"
    assert probe.count(old) == 1
    probe = probe.replace(old, old.replace("call='operator-call'", "call='operator-call',tool='apsimo_memory_annotate'"))
    old = "name='apsimo_memory_annotate',arguments=json.dumps(args)"
    assert probe.count(old) == 1
    probe = probe.replace(old, "name=tool,arguments=json.dumps(args)")
    old = "return json.JSONDecoder().raw_decode(results[0]['content'])[0]"
    assert probe.count(old) == 1
    probe = probe.replace(old, "dispatch.last_result=results[0]\n        " + old)
    old = "print(json.dumps({'native_dispatch':True"
    assert probe.count(old) == 1
    check = r'''
from apsimo_hermes.client import ApsimoClient
from apsimo.turns.source_attribution import correct
import copy,time
search_schema=next(s for s in apsimo_hermes._TOOL_SCHEMAS if s['name']=='colony_memory_search')
assert set(search_schema['parameters']['properties'])=={'query','limit'}
assert search_schema['parameters']['additionalProperties'] is False
assert search_schema['parameters']['properties']['query']['maxLength']==4096
search_calls=[]; read_calls=[]
post=ApsimoClient.post
def observed_post(self,path,**kwargs):
    if path=='/v1/host/memory/search':
        search_calls.append(copy.deepcopy(kwargs))
        assert kwargs['timeout']==10
        assert 9 < kwargs['_deadline_monotonic']-time.monotonic() <= 10
    if path=='/v1/host/memory/read': read_calls.append(copy.deepcopy(kwargs))
    return post(self,path,**kwargs)
ApsimoClient.post=observed_post
current=None; messages=[]; search_number=0
def begin_search(session):
    global current,messages
    current={'session':session,'task':session+'-task','turn':session+'-turn'}
    prime(session,current['task'],current['turn'],supplied=False)
    messages=[{'role':'user','content':''}]
def search(query,**extra):
    global search_number
    search_number+=1
    return dispatch({'query':query,**extra},**current,call='search-'+str(search_number),tool='apsimo_memory_search')
def open_original(reference):
    global search_number
    search_number+=1
    return dispatch(reference,**current,call='open-'+str(search_number),tool='apsimo_memory_read_source')
def carry(row):
    messages.append(dict(row))
    return apply_llm_request_middleware({'messages':messages},session_id=current['session'],
        task_id=current['task'],turn_id=current['turn']).payload
def seed(source_id,text):
    ledger.record_source(source_id,contact_id='person',session_id='original-source',
        messages=[{'role':'user','content':text}],occurred_at='2024-02-03T04:05:06+00:00',derive_claims=False)
    return ledger.source_references([source_id],contact_id='person',session_id='search-reader')[0]

text='The heliograph repair meeting starts at 16:40 in the south workshop.'
reference=seed('search-heliograph',text)
begin_search('search-reader')
assert 'error' in open_original(reference) and not read_calls
first=search('heliograph repair meeting')
result=dict(dispatch.last_result)
assert 'error' not in first,first
assert reference in first['source_refs'] and 'heliograph' in first['content'],first
assert first['apsimo_memory_search_v1'] and first['evidence_basis']=='recalled_excerpt'
assert first['full_source_opened'] is False and 'colony_source_read_v1' not in first
assert search_calls[-1]['json']=={'identity':{'host_id':'hermes'},'person_id':'person',
    'session_id':'search-reader','query':'heliograph repair meeting','limit':5}
# The handler returning a search result alone does not admit source ancestry.
assert 'error' in open_original(reference) and not read_calls
forged={**result,'tool_call_id':'not-the-authentic-call'}
filtered=carry(forged)
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==forged['tool_call_id'])!=forged
assert 'error' in open_original(reference) and not read_calls
filtered=carry(result)
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==result['tool_call_id'])==result
opened=open_original(reference)
assert text in opened['content'] and len(read_calls)==1,opened
assert opened['colony_source_read_v1'] and 'apsimo_memory_search_v1' not in opened
assert read_calls[-1]['json']['person_id']=='person'
assert read_calls[-1]['json']['session_id']=='search-reader'

begin_search('argument-reader')
before_calls=len(search_calls)
for invalid in ({'query':'   '},{'query':'heliograph','limit':True},{'query':'heliograph','person_id':'other'}):
    bad=dispatch(invalid,**current,call='bad-'+str(len(json.dumps(invalid))),tool='apsimo_memory_search')
    assert 'error' in bad or bad.get('status')=='denied',bad
assert len(search_calls)==before_calls
missing=json.loads(handle_function_call('apsimo_memory_search',{'query':'heliograph'}))
assert 'error' in missing or missing.get('status')=='denied',missing
assert len(search_calls)==before_calls
# The participant comes from native transport resolution; this owner-scoped
# API credential must reject an attempt to use a different contact's scope.
prime('search-guest','search-guest-task','search-guest-turn',platform='sms',sender='guest-fixture',supplied=False)
guest=dispatch({'query':'heliograph'},session='search-guest',task='search-guest-task',turn='search-guest-turn',
    call='guest-search',tool='apsimo_memory_search')
assert guest.get('status_code')==403 and 'heliograph repair meeting' not in json.dumps(guest),guest
assert search_calls[-1]['json']['person_id']=='guest'

begin_search('empty-reader')
empty=search('nonexistent zirconium submarine')
assert empty['count']==0 and empty['content']=='' and empty['source_refs']==[],empty
# An HTTP service failure stays unavailable, not a successful empty result.
original_respond=respond
def unavailable_search(request):
    if request.url.path=='/v1/host/memory/search':
        return httpx.Response(503,json={'detail':{'code':'memory_backend_unavailable'}})
    return original_respond(request)
respond=unavailable_search
unavailable=search('heliograph repair meeting')
assert unavailable.get('status_code')==503 and 'count' not in unavailable,unavailable
respond=original_respond

# A new owner correction after search must withhold the old excerpt before
# the next model request, even though the original bytes still exist.
begin_search('correction-reader')
uncorrected=search('heliograph repair meeting'); stale=dict(dispatch.last_result)
assert reference in uncorrected['source_refs']
note=ledger.append_source_annotation(contact_id='person',session_id='correction-reader',annotation_id='meeting-time-note',
    **reference,excerpt='16:40',correction='The meeting was moved to 17:20.',author_principal='operator')
filtered=carry(stale)
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==stale['tool_call_id'])!=stale
assert '16:40' not in json.dumps(filtered),filtered
fresh=search('heliograph repair meeting'); assert '17:20' in fresh['content'],fresh
fresh_result=dict(dispatch.last_result)
filtered=carry(fresh_result)
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==fresh_result['tool_call_id'])==fresh_result
# The same invariant applies to evidence which already had a correction.
ledger.append_source_annotation(contact_id='person',session_id='correction-reader',annotation_id='meeting-room-note',
    **reference,excerpt='south workshop',correction='Use the north workshop instead.',author_principal='operator')
filtered=apply_llm_request_middleware({'messages':messages},session_id=current['session'],
    task_id=current['task'],turn_id=current['turn']).payload
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==fresh_result['tool_call_id'])!=fresh_result

begin_search('erasure-reader')
erasable=seed('search-tachometer','Tachometer acceptance requires a red calibration tab.')
searched=search('Tachometer acceptance'); assert erasable in searched['source_refs'],searched
stale=dict(dispatch.last_result)
ledger.erase_sources(contact_id='person',turn_ids=[erasable['source_id']])
filtered=carry(stale)
assert 'red calibration tab' not in json.dumps(filtered),filtered
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==stale['tool_call_id'])!=stale
assert 'error' in open_original(erasable)

begin_search('identity-reader')
misattributed=seed('search-astrolabe','Astrolabe collection is scheduled for the east entrance.')
searched=search('Astrolabe collection'); assert misattributed in searched['source_refs'],searched
stale=dict(dispatch.last_result)
correct(ledger,operation_id='search-person-correction',performed_by='operator',old_contact_id='person',
    contact_id='actual-person',source_ids=[misattributed['source_id']],evidence_refs=['owner-confirmation'])
filtered=carry(stale)
assert 'east entrance' not in json.dumps(filtered),filtered
assert next(r for r in filtered['messages'] if r.get('tool_call_id')==stale['tool_call_id'])!=stale
assert 'error' in open_original(misattributed)
client.chat.completions.create.assert_not_called()
'''
    probe = probe.replace(old, check + '\n' + old)
    monkeypatch.setattr(annotation, 'PROBE', probe)
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path)
