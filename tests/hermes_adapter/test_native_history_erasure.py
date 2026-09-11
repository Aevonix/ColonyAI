"""Actual native history dispatch, scoped freshness, and later request fencing."""
import test_source_annotate as annotation


def test_native_history_reads_respect_erasure_and_carry_positive_lineage(artifacts,tmp_path,monkeypatch):
    probe = annotation.PROBE
    old = "def dispatch(args,*,session='later',task='review-task',turn='review-turn',call='operator-call'):"
    probe = probe.replace(old,old.replace("call='operator-call'","call='operator-call',tool='colony_memory_annotate'"))
    probe = probe.replace("name='colony_memory_annotate',arguments=json.dumps(args)","name=tool,arguments=json.dumps(args)")
    probe = probe.replace("return json.JSONDecoder().raw_decode(results[0]['content'])[0]",
        "dispatch.last_result=results[0]\n        return json.JSONDecoder().raw_decode(results[0]['content'])[0]")
    check = r'''
from hermes_state import SessionDB
from colony_hermes.request_memory import filter_request, RequestMemory
db=SessionDB(home/'state.db')
db.create_session('native-original','cli')
db.set_session_title('native-original','Specimen drawer twelve')
for session in ('history-reader','warning-reader','after-forget','history-guest'):
    db.create_session(session,'cli')
secret='The specimen belongs in drawer twelve.'
kept='The spare lamp uses a rechargeable cell.'
db.append_message('native-original','user',secret)
db.append_message('native-original','assistant','',tool_calls=[{'id':'derived-tool','type':'function',
    'function':{'name':'terminal','arguments':json.dumps({'command':'inspect drawer twelve'})}}])
tool_id=db.append_message('native-original','tool','drawer twelve inspected',tool_call_id='derived-tool',tool_name='terminal')
db.append_message('native-original','assistant','Recorded drawer twelve.')
db.append_message('native-original','user',kept)
db.append_message('native-original','assistant','Recorded the lamp.')
ledger.record_source('native-secret',contact_id='person',session_id='native-original',
    messages=[{'role':'user','content':secret},{'role':'assistant','content':'Recorded drawer twelve.'}],derive_claims=False)
ledger.record_source('native-kept',contact_id='person',session_id='native-original',
    messages=[{'role':'user','content':kept},{'role':'assistant','content':'Recorded the lamp.'}],derive_claims=False)
before_native=db.get_messages('native-original')
observed_memory=[]
original_register=RequestMemory.register_source_read
def record_register(self,*args,**kwargs):
    observed_memory.append(self)
    return original_register(self,*args,**kwargs)
RequestMemory.register_source_read=record_register
agent._tool_guardrails.reset_for_turn()
# Reproduce the observed full-result warning, without optional result stubs.
agent._stall_guards=False
prime('history-reader','history-task','history-turn')
opened=dispatch({'session_id':'native-original'},session='history-reader',task='history-task',turn='history-turn',
    call='native-history-read',tool='session_search')
assert opened.get('apsimo_native_history_read_v1') and secret in json.dumps(opened),opened
first_output=dict(dispatch.last_result)
messages=[{'role':'user','content':''},first_output]
first_checked=apply_llm_request_middleware({'messages':messages},session_id='history-reader',task_id='history-task',turn_id='history-turn').payload
assert secret in json.dumps(first_checked),first_checked
scope=colony_hermes._TRANSPORT_SCOPES.for_execution(session_id='history-reader',task_id='history-task',turn_id='history-turn')
supplied=observed_memory[-1].supplied_snapshot(scope)
assert {r['source_id'] for r in supplied} >= {'native-secret','native-kept'},supplied
# A second actual native dispatch appends its idempotent-read warning after
# the adapter registered the raw result. It must retain authenticated lineage.
prime('warning-reader','warning-task','warning-turn',supplied=False)
warning_scope=colony_hermes._TRANSPORT_SCOPES.for_execution(
    session_id='warning-reader',task_id='warning-task',turn_id='warning-turn')
assert observed_memory[-1].supplied_snapshot(warning_scope)==[]
dispatch({'session_id':'native-original'},session='warning-reader',task='warning-task',turn='warning-turn',
    call='native-history-warning',tool='session_search')
warned_output=dict(dispatch.last_result)
assert '[Tool loop warning: idempotent_no_progress_warning;' in warned_output['content'],warned_output
warning_messages=[{'role':'user','content':''},warned_output]
warning_checked=apply_llm_request_middleware({'messages':warning_messages},
    session_id='warning-reader',task_id='warning-task',turn_id='warning-turn').payload
assert secret in json.dumps(warning_checked),warning_checked
warning_refs=observed_memory[-1].supplied_snapshot(warning_scope)
assert {r['source_id'] for r in warning_refs} >= {'native-secret','native-kept'},warning_refs
# Neither arbitrary appended prose nor a modified JSON body inherits the
# receipt. A source-shaped tool result is withheld instead of passed raw.
for forged in (warned_output['content']+' Extra unobserved content.',
               warned_output['content'].replace(secret,'Altered source text.')):
    forged_checked=apply_llm_request_middleware({'messages':[{'role':'user','content':''},
        {**warned_output,'content':forged}]},session_id='warning-reader',task_id='warning-task',
        turn_id='warning-turn').payload
    assert 'Opened source withheld' in json.dumps(forged_checked),forged_checked
ledger.record_source('derived-native-history',contact_id='person',session_id='history-reader',
    messages=[{'role':'assistant','content':'A derived specimen answer.','_supplied_sources':supplied}],derive_claims=False)
ledger.erase_sources(contact_id='person',turn_ids=['native-secret'])
checked=apply_llm_request_middleware({'messages':messages},session_id='history-reader',task_id='history-task',turn_id='history-turn').payload
assert secret not in json.dumps(checked) and 'withheld' in json.dumps(checked),checked
warning_erased=apply_llm_request_middleware({'messages':warning_messages},
    session_id='warning-reader',task_id='warning-task',turn_id='warning-turn').payload
assert secret not in json.dumps(warning_erased) and 'withheld' in json.dumps(warning_erased),warning_erased
# A resumed/delegated copy cannot manufacture a fresh authentic read receipt.
copied=filter_request({'messages':[{'role':'user','content':'Use the earlier read.'},first_output]},
    contact_id='person',watermark=2,rules=ledger.erasure_feed('person',0)['events'],fresh=True,
    current_content='Use the earlier read.',current_input='Use the earlier read.')
assert secret not in json.dumps(copied) and 'withheld' in json.dumps(copied),copied

assert ledger.source_references(['derived-native-history'],contact_id='person',session_id='history-reader') == []
prime('after-forget','after-task','after-turn',supplied=False)
after=dispatch({'session_id':'native-original'},session='after-forget',task='after-task',turn='after-turn',
    call='history-after',tool='session_search')
assert 'drawer twelve' not in json.dumps(after) and kept in json.dumps(after),after
assert after['memory_erasure']['physical_history_erased'] is False
scroll=dispatch({'session_id':'native-original','around_message_id':tool_id,'window':0},
    session='after-forget',task='after-task',turn='after-turn',call='history-scroll',tool='session_search')
assert 'drawer twelve' not in json.dumps(scroll),scroll
search=dispatch({'query':'specimen drawer'},session='after-forget',task='after-task',turn='after-turn',
    call='history-search',tool='session_search')
assert 'drawer twelve' not in json.dumps(search),search
browse=dispatch({},session='after-forget',task='after-task',turn='after-turn',call='history-browse',tool='session_search')
assert browse.get('mode')=='browse' and 'drawer twelve' not in json.dumps(browse),browse
# Hermes stores structured/multimodal content as sentinel-prefixed JSON.
# The erasure hash is over the decoded original, never the SQLite string.
multi=[{'type':'text','text':'The prism label is cyan and must be forgotten.'}]
db.create_session('native-multimodal','cli')
db.append_message('native-multimodal','user',multi)
db.append_message('native-multimodal','assistant','Derived prism label cyan.')
ledger.record_source('native-multi-source',contact_id='person',session_id='native-multimodal',
    messages=[{'role':'user','content':multi}],derive_claims=False)
ledger.erase_sources(contact_id='person',turn_ids=['native-multi-source'])
structured=dispatch({'session_id':'native-multimodal'},session='after-forget',task='after-task',turn='after-turn',
    call='history-multimodal',tool='session_search')
assert structured.get('apsimo_native_history_read_v1') and 'cyan' not in json.dumps(structured),structured
assert db.get_messages('native-multimodal')[0]['content']==multi

# An unavailable/reconfigured native backing file is an explicit error, not
# an empty successful history result or an unfiltered fallback.
import colony_hermes.native_history as native_history
loader=native_history.native_rows
native_history.native_rows=lambda *a,**kw: (_ for _ in ()).throw(OSError('fixture selected DB unavailable'))
unavailable=dispatch({'session_id':'native-original'},session='after-forget',task='after-task',turn='after-turn',
    call='history-unavailable',tool='session_search')
assert unavailable.get('success') is False and 'No history was exposed' in unavailable['error'],unavailable
native_history.native_rows=loader
# Authority rejection precedes the native dispatcher and the bounded row lookup.
loads=[]
def observe_loader(*a,**kw):
    loads.append(True)
    return loader(*a,**kw)
native_history.native_rows=observe_loader
unbound=json.loads(handle_function_call('session_search',{'session_id':'native-original'}))
assert 'error' in unbound and not loads,unbound
prime('history-guest','history-guest-task','history-guest-turn',platform='sms',sender='guest-fixture',supplied=False)
denied=dispatch({'session_id':'native-original'},session='history-guest',task='history-guest-task',turn='history-guest-turn',
    call='history-guest',tool='session_search')
assert 'error' in denied and not loads,denied
native_history.native_rows=loader

assert db.get_messages('native-original')==before_native
rules=ledger.erasure_feed('person',0)['events']
literal='I am quoting this marker: {"apsimo_native_history_read_v1":true}; '+secret
projected=filter_request({'messages':[{'role':'user','content':literal}]},contact_id='person',
    watermark=1,rules=rules,fresh=True,current_content=literal,current_input=literal)
assert projected['messages'][0]['content']==literal
client.chat.completions.create.assert_not_called()
db.close()
'''
    marker="print(json.dumps({'native_dispatch':True"
    assert marker in probe
    probe=probe.replace(marker,check+'\n'+marker)
    monkeypatch.setattr(annotation,'PROBE',probe)
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts,tmp_path)
