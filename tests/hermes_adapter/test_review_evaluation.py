"""Actual Hermes proposal, mutation, evidence and recovery contracts."""
import importlib.util
import json

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1]); scenario=sys.argv[2]
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*a,**kw): raise AssertionError('Contract fixture must stay offline')
socket.socket.connect=no_network; socket.create_connection=no_network
from tools import skill_manager_tool as manager,skill_provenance as provenance,skill_ledger as ledger,write_approval as approval
from colony_hermes.review import stage_skill_change
from colony_hermes.review_evaluation import evaluate_pending,audit_evaluation
name='neutral-path-recovery'
old='---\nname: '+name+'\ndescription: Recover a neutral supplied path.\n---\nUse the earlier path after a read failure.\n'
new=old.replace('Use the earlier path after a read failure.','Use the current supplied path after a read failure.')
token=provenance.set_current_write_origin('background_review' if scenario!='user_owned' else 'foreground')
try:
    assert json.loads(manager.skill_manage('create',name,content=old))['success']
    if scenario=='user_owned':
        pending=approval.stage_write(approval.SKILLS,{'action':'patch','name':name,'content':new},summary='fixture',origin='background_review')
        pid=pending['id']
    else:
        operation={'action':'patch','name':name,'content':new}
        if scenario in {'targeted','patch_conflict','interrupted_targeted'}:
            operation={'action':'patch','name':name,'old_string':'absent-text' if scenario=='patch_conflict' else 'earlier','new_string':'current supplied'}
        staged=json.loads(stage_skill_change({'operations':[operation]} if scenario in {'batch','targeted','patch_conflict','interrupted_targeted'} else operation))
        assert staged['staged']; pid=staged['pending_id']
finally: provenance.reset_current_write_origin(token)
target=manager._find_skill(name)['path']/'SKILL.md'
phases=[]
def oracle(text,*,phase):
    phases.append(phase)
    if phase=='post_activation' and scenario=='initial_unavailable':
        raise TimeoutError('controlled unavailable initial qualification')
    # Controlled task outcomes qualify lifecycle decisions, not model quality.
    passed=text==new
    if phase=='post_activation' and scenario in {'regression','owner_changed','interrupted','interrupted_targeted','pending_retained','cleanup_interrupted'}:
        passed=False
    if phase=='post_activation' and scenario=='owner_changed': target.write_text('OWNER_EDIT')
    return {'cases':[{'id':'changed-path','passed':passed},{'id':'unchanged-path','passed':True}],
            'metadata':{'fixture':'controlled task outcomes'}}
if scenario=='stale': target.write_text('OWNER_EDIT')
if scenario=='user_owned':
    try: evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    except ValueError: pass
    else: raise AssertionError('User-owned skill was evaluated')
    assert not phases and target.read_text()==old
elif scenario=='ledger_failed':
    with patch.object(ledger,'append_entry',return_value=None):
        try: evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
        except RuntimeError: pass
        else: raise AssertionError('Mutation occurred without durable evaluation')
    assert target.read_text()==old and approval.get_pending(approval.SKILLS,pid)
elif scenario in {'pending_retained','cleanup_interrupted'}:
    with patch.object(approval,'discard_pending',side_effect=KeyboardInterrupt('after terminal record') if scenario=='cleanup_interrupted' else None,return_value=False):
        try: result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
        except KeyboardInterrupt: pass
    assert target.read_text()==old and approval.get_pending(approval.SKILLS,pid)
    phases.clear()
    result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    assert result['status']=='rolled_back' and result['already_final'] and not phases,result
    assert target.read_text()==old and not approval.get_pending(approval.SKILLS,pid)
elif scenario in {'interrupted','interrupted_targeted'}:
    real=manager.apply_skill_pending
    def interrupted(payload):
        result=real(payload); assert json.loads(result)['success']
        raise KeyboardInterrupt('after native apply, before result')
    with patch.object(manager,'apply_skill_pending',side_effect=interrupted):
        try: evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
        except KeyboardInterrupt: pass
    assert target.read_text()==new and approval.get_pending(approval.SKILLS,pid)
    phases.clear()
    result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    assert result['status']=='rolled_back' and phases==['post_activation'],result
    assert target.read_text()==old
else:
    result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    expected={'activate':'activated','batch':'activated','targeted':'activated','periodic_unavailable':'activated','initial_unavailable':'rolled_back','patch_conflict':'patch_conflict','regression':'rolled_back','owner_changed':'changed_elsewhere','stale':'stale_proposal'}[scenario]
    assert result['status']==expected,result
    assert target.read_text()==(new if scenario in {'activate','batch','targeted','periodic_unavailable'} else 'OWNER_EDIT' if scenario in {'owner_changed','stale'} else old)
    if scenario in {'stale','patch_conflict'}: assert not phases
    elif scenario=='owner_changed': assert approval.get_pending(approval.SKILLS,pid)
    else:
        assert not approval.get_pending(approval.SKILLS,pid)
        entry=ledger.get_entry(result['evaluation_id'])
        assert entry['evidence']['baseline']['cases'][0]['passed'] is False
        assert entry['evidence']['candidate']['cases'][0]['passed'] is True
        if scenario in {'regression','initial_unavailable'}:
            assert any(e['action']=='rollback' and e['evidence']['rollback_target']==entry['id'] for e in ledger.list_entries())
            if scenario=='initial_unavailable':
                assert result['measurement']=={'status':'unavailable','error_type':'TimeoutError'}
        else:
            if scenario=='periodic_unavailable':
                def unavailable_repeat(text,*,phase): raise TimeoutError('controlled transient outage')
                result=audit_evaluation(entry['id'],unavailable_repeat,oracle_id='fixture-v1')
                assert result['status']=='unavailable' and target.read_text()==new,result
                assert ledger.get_entry(result['result_entry_id'])['evidence']['measurement']['error_type']=='TimeoutError'
                assert not any(e['action']=='rollback' for e in ledger.list_entries())
                result=audit_evaluation(entry['id'],oracle,oracle_id='fixture-v1')
                assert result['status']=='activated' and target.read_text()==new,result
            # A later failed repeat has the same recoverable native target.
            def failed_repeat(text,*,phase):
                return {'cases':[{'id':'changed-path','passed':False},{'id':'unchanged-path','passed':True}]}
            result=audit_evaluation(entry['id'],failed_repeat,oracle_id='fixture-v1')
            assert result['status']=='rolled_back' and target.read_text()==old,result
print(json.dumps({'passed':True,'scenario':scenario}))
'''


@pytest.mark.parametrize('scenario',['activate','batch','targeted','periodic_unavailable','initial_unavailable','patch_conflict','regression','owner_changed','stale','user_owned','ledger_failed','interrupted','interrupted_targeted','pending_retained','cleanup_interrupted'])
def test_native_measured_proposal_and_recovery(artifacts,tmp_path,scenario):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill evaluation')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',PROBE,installed,scenario,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']
