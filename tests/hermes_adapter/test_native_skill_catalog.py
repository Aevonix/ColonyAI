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
print(json.dumps({'sidecar_internal_catalog_absent':True,'actual_native_skill_index_preserved':True,
    'actual_native_skill_list_and_view':True,'model_calls':0,'network':0}))
'''


def test_native_skill_index_and_view_remain_authoritative(artifacts, tmp_path):
    native = os.environ.get('COLONY_TEST_HERMES_PATH') or os.environ.get('HERMES_TEST_SOURCE', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill catalog integration')
    env = environment(tmp_path)
    env.update(COLONY_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3], ROOT/'sidecar', native, cwd=tmp_path, env=env)
    assert '"actual_native_skill_list_and_view": true' in result.stdout
