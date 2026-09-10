"""Installed plugin: owned original bytes reach native vision tool input parts."""
import test_source_annotate as annotation


def test_native_owned_pixels_reopen_on_supported_protocols_and_erase(artifacts, tmp_path, monkeypatch):
    probe = annotation.PROBE
    old = "def dispatch(args,*,session='later',task='review-task',turn='review-turn',call='operator-call'):"
    assert probe.count(old) == 1
    probe = probe.replace(old, old.replace("call='operator-call'", "call='operator-call',tool='colony_memory_annotate'"))
    old = "name='colony_memory_annotate',arguments=json.dumps(args)"
    assert probe.count(old) == 1
    probe = probe.replace(old, 'name=tool,arguments=json.dumps(args)')
    old = "return json.JSONDecoder().raw_decode(results[0]['content'])[0]"
    assert probe.count(old) == 1
    probe = probe.replace(old, "dispatch.last_result=results[0]\n        value=results[0]['content']\n        "
        "return json.JSONDecoder().raw_decode(value[0]['text'] if isinstance(value,list) else value)[0]")
    marker = "print(json.dumps({'native_dispatch':True"
    assert probe.count(marker) == 1
    check = r'''
import base64, copy, hashlib, io
from PIL import Image, ImageDraw
from agent.codex_responses_adapter import _chat_messages_to_responses_input
from agent.anthropic_message_convert import convert_messages_to_anthropic
picture=Image.new('RGB',(96,48),'white'); draw=ImageDraw.Draw(picture)
draw.rectangle((5,5,30,35),fill='red'); draw.ellipse((55,5,80,30),fill='blue')
buffer=io.BytesIO(); picture.save(buffer,format='PNG'); original=buffer.getvalue()
encoded=base64.b64encode(original).decode(); asset=hashlib.sha256(original).hexdigest()
ledger.record_source('native-image',contact_id='person',session_id='source-session',messages=[{
    'role':'user','content':[{'type':'text','text':'Retain this neutral geometry reference image.'},
    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+encoded}}]}],derive_claims=False)
ref=ledger.source_references(['native-image'],contact_id='person',session_id='reader')[0]
recalled=provider.prefetch('neutral geometry reference image',session_id='reader')
assert ref['source_version'] in recalled,recalled
prime('reader','reader-task','reader-turn')
# Controlled active-model capability declarations; no provider inference call.
# Actual native dispatch still owns normalization and vision tool gating.
agent._model_supports_vision=lambda: True
agent._provider_supports_vision_tool_messages=lambda: True
args={**ref,'view':'image','asset_hash':asset}
opened=dispatch(args,session='reader',task='reader-task',turn='reader-turn',
                call='call_image',tool='colony_memory_read_source')
assert opened['image_bytes_included'] is True and opened['image']['asset_hash']==asset,opened
result=copy.deepcopy(dispatch.last_result)
assert isinstance(result['content'],list) and len(result['content'])==2,result
assert encoded not in result['content'][0]['text']
assert base64.b64decode(result['content'][1]['image_url']['url'].split(',')[1])==original
messages=[{'role':'user','content':compose_user_api_content('',recalled,'')},
    {'role':'assistant','content':'','tool_calls':[{'id':'call_image','type':'function','function':{
        'name':'colony_memory_read_source','arguments':json.dumps(args)}}]},result]
responses={'input':_chat_messages_to_responses_input(copy.deepcopy(messages))}
_,anthropic=convert_messages_to_anthropic(copy.deepcopy(messages))
requests=[{'messages':messages},responses,{'messages':anthropic}]
for request in requests:
    before=copy.deepcopy(request)
    checked=apply_llm_request_middleware(request,session_id='reader',task_id='reader-task',turn_id='reader-turn').payload
    assert encoded in json.dumps(checked),checked
    assert request==before
    # The native conversion keeps pixels in provider-supported parts, never
    # just a JSON caption containing a base64 string.
assert responses['input'][-1]['output'][1]['type']=='input_image'
anthropic_result=next(p for row in anthropic if isinstance(row.get('content'),list)
    for p in row['content'] if p.get('type')=='tool_result')
assert anthropic_result['content'][1]['type']=='image'
assert base64.b64decode(anthropic_result['content'][1]['source']['data'])==original
agent._model_supports_vision=lambda: False
nonvision=dispatch(args,session='reader',task='reader-task',turn='reader-turn',
                   call='call_no_vision',tool='colony_memory_read_source')
assert nonvision['complete'] is False and nonvision['image_bytes_included'] is False,nonvision
assert 'No visual inspection occurred' in nonvision['error']
agent._model_supports_vision=lambda: True
agent._provider_supports_vision_tool_messages=lambda: False
unsupported=dispatch(args,session='reader',task='reader-task',turn='reader-turn',
                     call='call_no_parts',tool='colony_memory_read_source')
assert unsupported['image_bytes_included'] is False and 'error' in unsupported,unsupported
ledger.erase_sources(contact_id='person',turn_ids=['native-image'])
for request in requests:
    checked=apply_llm_request_middleware(request,session_id='reader',task_id='reader-task',turn_id='reader-turn').payload
    assert encoded not in json.dumps(checked) and 'withheld' in json.dumps(checked),checked
client.chat.completions.create.assert_not_called()
'''
    monkeypatch.setattr(annotation, 'PROBE', probe.replace(marker, check+'\n'+marker))
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path)
