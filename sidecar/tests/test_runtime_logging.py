"""Real server access records and process-owned rotation, without inference."""
import os
from pathlib import Path
import subprocess
import sys


def run(tmp_path, script):
    env = {key: os.environ[key] for key in ('PATH', 'LANG') if key in os.environ}
    env.update(HOME=str(tmp_path), PACOMIND_SKIP_DOTENV='1')
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);\n'+script,
        str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    return result


def test_actual_uvicorn_suppresses_only_measured_fast_successful_polls(tmp_path):
    result = run(tmp_path, r'''
import asyncio,copy,json,logging,socket,threading,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import uvicorn
from pacomind.runtime_logging import configure_runtime_logging,RequestLogTiming,RuntimeFormatter
log=Path(sys.argv[2])/'service/sidecar.log'
configure_runtime_logging(log)
async def app(scope,receive,send):
    if scope['query_string']==b'slow=1':await asyncio.sleep(1.05)
    status=503 if scope['query_string']==b'fail=1' else 200
    await send({'type':'http.response.start','status':status,'headers':[]})
    await send({'type':'http.response.body','body':b'ok'})
sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(RequestLogTiming(app),log_config=None,lifespan='off'))
thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[sock])),daemon=True)
thread.start()
try:
    deadline=time.monotonic()+5
    while not server.started and time.monotonic()<deadline:time.sleep(.01)
    assert server.started
    for path,method in [('/v1/host/health','GET'),('/v1/host/memory/sources/erasures','GET'),
        ('/v1/host/transport/ingress/receipts?ids=bounded-fixture','GET'),
        ('/v1/host/queue/jobs/pending','GET'),('/v1/host/queue/stats','GET'),
        ('/v1/host/health?slow=1','GET'),('/v1/host/health?fail=1','GET'),
        ('/v1/host/health','POST'),('/v1/host/unusual','GET')]:
        try:
            with urlopen(Request(f'http://127.0.0.1:{port}'+path,method=method),timeout=3) as response:response.read()
        except HTTPError as error:assert error.code==503
finally:
    server.should_exit=True;thread.join(5);assert not thread.is_alive();sock.close()
value=log.read_text()
assert 'GET /v1/host/health HTTP' not in value,value
for route in ('sources/erasures','ingress/receipts','queue/jobs/pending','queue/stats'):
    assert route not in value,value
for marker in ('health?slow=1','health?fail=1','POST /v1/host/health','GET /v1/host/unusual','503','elapsed_ms='):
    assert marker in value,(marker,value)
# Unknown duration, errors, redirects and unusual traffic are not guessed fast.
access=logging.getLogger('uvicorn.access')
for method,path,status in [('GET','/v1/host/health',200),
    ('GET','/v1/host/transport/ingress/receipts?ids=successful-query',200),
    ('GET','/v1/host/transport/ingress/receipts?ids=failed-query',403),
    ('GET','/v1/host/transport/ingress/receipts?ids=redirect-query',302)]:
    access.info('%s - "%s %s HTTP/%s" %d','fixture',method,path,'1.1',status)
value=log.read_text()
assert 'GET /v1/host/health HTTP/1.1" 200' in value
assert 'successful-query' not in value and '?<omitted>' in value
assert 'failed-query' in value and 'redirect-query' in value
item=logging.LogRecord('uvicorn.access',logging.INFO,__file__ if '__file__' in globals() else '',1,
    '%s %s %s %s %d',('fixture','GET','/v1/host/transport/ingress/receipts?ids=original','1.1',200),None)
before=copy.deepcopy(item.__dict__);RuntimeFormatter().format(item);assert item.__dict__==before
print('ACTUAL_UVICORN_POLL_VISIBILITY_OK')
''')
    assert 'ACTUAL_UVICORN_POLL_VISIBILITY_OK' in result.stdout


def test_real_rotation_bounds_retention_and_captures_python_output(tmp_path):
    result = run(tmp_path, r'''
import json,logging,os,threading
from pathlib import Path
from pacomind.runtime_logging import configure_runtime_logging,runtime_log_directory,MAX_RECORD_CHARS
path=Path(sys.argv[2])/'logs/sidecar.log'
handler=configure_runtime_logging(path,max_bytes=1024,backups=2,redirect_stdio=True)
assert configure_runtime_logging(path,max_bytes=1024,backups=2,redirect_stdio=True) is handler
assert runtime_log_directory()==path.parent
for n in range(30):logging.getLogger('fixture').info('rotation record %s %s',n,'x'*500)
assert len(list(path.parent.glob('sidecar.log.[0-9]*')))==2
assert all(p.stat().st_size<=1024 for p in path.parent.glob('sidecar.log*') if p.suffix!='.json')
print('python stdout observed')
sys.stderr.write('python stderr observed\n')
threads=[threading.Thread(target=lambda:print('thread stdout observed')) for _ in range(3)]
for thread in threads:thread.start()
for thread in threads:thread.join()
sys.stdout.write('partial stdout observed');sys.stdout.flush()
try:raise ValueError('failure remains visible')
except ValueError:logging.getLogger('fixture').exception('diagnostic failure')
value=''.join(p.read_text() for p in path.parent.glob('sidecar.log*') if p.suffix!='.json')
for marker in ('python stdout observed','python stderr observed','thread stdout observed','partial stdout observed','Traceback','failure remains visible'):
    assert marker in value,marker
# Huge records remain bounded independently of the caller's request size.
logging.getLogger('fixture').info('z'*(MAX_RECORD_CHARS*4))
assert max(p.stat().st_size for p in path.parent.glob('sidecar.log*') if p.suffix!='.json') < MAX_RECORD_CHARS+256
sys.stdout.write('p'*(MAX_RECORD_CHARS*3));sys.stdout.flush()
assert len(sys.stdout.pending)==0
assert len(list(path.parent.glob('sidecar.log.[0-9]*')))==2
logging.getLogger('fixture').error('current final marker')
assert 'current final marker' in path.read_text()
policy=json.loads(path.with_name(path.name+'.runtime.json').read_text())
assert policy['handler']=='logging.handlers.RotatingFileHandler'
assert policy['writer_pid']==os.getpid() and policy['path']==str(path)
assert policy['max_bytes']==1024 and policy['backup_count']==2 and policy['python_stdio'] is True
assert path.stat().st_mode & 0o777==0o600
assert path.with_name(path.name+'.runtime.json').stat().st_mode & 0o777==0o600
sys.__stdout__.write('ACTUAL_ROTATION_STDIO_POLICY_OK\n')
''')
    assert 'ACTUAL_ROTATION_STDIO_POLICY_OK' in result.stdout
