"""Real installed Hermes skills remain discoverable beside sidecar context."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python
from test_native_current_work import environment


PROBE = r'''
import asyncio,json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.insert(0,sys.argv[3])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('plugins: {enabled: []}\n')
skill=home/'skills'/'manual-index';skill.mkdir(parents=True)
(skill/'SKILL.md').write_text('---\nname: manual-index\ndescription: Organize printed manuals with colored tabs.\n---\nUse one color for each manual category.\n')
def no_network(*args,**kwargs):raise AssertionError('Skill catalog test is local')
socket.socket.connect=no_network;socket.create_connection=no_network
from apsimo.api.routers import host
from apsimo.api.schemas.host import ContextAssembleRequest
from apsimo.skills.registry import SkillRegistry
from apsimo_memory.provider import ApsimoMemoryProvider
host._skills_registry=SkillRegistry()
body=ContextAssembleRequest.model_validate({'identity':{'host_id':'fixture-host'},
    'context':{'session_id':'fixture-session','contact_id':'fixture-person'},
    'incoming_message':{'role':'user','content':'How should I index the manuals?'}})
response=asyncio.run(host.context_assemble(body))
sections=[section.model_dump() for section in response.sections]
context=ApsimoMemoryProvider.__new__(ApsimoMemoryProvider)._format_sections(sections)
assert 'behavioral_correction' not in context and 'Available Skills' not in context,context
from agent.prompt_builder import build_skills_system_prompt
from tools.skills_tool import skills_list,skill_view
catalog=build_skills_system_prompt(available_tools={'skills_list','skill_view'},
    skills_dir_override=home/'skills')
assert 'manual-index' in catalog and 'Organize printed manuals with colored tabs.' in catalog,catalog
listed=json.loads(skills_list())
assert any(row['name']=='manual-index' for row in listed['skills']),listed
viewed=json.loads(skill_view('manual-index',preprocess=False))
assert viewed.get('success') and 'Use one color for each manual category.' in viewed['content'],viewed
assert 'behavioral_correction' not in catalog
missing=json.loads(skill_view('behavioral_correction',preprocess=False))
assert not missing.get('success'),missing

# Install from the built wheel through the same no-model setup used by an
# existing profile. Exercise discovery and real native tool-result delivery.
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from apsimo import setup
from apsimo.setup_skills import BUNDLE_PREFIX
from apsimo.setup_hermes import _adapter_resources
from agent.skill_utils import parse_frontmatter
assert setup.run_init(None, NS(skills_only=True, hermes_home=str(home), adapter_wheel=sys.argv[4])) == 0
bundled={name[len(BUNDLE_PREFIX):-len('/SKILL.md')]:content.decode()
    for name,content in _adapter_resources(sys.argv[4]).items()
    if name.startswith(BUNDLE_PREFIX) and name.endswith('/SKILL.md')}
assert 'apsimo-deep-research' in bundled
from agent.prompt_builder import clear_skills_system_prompt_cache
# This process already built a catalog above. An ordinary fresh process has
# an empty cache; installation intentionally does not mutate a live gateway.
clear_skills_system_prompt_cache(clear_snapshot=True)
catalog=build_skills_system_prompt(available_tools={'skills_list','skill_view'},
    skills_dir_override=home/'skills')
listed=json.loads(skills_list())
for name,source in bundled.items():
    frontmatter,body=parse_frontmatter(source)
    assert frontmatter['name']==name
    assert name in catalog and frontmatter['description'][:40] in catalog,catalog
    assert body.strip() not in catalog
    assert any(row['name']==name for row in listed['skills']),listed
    viewed=json.loads(skill_view(name,preprocess=False))
    assert viewed.get('success') and viewed['content']==source,viewed
from run_agent import AIAgent
import run_agent
target='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
total_requests=0
for name,source in sorted(bundled.items()):
    frontmatter,body=parse_frontmatter(source)
    requests=[]
    def completion(**kwargs):
        requests.append(json.loads(json.dumps(kwargs['messages'])))
        if len(requests)==1:
            system='\n'.join(str(row.get('content','')) for row in requests[-1] if row['role']=='system')
            assert name in system and frontmatter['description'][:40] in system,system
            assert body.strip() not in system
            tool=NS(id='load-research-skill',type='function',function=NS(name='skill_view',
                arguments=json.dumps({'name':name})))
            message=NS(content=None,tool_calls=[tool]); reason='tool_calls'
        else:
            assert len(requests)==2, 'Unexpected extra completion'
            result=next(row['content'] for row in requests[-1] if row.get('tool_call_id')=='load-research-skill')
            loaded=json.loads(result)
            assert loaded['success'] and loaded['content']==source,loaded
            message=NS(content='RESEARCH_SKILL_LOADED',tool_calls=None); reason='stop'
        return NS(choices=[NS(message=message,finish_reason=reason)],model='fixture/model',usage=None)
    client=MagicMock(); client.chat.completions.create.side_effect=completion
    with patch(target,return_value=client):
        agent=AIAgent(api_key='fixture',base_url='http://127.0.0.1:1/v1',provider='openai',
            model='fixture/model',max_iterations=3,quiet_mode=True,skip_context_files=True,
            skip_memory=True,platform='cli',enabled_toolsets=['skills'])
        agent._use_prompt_caching=False; agent.compression_enabled=False; agent.save_trajectories=False
        outcome=agent.run_conversation('Load the workflow '+name+'.',task_id='bundled-skill-fixture-'+name)
        assert outcome['final_response']=='RESEARCH_SKILL_LOADED',outcome
        agent.close()
    assert len(requests)==2
    total_requests+=len(requests)
print(json.dumps({'sidecar_internal_catalog_absent':True,'actual_native_skill_index_preserved':True,
    'actual_native_skill_list_and_view':True,'bundled_skill_delivered_to_native_request':True,
    'bundled_skills':sorted(bundled),'controlled_sdk_completions':total_requests,'model_calls':0,'network':0}))
'''


def test_native_skill_index_and_view_remain_authoritative(artifacts, tmp_path):
    native = os.environ.get('COLONY_TEST_HERMES_PATH') or os.environ.get('HERMES_TEST_SOURCE', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill catalog integration')
    env = environment(tmp_path)
    env.update(COLONY_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3], ROOT/'sidecar', native, artifacts[1], cwd=tmp_path, env=env)
    assert '"actual_native_skill_list_and_view": true' in result.stdout
    assert '"bundled_skill_delivered_to_native_request": true' in result.stdout
