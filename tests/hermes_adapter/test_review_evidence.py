"""Actual request result references, isolated from model-proposed provenance."""
from conftest import run_python


PROBE = r'''
import copy,hashlib,json,sys
from contextvars import copy_context
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from colony_hermes.review_evidence import capture,current
scope=NS(valid_participant=True,authority_lane='owner',platform='cli',session_id='session-a',turn_id='turn-a')
raw=json.dumps({'error':'regex parse error: look-around is not supported'})
messages=[{'role':'assistant','tool_calls':[{'id':'call-a','function':{'name':'search_files','arguments':'{}'}}]},
          {'role':'tool','tool_call_id':'call-a','content':raw}]
capture(scope,{'messages':messages})
actual=current();assert actual['session_id']=='session-a'
assert actual['source']=='native_request_tool_results'
assert actual['failures']==[{'tool_call_id':'call-a','tool_name':'search_files',
    'request_visible_result_sha256':hashlib.sha256(raw.encode()).hexdigest(),'error_class':'unsupported_regex_features'}]
assert raw not in json.dumps(actual)
fork=copy_context()
scope.session_id='session-b';scope.turn_id='turn-b'
capture(scope,{'messages':[]});assert current() is None
assert fork.run(current)==actual
snapshot=fork.run(current);snapshot['failures'].clear();assert fork.run(current)==actual
# A user message or a tool result with no native assistant call is not evidence.
for request in [
    {'messages':[{'role':'user','content':raw}]},
    {'messages':[messages[-1]]},
    {'messages':[messages[0],{'role':'tool','tool_call_id':'unmatched','content':raw}]},
    {'messages':[messages[0],{'role':'tool','tool_call_id':'call-a','content':json.dumps({'content':'error: not supported'})}]},
]:
    capture(scope,request);assert current() is None
capture(None,{'messages':messages});assert current() is None
guest=NS(**{**vars(scope),'authority_lane':'guest'})
capture(guest,{'messages':messages});assert current() is None
# Responses uses the same linked call/result contract, without prompt parsing.
capture(scope,{'input':[{'type':'function_call','call_id':'call-a','name':'search_files'},
    {'type':'function_call_output','call_id':'call-a','output':raw}]})
assert current()['failures']==actual['failures']
# A replayed result counts once; bounded evidence cannot retain a transcript.
capture(scope,{'messages':messages+[messages[-1]]*40})
assert len(current()['failures'])==1
print('REQUEST_VISIBLE_REVIEW_EVIDENCE_OK')
'''


def test_packaged_review_evidence_is_linked_and_context_scoped(artifacts,tmp_path):
    _,_,_,installed=artifacts
    result=run_python('-c',PROBE,installed,cwd=tmp_path)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'REQUEST_VISIBLE_REVIEW_EVIDENCE_OK' in result.stdout
