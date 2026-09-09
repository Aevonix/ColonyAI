"""Exercise source opening through the installed native dispatcher and middleware."""
import test_source_annotate as annotation


def test_native_source_reader_pages_and_reconciles_actual_tool_outputs(artifacts, tmp_path, monkeypatch):
    probe = annotation.PROBE
    old = "def dispatch(args,*,session='later',task='review-task',turn='review-turn',call='operator-call'):"
    assert probe.count(old) == 1
    probe = probe.replace(old, old.replace("call='operator-call'", "call='operator-call',tool='colony_memory_annotate'"))
    old = "name='colony_memory_annotate',arguments=json.dumps(args)"
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
    messages=[{'role':'user','content':long_text}],derive_claims=False)
long_ref=ledger.source_references(['long-native'],contact_id='person',session_id='reader')[0]
recalled=provider.prefetch('Pump procedure',session_id='reader')
assert long_ref['source_version'] in recalled
ref=long_ref
prime('reader','reader-task','reader-turn')
messages=[{'role':'user','content':compose_user_api_content('',recalled,'')}]
args=dict(long_ref); pages=[]; count=0
while True:
    opened=dispatch(args,session='reader',task='reader-task',turn='reader-turn',
                    call='source-read-'+str(count),tool='colony_memory_read_source')
    assert 'error' not in opened,opened
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
