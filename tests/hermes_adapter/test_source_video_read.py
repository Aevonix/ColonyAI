"""Actual native dispatch, source decoder and SDK frame carriage without inference."""
import pytest
import test_source_annotate as annotation


@pytest.mark.parametrize('actual_backend', [False, True])
def test_native_video_frame_reaches_sdk_and_is_withheld_after_correction_and_erasure(artifacts, tmp_path, monkeypatch, actual_backend):
    probe = annotation.PROBE
    old = "httpx.Client=lambda **kw: original_client(**{**kw,'transport':httpx.MockTransport(respond)})"
    assert probe.count(old) == 1
    # Preserve a real Client type for the actual OpenAI SDK's subclass/type
    # checks, while all Colony and SDK traffic stays in controlled transports.
    probe = probe.replace(old, """class FixtureClient(original_client):
    def __init__(self,**kwargs):
        if not isinstance(kwargs.get('transport'),httpx.MockTransport):
            kwargs['transport']=httpx.MockTransport(respond)
        super().__init__(**kwargs)
httpx.Client=FixtureClient""")
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
ACTUAL_BACKEND = False
import base64, copy, hashlib, io
from PIL import Image
from openai import OpenAI
from agent.codex_responses_adapter import _chat_messages_to_responses_input
from agent.anthropic_message_convert import convert_messages_to_anthropic
picture=Image.new('RGB',(32,24),'orange'); buffer=io.BytesIO(); picture.save(buffer,format='PNG')
pixels=buffer.getvalue(); encoded=base64.b64encode(pixels).decode()
asset=hashlib.sha256(b'controlled original clip identity').hexdigest()
frame_hash=hashlib.sha256(pixels).hexdigest()
clip_block=[]
if ACTUAL_BACKEND:
    import av
    from fractions import Fraction
    output=io.BytesIO()
    with av.open(output,'w',format='mp4') as container:
        stream=container.add_stream('mpeg4',rate=4)
        stream.width=160; stream.height=120; stream.pix_fmt='yuv420p'; stream.time_base=Fraction(1,1000)
        for index,timestamp in enumerate((1000,1250,2000,2500)):
            frame=av.VideoFrame.from_image(Image.new('RGB',(160,120),('red','green','blue','yellow')[index]))
            frame.pts=timestamp; frame.time_base=Fraction(1,1000)
            for packet in stream.encode(frame): container.mux(packet)
        for packet in stream.encode(): container.mux(packet)
    clip=output.getvalue(); asset=hashlib.sha256(clip).hexdigest()
    clip_block=[{'type':'input_video','input_video':{'mime_type':'video/mp4','data':base64.b64encode(clip).decode()}}]
ledger.record_source('native-video',contact_id='person',session_id='source-session',messages=[{
    'role':'user','content':[{'type':'text','text':'Neutral selected clip reference, original asset '+asset},*clip_block]}],derive_claims=False)
ref=ledger.source_references(['native-video'],contact_id='person',session_id='reader')[0]
recalled=provider.prefetch('neutral selected clip reference',session_id='reader')
assert ref['source_version'] in recalled and asset in recalled,recalled
selector={'asset_hash':asset,'mime_type':'video/mp4','requested_ms':1000}
source={**ref,'view':'video','read_revision':'c'*64,'source_refs':[ref],'watermark':0,'complete':True,
    'content':'Attributed clip, selected frame at requested 1000 ms. Not all activity or capture wall time.',
    'video':{**selector,'actual_ms':1020.0,'frame_pts':151,'time_base':'1/50','stream_index':0,
        'origin_pts':100,'origin_time_base':'1/50',
        'decoder':'PyAV','decoder_version':'18.1.0','selection':'first_frame_at_or_after',
        'timestamp_origin':'first_decoded_frame','source_width':32,'source_height':24,
        'width':32,'height':24,'transform':'rgb24_png_no_resize','audio_processed':False},
    'image':{'asset_hash':frame_hash,'mime_type':'image/png','data_url':'data:image/png;base64,'+encoded},
    'image_bytes_included':True}
original_respond=respond; opens=[]; rechecks=[]; corrected=False
def respond(request):
    if request.url.path=='/v1/host/memory/read':
        body=json.loads(request.content)
        if body.get('source_view')=='video':
            assert body['asset_hash']==asset and body['requested_ms']==1000,body
            result=copy.deepcopy(source)
            if body.get('read_revision'):
                rechecks.append(body)
                result['video']=dict(selector); result.pop('image'); result['image_bytes_included']=False
                if corrected: result['read_revision']='d'*64
            else:
                opens.append(body)
            if not ACTUAL_BACKEND: return httpx.Response(200,json={'source':result})
    return original_respond(request)
prime('reader','reader-task','reader-turn')
agent._model_supports_vision=lambda: True
agent._provider_supports_vision_tool_messages=lambda: True
args={**ref,'view':'video','asset_hash':asset,'requested_ms':1000}
opened=dispatch(args,session='reader',task='reader-task',turn='reader-turn',call='call_video',tool='colony_memory_read_source')
if ACTUAL_BACKEND:
    actual_result=dispatch.last_result
    assert isinstance(actual_result['content'],list),actual_result
    pixels=base64.b64decode(actual_result['content'][1]['image_url']['url'].split(',')[1])
    encoded=base64.b64encode(pixels).decode(); frame_hash=hashlib.sha256(pixels).hexdigest()
    r,g,b=Image.open(io.BytesIO(pixels)).getpixel((80,60))
    assert b>240 and r<15 and g<15,(r,g,b)
