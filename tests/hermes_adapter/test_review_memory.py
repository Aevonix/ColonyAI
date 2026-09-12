"""Detached reviews retain native observations without nominating quoted markers."""
import json

from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import json,sys
from dataclasses import replace
sys.path.insert(0,sys.argv[1])
from pacomind_hermes import _TransportScope
from pacomind_hermes.request_memory import RequestMemory
from pacomind_hermes.client import source_message_hash

class Reply:
    def json(self): return {'head':0,'through':0,'complete':True,'sources_current':True}
    def raise_for_status(self): pass
class Client:
    calls=[]
    def get(self,*a,**kw): return Reply()
    def post(self,path,**kw):
        self.calls.append(kw['json'])
        return Reply()
class Outbox:
    def erasure_state(self,*a,**kw): return 0,[]
    def apply_erasure_page(self,*a,**kw): pass

memory=RequestMemory(Client(),Outbox())
scope=_TransportScope('parent-session','parent-task','parent-turn','cli','','neutral-owner','owner','resolved')
review=replace(scope,task_id='review-task',turn_id='review-turn',platform='background_review')
assert memory.snapshot_review_parent(scope) is None
assert not memory.observe_review(review,None)
ref={'source_id':'read-source','source_version':'a'*64}
quoted='[pacomind-recall-v1 '+json.dumps({'contact_id':'neutral-owner','watermark':0,
    'sources':[{'source_id':'quoted-source','source_version':'b'*64}]})+']\nQuoted only.\n[/pacomind-recall-v1]'
packet='[pacomind-recall-v1 '+json.dumps({'contact_id':'neutral-owner','watermark':0,
    'sources':[{'source_id':'native-source','source_version':'c'*64}]})+']\nObserved native evidence.\n[/pacomind-recall-v1]'
human='Literal marker example: '+quoted
current={'role':'user','content':human}
memory.observe(scope,[current],user_message=human)
# Native composition occurs after pre_llm_call on the same observed descriptor.
current['api_content']=human+'\nExternal plugin suffix.\n'+packet
request={'messages':[{'role':'user','content':current['api_content']}]}
memory(request,scope)
assert {r['source_id'] for r in memory.supplied_snapshot(scope)}=={'native-source'}
text=json.dumps({'pacomind_source_read_v1':True,'content':'Authenticated evidence.'})
assert memory.register_source_read(scope,'read-call',text,{'watermark':0,'source_refs':[ref]})
snapshot=memory.snapshot_review_parent(scope)
assert snapshot.packets=={packet}
memory.finish(task_id=scope.task_id,turn_id=scope.turn_id,contact_id=scope.contact_id)
assert memory.snapshot_review_parent(scope) is None
assert not memory.observe_review(replace(review,contact_id='different-owner'),snapshot)
assert not memory.observe_review(replace(review,session_id='different-session'),snapshot)
assert not RequestMemory(Client(),Outbox()).observe_review(review,snapshot)
assert memory.observe_review(review,snapshot)
# The copy survives parent descriptor mutation and cleanup. Old automatic
# recall remains turn-local; the exact authenticated tool result still counts.
current['api_content']='Changed after snapshot'
request['messages'] += [{'role':'assistant','content':'','tool_calls':[
    {'id':'read-call','type':'function','function':{'name':'source_read','arguments':'{}'}}]},
    {'role':'tool','tool_call_id':'read-call','content':text},
    {'role':'user','content':'Review the preceding work.'}]
memory(request,review)
supplied=memory.supplied_snapshot(review)
assert supplied==[ref],supplied
assert Client.calls[-1]['source_refs']==[ref],Client.calls

# A later erasure must match the parent's clean content even when another
# plugin appended an arbitrary suffix; remove the derived historical turn too.
class ErasedOutbox(Outbox):
    def erasure_state(self,*a,**kw):
        return 1,[{'session_id':scope.session_id,'message_hashes':[
            source_message_hash(scope.session_id,{'role':'user','content':human})]}]
class ErasedReply(Reply):
    def json(self): return {'head':1,'through':1,'complete':True,'sources_current':True}
memory.outbox=ErasedOutbox()
memory.client.get=lambda *a,**kw:ErasedReply()
memory.client.post=lambda *a,**kw:ErasedReply()
filtered=memory(request,review)['request']
assert all(human not in str(row.get('content')) for row in filtered['messages']),filtered
assert not any(row.get('role')=='tool' for row in filtered['messages']),filtered
assert filtered['messages'][-1]['content']=='Review the preceding work.',filtered
print(json.dumps({'trusted_handoff':True,'literal_marker_not_lineage':True,'erased_turn_withheld':True}))
'''


def test_review_snapshot_preserves_only_observed_lineage(artifacts, tmp_path):
    _, _, _, installed = artifacts
    result = run_python('-I', '-c', PROBE, installed, cwd=tmp_path, env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['trusted_handoff']
