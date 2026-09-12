"""Built adapter opens actual PDF page text through the native tool boundary."""
import test_source_annotate as annotation


def test_native_document_pages_keep_exact_derivative_and_correction_lineage(artifacts, tmp_path, monkeypatch):
    probe = annotation.PROBE
    old = "def dispatch(args,*,session='later',task='review-task',turn='review-turn',call='operator-call'):"
    assert probe.count(old) == 1
    probe = probe.replace(old, old.replace("call='operator-call'", "call='operator-call',tool='pacomind_memory_annotate'"))
    old = "name='pacomind_memory_annotate',arguments=json.dumps(args)"
    assert probe.count(old) == 1
    probe = probe.replace(old, "name=tool,arguments=json.dumps(args)")
    old = "return json.JSONDecoder().raw_decode(results[0]['content'])[0]"
    assert probe.count(old) == 1
    probe = probe.replace(old, "dispatch.last_result=results[0]\n        " + old)
    marker = "print(json.dumps({'native_dispatch':True"
    assert probe.count(marker) == 1
    check = r'''
import asyncio, base64, copy, hashlib, io
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from pacomind.turns.media import SourceMedia
from agent.codex_responses_adapter import _chat_messages_to_responses_input
from agent.anthropic_message_convert import convert_messages_to_anthropic
writer=PdfWriter()
page_texts=['The first tray holds seven tiles. ' * 160, 'The second tray holds nine tiles.']
for text in page_texts:
    page=writer.add_blank_page(width=300,height=200)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
        NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 20 100 Td ('+text.encode()+b') Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream.flate_encode())
output=io.BytesIO(); writer.write(output); original=output.getvalue()
asset=hashlib.sha256(original).hexdigest(); caption='Retain the supplied tray reference PDF.'
ledger.record_source('native-document',contact_id='person',session_id='source-session',messages=[{
    'role':'user','content':[{'type':'text','text':caption},
    {'type':'input_document','input_document':{'mime_type':'application/pdf',
        'data':base64.b64encode(original).decode()}}]}],derive_claims=False)
assert asyncio.run(SourceMedia(ledger).process_one(None))
ref=ledger.source_references(['native-document'],contact_id='person',session_id='reader')[0]
recalled=provider.prefetch('supplied tray reference PDF',session_id='reader')
assert ref['source_version'] in recalled,recalled
prime('reader','reader-task','reader-turn')
# PDF text remains usable on a nonvision model. Actual attachment ingestion
# above is explicit; this test does not claim Hermes captured a channel file.
agent._model_supports_vision=lambda: False
args={**ref,'view':'document','asset_hash':asset,'page':1}
pieces=[]; results=[]; selectors=[]
for index in range(4):
    opened=dispatch(args,session='reader',task='reader-task',turn='reader-turn',
        call='document-read-'+str(index),tool='pacomind_memory_read_source')
    assert 'error' not in opened and opened['document']['status']=='complete',opened
    assert opened['document']['page']==1 and opened['document']['page_count']==2,opened
    assert opened['document']['ocr_performed'] is False,opened
    pieces.append(opened['content']); results.append(copy.deepcopy(dispatch.last_result)); selectors.append(dict(args))
    if opened['complete']: break
    args={**args,'offset':opened['next_offset'],'read_revision':opened['read_revision']}
else: raise AssertionError('Bounded PDF page did not finish')
assert len(pieces)>1
assert json.loads(''.join(pieces))['document']['text']==page_texts[0]
second=dispatch({**ref,'view':'document','asset_hash':asset,'page':2},
    session='reader',task='reader-task',turn='reader-turn',call='document-second',tool='pacomind_memory_read_source')
assert second['complete'] and json.loads(second['content'])['document']['text']==page_texts[1],second
assert 'error' in dispatch({**ref,'view':'document','asset_hash':asset,'page':3},
    session='reader',task='reader-task',turn='reader-turn',call='document-missing',tool='pacomind_memory_read_source')
messages=[{'role':'user','content':compose_user_api_content('',recalled,'')}]
for result,selector in zip(results,selectors):
    messages.extend([{'role':'assistant','content':'','tool_calls':[{
        'id':result['tool_call_id'],'type':'function','function':{
            'name':'pacomind_memory_read_source','arguments':json.dumps(selector)}}]},result])
_,anthropic=convert_messages_to_anthropic(copy.deepcopy(messages))
requests=[{'messages':messages},{'input':_chat_messages_to_responses_input(copy.deepcopy(messages))},{'messages':anthropic}]
for request in requests:
    checked=apply_llm_request_middleware(request,session_id='reader',task_id='reader-task',turn_id='reader-turn').payload
    assert 'The first tray holds seven tiles.' in json.dumps(checked),checked
annotation_note=ledger.append_source_annotation(contact_id='person',session_id='correction',
    annotation_id='document-caption-correction',**ref,excerpt=caption,
    correction='This document is a historical draft; the tray counts are not current inventory.',
    author_principal='native-operator')
for request in requests:
    checked=apply_llm_request_middleware(request,session_id='reader',task_id='reader-task',turn_id='reader-turn').payload
    assert 'The first tray holds seven tiles.' not in json.dumps(checked) and 'withheld' in json.dumps(checked),checked
fresh=dispatch({**ref,'view':'document','asset_hash':asset,'page':2},
    session='reader',task='reader-task',turn='reader-turn',call='document-corrected',tool='pacomind_memory_read_source')
assert annotation_note['source_id'] in {r['source_id'] for r in fresh['source_refs']},fresh
assert 'historical draft' in fresh['content'],fresh
corrected={'messages':[{'role':'user','content':compose_user_api_content('',recalled,'')},copy.deepcopy(dispatch.last_result)]}
ledger.erase_sources(contact_id='person',turn_ids=['native-document'])
checked=apply_llm_request_middleware(corrected,session_id='reader',task_id='reader-task',turn_id='reader-turn').payload
assert 'nine tiles' not in json.dumps(checked) and 'withheld' in json.dumps(checked),checked
assert not SourceMedia(ledger).store._original_path(asset,'application/pdf').exists()
client.chat.completions.create.assert_not_called()
'''
    monkeypatch.setattr(annotation, 'PROBE', probe.replace(marker, check + '\n' + marker))
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path)
