"""Both configured names load one canonical provider through the native loader."""
import importlib.util
import os
from pathlib import Path

import pytest

from conftest import ROOT, run_python


PROBE = r'''
import importlib,json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
def blocked(*args,**kwargs):raise AssertionError('Provider-name qualification makes no network calls')
socket.socket.connect=blocked;socket.create_connection=blocked
home=Path(os.environ['HERMES_HOME']);home.mkdir()
selected=sys.argv[3];first=sys.argv[4];local_source=sys.argv[5]=='profile'
if local_source:
 import shutil
 selected_source=home/'plugins'/selected
 shutil.copytree(Path(sys.argv[6])/'plugins/apsimo-memory',selected_source)
 with (selected_source/'provider.py').open('a') as stream:stream.write('\nQUALIFIED_SOURCE_MARKER = \"selected-profile-source\"\n')
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':[]},'memory':{'provider':selected}}))
first_module=importlib.import_module(first+'.provider')
canonical=importlib.import_module('apsimo_memory.provider')
legacy=importlib.import_module('colony_memory.provider')
assert first_module is canonical is legacy
assert canonical.ApsimoMemoryProvider is canonical.ColonyMemoryProvider
from plugins import memory
found={entry.name:entry.value for entry in memory._iter_entry_points()}
assert found['apsimo-memory']=='apsimo_memory'
assert found['colony-memory']=='apsimo_memory'
from agent.memory_provider import MemoryProvider
created=[]
def counted(cls,*args,**kwargs):
 value=object.__new__(cls);created.append(value);return value
with patch.object(MemoryProvider,'__new__',staticmethod(counted)):
 provider=memory.load_memory_provider(selected,register_skills=False)
assert provider is not None and len(created)==1, (type(provider).__name__,len(created))
assert provider is created[0]
if local_source:
 selected_module=importlib.import_module(type(provider).__module__)
 assert selected_module.QUALIFIED_SOURCE_MARKER=='selected-profile-source'
 assert Path(selected_module.__file__).resolve()==(selected_source/'provider.py').resolve()
else:
 assert isinstance(provider,canonical.ApsimoMemoryProvider)
assert provider.name=='apsimo' and provider.is_available()
assert not any(name.startswith(('colony_sidecar','apsimo_sidecar')) for name in sys.modules)
provider.shutdown()
print(json.dumps({'selected':selected,'first_import':first,'provider_instances':len(created),'name':provider.name,'module_identity_shared':True,'selected_profile_source':local_source,'model_calls':0}))
'''


@pytest.mark.parametrize('source', ['entrypoint', 'profile'])
@pytest.mark.parametrize('selected', ['apsimo-memory', 'colony-memory'])
@pytest.mark.parametrize('first', ['apsimo_memory', 'colony_memory'])
def test_native_memory_aliases_load_one_provider(artifacts, tmp_path, selected, first, source):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes runtime')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path / 'user'), HERMES_HOME=str(tmp_path / 'profile'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path / 'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True')
    run_python('-I', '-B', '-c', PROBE, artifacts[3],
        os.environ.get('HERMES_TEST_SOURCE', ''), selected, first, source, ROOT, cwd=tmp_path, env=env)
