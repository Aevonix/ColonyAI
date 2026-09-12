"""Exercise source opening through the installed native dispatcher and middleware."""
import test_source_annotate as annotation


def test_native_source_reader_pages_and_reconciles_actual_tool_outputs(artifacts, tmp_path, monkeypatch):
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
long_text='Pump procedure: isolate pressure. ' + 'Inspect the seal. '*500 + 'Only then reconnect power.'
ledger.record_source('long-native',contact_id='person',session_id='source-session',
    messages=[{'role':'user','content':long_text}],occurred_at='2024-02-03T04:05:06+00:00',derive_claims=False)
long_ref=ledger.source_references(['long-native'],contact_id='person',session_id='reader')[0]
recalled=provider.prefetch('Pump procedure',session_id='reader')
assert long_ref['source_version'] in recalled
ref=long_ref
prime('reader','reader-task','reader-turn')
messages=[{'role':'user','content':compose_user_api_content('',recalled,'')}]
args=dict(long_ref); pages=[]; count=0
while True:
    opened=dispatch(args,session='reader',task='reader-task',turn='reader-turn',
                    call='source-read-'+str(count),tool='apsimo_memory_read_source')
    assert 'error' not in opened,opened
    assert opened['reported_at']=='2024-02-03T04:05:06+00:00'
    assert opened['recorded_at']!=opened['reported_at'] and opened['evidence_basis']=='retained_record'
    assert 'does not re-inspect its underlying subject' in opened['guidance']
    result=dict(dispatch.last_result)
    messages.append(result)
    checked=apply_llm_request_middleware({'messages':messages},session_id='reader',
        task_id='reader-task',turn_id='reader-turn').payload
    supplied=next(row for row in checked['messages'] if row.get('tool_call_id')==result['tool_call_id'])
    assert supplied==result,checked
    pages.append(opened['content']); count+=1
    if opened['complete']: break
    assert count<8
    args={**long_ref,'offset':opened['next_offset'],'read_revision':opened['read_revision']}
assert count>1 and json.loads(''.join(pages))['messages'][0]['content']==long_text
ledger.erase_sources(contact_id='person',turn_ids=['long-native'])
checked=apply_llm_request_middleware({'messages':messages},session_id='reader',
    task_id='reader-task',turn_id='reader-turn').payload
