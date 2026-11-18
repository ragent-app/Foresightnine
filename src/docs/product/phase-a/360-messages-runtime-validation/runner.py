"""One-shot isolated research acceptance. No external request during preflight/freeze."""
from __future__ import annotations
import hashlib, json, os, secrets, shutil, socket, subprocess, sys, tempfile, time
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
API = ROOT/'src/apps/api'
BASE = 'https://api.z.ai/api/anthropic'
MODEL = 'glm-5.3-flash'
QUESTION = ('Using only the supplied Microsoft FY2026 annual-report revenue facts, compare total revenue for the years ended June 30, 2026 and June 30, 2025. Give amounts, currency and periods; use the deterministic calculation tool to calculate year-over-year growth, show inputs and formula, cite the supporting source, and explain limitations.')

def sha(b): return hashlib.sha256(b).hexdigest()
def inputs():
    return sorted([*API.joinpath('app').rglob('*.py'), HERE/'runner.py', HERE/'isolated_api.py', HERE/'source.txt', HERE/'expected.json', HERE/'preregistration.md'])
def hashes(): return {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in inputs()}
def write(path, data, exclusive=False):
    flags = os.O_WRONLY|os.O_CREAT|(os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump(data, f, ensure_ascii=False, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    d = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(d)
    finally: os.close(d)
def preflight():
    missing = []
    if not os.environ.get('FORESIGHTNINE_RESEARCH_LLM_API_KEY','').strip(): missing.append('FORESIGHTNINE_RESEARCH_LLM_API_KEY')
    if missing: raise RuntimeError('Missing dedicated configuration: '+', '.join(missing))
    if (HERE/'run-started.json').exists(): raise RuntimeError('Already started; no replay')
    expected = json.loads((HERE/'expected.json').read_text())
    value = (Decimal('331839')-Decimal('281724'))/Decimal('281724')*100
    if str(value) != expected['decimal_percent']: raise RuntimeError('Independent calculation mismatch')
    return {'protocol':'anthropic_messages','base_url':BASE,'model':MODEL,'version':'2023-06-01','auth_scheme':'bearer','native_tools':True,'continuation':True,'timeout_seconds':120,'max_output_tokens':4096,'credential_revision':'acceptance-360-isolated-v1','question_sha256':sha(QUESTION.encode())}

def request(base, method, path, body=None, token=None, key=None, experiment_token=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    if key: headers['Idempotency-Key']=key
    if experiment_token: headers['X-Experiment-Token']=experiment_token
    req=Request(base+path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
    try:
        with urlopen(req, timeout=15) as response: return response.status, json.load(response)
    except HTTPError as e: return e.code, {}
    except (URLError, OSError, ValueError): return None, {}

def main():
    config=preflight()
    frozen=json.loads((HERE/'freeze.json').read_text())
    if frozen != {'configuration':config, 'sha256':hashes()}: raise RuntimeError('Freeze mismatch; no call submitted')
    env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR'}}
    env.update(FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL='anthropic_messages',FORESIGHTNINE_RESEARCH_LLM_BASE_URL=BASE,FORESIGHTNINE_RESEARCH_LLM_MODEL=MODEL,FORESIGHTNINE_RESEARCH_LLM_API_KEY=os.environ['FORESIGHTNINE_RESEARCH_LLM_API_KEY'],FORESIGHTNINE_RESEARCH_LLM_ALLOWED_HOSTS='api.z.ai',FORESIGHTNINE_RESEARCH_LLM_CREDENTIAL_REVISION=config['credential_revision'],FORESIGHTNINE_RESEARCH_LLM_TIMEOUT_SECONDS='120',FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS='4096',FORESIGHTNINE_MESSAGES_VERSION='2023-06-01',FORESIGHTNINE_MESSAGES_AUTH_SCHEME='bearer',FORESIGHTNINE_MESSAGES_NATIVE_TOOLS='true',FORESIGHTNINE_MESSAGES_CONTINUATION='true',FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL=BASE,FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL=MODEL,FORESIGHTNINE_MESSAGES_CAPABILITY_VERSION='2023-06-01',FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_ABUSE_MODE='memory',FORESIGHTNINE_SEARCH_PROVIDER='client',FORESIGHTNINE_LOCAL_AUTH_MODE='mock',PYTHONPATH=str(API))
    experiment_token=secrets.token_urlsafe(32)
    env.update(FORESIGHTNINE_360_ISOLATED='1', FORESIGHTNINE_360_TOKEN=experiment_token)
    directory=Path(tempfile.mkdtemp(prefix='foresightnine-360-'))
    with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    log=os.open(directory/'server.log',os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
    server=subprocess.Popen([sys.executable,str(HERE/'isolated_api.py'),str(port)],cwd=directory,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    os.close(log)
    active = False
    try:
        for _ in range(100):
            status,_=request(base,'GET','/health')
            if status is not None: break
            if server.poll() is not None: raise RuntimeError('API failed before admission')
            time.sleep(.1)
        else: raise RuntimeError('API not ready before admission')
        status,auth=request(base,'POST','/v1/auth/local/register',{'email':f'acceptance-{uuid4().hex}@example.test','password':secrets.token_urlsafe(32)})
        if status != 201: raise RuntimeError('Registration failed before admission')
        token=auth['access_token']
        status,source=request(base,'POST','/v2/research/sources',{'title':'Microsoft FY2026 revenue facts, derived from annual report','text':(HERE/'source.txt').read_text(),'url':'https://www.microsoft.com/investor/reports/ar26/','kind':'user_supplied','permissions':{'store':True,'display':True,'model_input':True,'export':True}},token)
        if status != 201: raise RuntimeError('Source failed before admission')
        sid=source['source_id']
        status,topic=request(base,'POST','/v2/research/topics',{'question':QUESTION,'source_ids':[sid]},token)
        if status != 201: raise RuntimeError('Topic failed before admission')
        path='/v2/research/topics/'+topic['topic_id']
        control={'base':base,'token':token,'topic_id':topic['topic_id'],'pid':server.pid,'operation_id':None,'experiment_token':experiment_token}
        write(directory/'control.json',control)
        payload={'parent_version_id':topic['versions'][0]['version_id'],'selected_source_ids':[sid],'discover_sources':False,'auto_fetch_official':False,'max_search_calls':0}
        write(HERE/'run-started.json',{'freeze_sha256':sha((HERE/'freeze.json').read_bytes()),'control_dir':str(directory),'state':'admission_pending','payload_sha256':sha(json.dumps(payload,sort_keys=True).encode())},True)
        active = True  # Submission can become ambiguous; never retry.
        status,receipt=request(base,'POST',path+'/runs',payload,token,'acceptance-360-'+uuid4().hex)
        operation=(receipt.get('operation') or {}).get('operation_id')
        if status != 202 or not operation:
            write(HERE/'result.json',{'state':'admission_unknown','http_status':status}); return
        control['operation_id']=operation; write(directory/'control.json',control)
        while True:
            status,data=request(base,'GET',path+'/runs/'+operation,token=token)
            if status != 200: write(HERE/'result.json',{'state':'observation_unknown'}); return
            state=(data.get('operation') or {}).get('status')
            if state in {'completed','failed','cancelled'}: break
            time.sleep(2)
        status,settlement=request(base,'GET',path+'/runs/'+operation+'/settlement',token=token)
        pstatus,progress=request(base,'GET',path+'/runs/'+operation+'/progress',token=token)
        dstatus,diagnostics=request(base,'GET','/_360/diagnostics/'+operation,experiment_token=experiment_token)
        result={'diagnostic_http_status':dstatus,'diagnostics':diagnostics if dstatus==200 else None,'state':state,'settlement_http_status':status,'progress_http_status':pstatus,'settlement':settlement if status==200 else None,'progress':progress if pstatus==200 else None,'quality':'not_reviewed'}
        if status != 200 or pstatus != 200 or dstatus != 200: result['state']='reconciliation_unknown'
        version = progress.get('version_id') if pstatus == 200 else None
        if version:
            export_status, exported = request(base,'GET',path+'/versions/'+version+'/export',token=token)
            result['export_http_status'] = export_status
            if export_status == 200: write(HERE/'authorized-export.json',exported)
        write(HERE/'result.json',result)
        ledger=settlement.get('model_ledger',{}) if status==200 else {}
        if (result['state'] in {'completed','failed','cancelled'} and dstatus==200 and diagnostics.get('receipts_settled') and not diagnostics.get('pending') and ledger.get('reserved_calls')==0 and (not version or result.get('export_http_status')==200)): active=False
    finally:
        if not active:
            server.terminate()
            server.wait(timeout=10)
            shutil.rmtree(directory)
    # Retain isolated server and control directory for read-only reconciliation.

if __name__=='__main__':
    try:
        if sys.argv[1:]==['preflight']: print(json.dumps({'ready':True,'configuration':preflight()}))
        elif sys.argv[1:]==['freeze']: write(HERE/'freeze.json',{'configuration':preflight(),'sha256':hashes()},True); print('Frozen; no model request')
        elif sys.argv[1:]: raise RuntimeError('Use preflight, freeze, or no argument')
        else: main()
    except (RuntimeError, OSError, ValueError, KeyError) as e: print(str(e),file=sys.stderr); raise SystemExit(1)