assert opened['video']['asset_hash']==asset and opened['image']['asset_hash']==frame_hash!=asset,opened
if ACTUAL_BACKEND:
    assert opened['video']['actual_ms']==1000 and opened['video']['frame_pts']>opened['video']['origin_pts']!=0,opened
    video_meta=opened['video']
    relative=(video_meta['frame_pts']*Fraction(video_meta['time_base'])
        -video_meta['origin_pts']*Fraction(video_meta['origin_time_base']))*1000
    assert relative==video_meta['actual_ms']
    assert Image.open(io.BytesIO(pixels)).size==(video_meta['width'],video_meta['height'])==(160,120)
    import colony_sidecar.turns.video as decoder_module
    async def forbid_decode(*a,**kw): raise AssertionError('Metadata revalidation decoded again')
    decoder_module.decode_video=forbid_decode
else:
    assert opened['video']['actual_ms']==1020 and opened['video']['frame_pts']==151
    assert opened['video']['origin_pts']==100 and opened['video']['origin_time_base']=='1/50'
result=copy.deepcopy(dispatch.last_result)
assert isinstance(result['content'],list) and base64.b64decode(result['content'][1]['image_url']['url'].split(',')[1])==pixels
assert encoded not in result['content'][0]['text']
messages=[{'role':'user','content':compose_user_api_content('',recalled,'')},
    {'role':'assistant','content':'','tool_calls':[{'id':'call_video','type':'function','function':{
        'name':'colony_memory_read_source','arguments':json.dumps(args)}}]},result]
responses={'input':_chat_messages_to_responses_input(copy.deepcopy(messages))}
_,anthropic=convert_messages_to_anthropic(copy.deepcopy(messages))
requests=[{'messages':messages},responses,{'messages':anthropic}]
wire_requests=[]
def sdk_response(request):
    wire_requests.append(json.loads(request.content))
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':'fixture/vision',
        'choices':[{'index':0,'message':{'role':'assistant','content':'fixture result'},'finish_reason':'stop'}]})
# Real SDK serialization terminates in an in-process transport, never a model.
with httpx.Client(transport=httpx.MockTransport(sdk_response)) as sdk_http:
    sdk=OpenAI(api_key='fixture',base_url='http://sdk.fixture/v1',http_client=sdk_http,max_retries=0)
    def checked(request):
        return apply_llm_request_middleware(copy.deepcopy(request),session_id='reader',task_id='reader-task',turn_id='reader-turn').payload
    for request in requests:
        assert encoded in json.dumps(checked(request))
    sdk.chat.completions.create(model='fixture/vision',messages=checked(requests[0])['messages'])
    actual=next(row['content'] for row in wire_requests[-1]['messages']
        if row.get('role')=='tool' and row.get('tool_call_id')=='call_video')
    assert base64.b64decode(actual[1]['image_url']['url'].split(',')[1])==pixels
    actual_metadata=json.loads(actual[0]['text'])
    assert actual_metadata['video']==opened['video'] and actual_metadata['image']['asset_hash']==frame_hash
    assert len(opens)==1 and rechecks and all(r['asset_hash']==asset for r in rechecks)
    corrected=True
    if ACTUAL_BACKEND:
        ledger.append_source_annotation(contact_id='person',session_id='reader',annotation_id='clip-correction',
            **ref,excerpt='Neutral selected clip reference, original asset '+asset,
            correction='This is a generated color fixture, not an observation of the physical world.',author_principal='operator')
    for request in requests:
        stale=checked(request)
        assert encoded not in json.dumps(stale) and 'withheld' in json.dumps(stale)
    sdk.chat.completions.create(model='fixture/vision',messages=checked(requests[0])['messages'])
    assert encoded not in json.dumps(wire_requests[-1])
    ledger.erase_sources(contact_id='person',turn_ids=['native-video'])
    for request in requests:
        stale=checked(request)
        assert encoded not in json.dumps(stale) and 'withheld' in json.dumps(stale)
    sdk.chat.completions.create(model='fixture/vision',messages=checked(requests[0])['messages'])
    assert encoded not in json.dumps(wire_requests[-1]) and len(wire_requests)==3
assert len(opens)==1
client.chat.completions.create.assert_not_called()
print(json.dumps({'native_video_dispatch':True,'actual_sdk_requests':3,'actual_model_calls':0,
    'clip_hash_distinct_from_frame':True,'correction_withheld':True,'erasure_withheld':True,'actual_backend_decoder':ACTUAL_BACKEND}))
'''
    check = check.replace('ACTUAL_BACKEND = False', 'ACTUAL_BACKEND = ' + str(actual_backend))
    monkeypatch.setattr(annotation, 'PROBE', probe.replace(marker, check + '\n' + marker))
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts, tmp_path)
