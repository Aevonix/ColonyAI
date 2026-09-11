"""Only exact observed native user-tail repairs may recover a current suffix."""
import os
from conftest import run_python


def test_mismatched_or_unenriched_native_tail_cannot_restore_context(artifacts,tmp_path):
    installed=artifacts[3]
    run_python('-I','-c',r'''
import sys
sys.path.insert(0,sys.argv[1])
from colony_hermes.request_memory import _restore_current_suffix
current={'role':'user','content':'Continue the accepted task.',
 'api_content':'Continue the accepted task.\n\n<memory-context>current native suffix</memory-context>'}
tail=['Earlier instruction.','Continue the accepted task.']
for text in ('Unrelated text.','Earlier instruction.\nContinue the accepted task.',
 'Earlier instruction.\n\nContinue the accepted task.\nAn extra user-authored clause.'):
 request={'messages':[{'role':'user','content':text}]}
 restored,repair=_restore_current_suffix(request,tail,current)
 assert restored is request and repair is None
request={'messages':[{'role':'user','content':'\n\n'.join(tail)}]}
restored,repair=_restore_current_suffix(request,tail,{'role':'user','content':tail[-1]})
assert restored is request and repair is None
''',installed,cwd=tmp_path,env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ})