assert 'Inspect the seal' not in json.dumps(checked) and 'withheld' in json.dumps(checked),checked
client.chat.completions.create.assert_not_called()
'''
    probe = probe.replace(old, check + '\n' + old)
    monkeypatch.setattr(annotation, 'PROBE', probe)
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path)


def test_native_observation_directory_admits_only_returned_refs_and_opens_current_evidence(artifacts, tmp_path, monkeypatch):
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
from apsimo.turns.tool_observations import ToolObservation, identity_id, record
from apsimo.turns.source_attribution import correct
import hashlib
instruction='Inspect the Corvus export bundle and resolve its checksum before marking it ready.'
ledger.record_source('corvus-task',contact_id='person',session_id='original',
    messages=[{'role':'user','content':instruction}],derive_claims=False)
origin=ledger.source_references(['corvus-task'],contact_id='person',session_id='reader')[0]
# Prime ordinary recall before retaining results, so their references have not
# been supplied. The new view must provide actual reference discovery.
recalled=provider.prefetch('Corvus export bundle',session_id='reader');ref=origin
prime('reader','reader-task','reader-turn')
messages=[{'role':'user','content':compose_user_api_content('',recalled,'')}]
def retain(number,content,reason,parent=origin,sources=()):
    native=dict(profile_id='a'*64,session_id='original',task_id='inspection',turn_id='original-turn',
        tool_call_id='original-call-'+str(number),api_request_id='original-request',tool_name='terminal',
        message_id=number,timestamp=1234567890.0+number,result_sha256=hashlib.sha256(content.encode()).hexdigest())
    sid=identity_id(native)
    record(ledger,ToolObservation(native=native,content=content,reason=reason,origin=parent,sources=list(sources)),
        contact_id='person',session_id='original',source_id=sid)
    return ledger.source_references([sid],contact_id='person',session_id='reader')[0]
weather=retain(1,'Weather station: light rain.','The checksum was repaired and the bundle is ready.')
checksum=retain(2,'Checksum mismatch; exit 1; files modified 0.','Retain the observed inspection failure.')
ledger.record_source('unrelated-task',contact_id='person',session_id='original',
    messages=[{'role':'user','content':'Read the weather file.'}],derive_claims=False)
other=ledger.source_references(['unrelated-task'],contact_id='person',session_id='reader')[0]
unrelated=retain(3,'Unrelated original call.','Corvus result.',parent=other,sources=[origin])
count=0
def open_source(selector):
    global count
    count+=1
    return dispatch(selector,session='reader',task='reader-task',turn='reader-turn',
        call='opening-'+str(count),tool='apsimo_memory_read_source')
def carry():
    result=dict(dispatch.last_result);messages.append(result)
    checked=apply_llm_request_middleware({'messages':messages},session_id='reader',
        task_id='reader-task',turn_id='reader-turn').payload
    assert next(row for row in checked['messages'] if row.get('tool_call_id')==result['tool_call_id'])==result,checked
    return checked
assert 'supplied' in open_source(checksum)['error']
directory=open_source({**origin,'view':'observations'})
assert 'error' not in directory,directory
entries=json.loads(directory['content'])['observations']
assert directory['reported_at'] is None and directory['evidence_basis']=='retained_record'
from datetime import datetime,timezone
assert next(r for r in entries if r['source_id']==checksum['source_id'])['observed_at']==datetime.fromtimestamp(1234567892.0,timezone.utc).isoformat()
assert {r['source_id'] for r in entries}=={weather['source_id'],checksum['source_id']},entries
assert 'files modified 0' not in directory['content'] and 'light rain' not in directory['content']
assert next(r for r in entries if r['source_id']==weather['source_id'])['selection_reason']['author']=='model'
assert unrelated not in directory['source_refs']
carry()
actual_weather=open_source(weather)
assert 'light rain' in actual_weather['content'] and 'files modified 0' not in actual_weather['content']
carry()
actual=open_source(checksum)
assert 'files modified 0' in actual['content'];carry()
# A later annotation of the original instruction travels on an independent
# result open, not just through the earlier directory's origin reference.
note=ledger.append_source_annotation(contact_id='person',session_id='reader',annotation_id='parent-note',
    **origin,excerpt='Corvus export bundle',correction='Only the staging bundle was requested.',author_principal='operator')
actual=open_source(checksum)
assert 'staging bundle' in actual['content'],actual
assert {key:note[key] for key in ('source_id','source_version')} in actual['source_refs']
carry()
# Genuine listing receipts are subject to the existing erasure check before
# the next native request, even when HTTP already returned successfully.
directory=open_source({**origin,'view':'observations'})
assert 'error' not in directory,directory
listed_result=dict(dispatch.last_result);messages.append(listed_result)
ledger.erase_sources(contact_id='person',turn_ids=[weather['source_id']])
checked=apply_llm_request_middleware({'messages':messages},session_id='reader',
    task_id='reader-task',turn_id='reader-turn').payload
assert 'light rain' not in json.dumps(checked)
assert next(row for row in checked['messages'] if row.get('tool_call_id')==listed_result['tool_call_id'])!=listed_result
correct(ledger,operation_id='native-person-correction',performed_by='operator',old_contact_id='person',
    contact_id='actual-person',source_ids=['corvus-task'],evidence_refs=['owner-confirmation'])
assert not ledger.source_references([checksum['source_id']],contact_id='person',session_id='reader')
assert 'error' in open_source(checksum)
checked=apply_llm_request_middleware({'messages':messages},session_id='reader',
    task_id='reader-task',turn_id='reader-turn').payload
assert 'files modified 0' not in json.dumps(checked),checked
client.chat.completions.create.assert_not_called()
'''
    probe = probe.replace(old, check + '\n' + old)
    monkeypatch.setattr(annotation, 'PROBE', probe)
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path)
