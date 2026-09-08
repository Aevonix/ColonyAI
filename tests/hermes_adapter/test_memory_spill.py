"""Actual Hermes MemoryManager keeps selected evidence whole after attachment."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]

CHECK = r'''
import sys,os,json,socket
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.append(sys.argv[2])
if sys.argv[3]:sys.path.insert(0,sys.argv[3])
def no_network(*a,**k):raise AssertionError('Memory transfer regression is offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from colony_sidecar.setup import _prepare_hermes_config
home=Path(os.environ['HERMES_HOME']);home.mkdir(parents=True,exist_ok=True)
path=home/'config.yaml';path.write_text('{}\n')
refs=[{'source_id':'source:'+str(i)+'a'*64,'source_version':'b'*64} for i in range(8)]
payload='[colony-recall-v1 '+json.dumps({'contact_id':'fixture-owner','watermark':0,'sources':refs})+']\n'+json.dumps({'original':'original evidence '*800,'correction':'attributed correction '*600})+'\n[/colony-recall-v1]'
assert 24000<len(payload)<65536
class Provider(MemoryProvider):
    name='colony-memory'
    def initialize(self,**kwargs):pass
    def is_available(self):return True
    def get_tool_schemas(self):return []
    def prefetch(self,query,*,session_id=''):return payload
legacy=MemoryManager();legacy.add_provider(Provider())
preview=legacy.prefetch_all('Recall the original and correction',session_id='before')
assert preview!=payload and 'output truncated' in preview
assert list((home/'hook_outputs').rglob('*.txt'))
_,prepared=_prepare_hermes_config(path,'http://127.0.0.1:7777','fixture-owner')
path.write_bytes(prepared)
aligned=MemoryManager();aligned.add_provider(Provider())
result=aligned.prefetch_all('Recall the original and correction',session_id='after')
assert result==payload
assert not (home/'hook_outputs'/'after').exists()
print(json.dumps({'native_memory_manager':True,'baseline_spilled':True,'aligned_packet_exact':True,'payload_chars':len(payload),'model_calls':0,'network_calls':0}))
'''

def test_guided_config_preserves_complete_native_memory_packet(tmp_path):
    python=os.environ.get('AEVA_HERMES_TEST_PYTHON')
    if not python and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Use the qualified native Hermes interpreter')
    python=python or sys.executable
    env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
               COLONY_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',HERMES_DISABLE_LAZY_INSTALLS='1',
               HERMES_DISABLE_TELEMETRY='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=subprocess.run([python,'-I','-B','-c',CHECK,str(ROOT/'sidecar'),
        os.environ.get('COLONY_TEST_DEPENDENCY_PATH',''),os.environ.get('AEVA_HERMES_TEST_SOURCE','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['aligned_packet_exact']
