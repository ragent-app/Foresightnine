"""Single serial admission queue. An unknown settlement ends the batch."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from preflight import env_for, main as preflight

ROOT = Path(__file__).resolve().parent


def main():
    preflight()
    results=ROOT/'results'
    results.mkdir(exist_ok=False)
    plan=[(case,arm,repeat) for case in ('D1','D2','D3','D4') for arm,repeat in [('A',1),('B',1),('B',2),('A',2)]]
    index=[]
    for case,arm,repeat in plan:
        name=f'{case}-{arm}-{repeat}'
        path=results/(name+'.json')
        path.with_suffix('.started').touch(exist_ok=False)
        code=None
        try:
            p=subprocess.run([sys.executable,str(ROOT/'worker.py'),case,arm,str(path)],cwd=ROOT,env=env_for(arm),timeout=180)
            code=p.returncode
            if not path.exists():
                path.write_text(json.dumps(dict(status='worker_failed',exit_code=p.returncode,external_outcome='unknown')))
        except subprocess.TimeoutExpired:
            if not path.exists():
                path.write_text(json.dumps(dict(status='hard_deadline',external_outcome='unknown',seconds=180)))
            timeout=True
        else:
            timeout=False
        try:
            d=json.loads(path.read_text())
        except (ValueError,OSError):
            d=dict(status='worker_output_invalid',external_outcome='unknown')
        failures=[e for e in d.get('events',[]) if e.get('kind')=='model_failure']
        known_trunc=bool(failures) and all(e.get('code')=='provider_output_truncated' and
            (e.get('external_state'),e.get('settlement'))==('returned','settled') for e in failures)
        reason=d.get('reason') or ''
        allowed=reason=='submitted' or (reason=='provider_output_truncated' and known_trunc) or (reason=='provider_budget_exhausted' and d.get('calls')==4)
        stop=code!=0 or timeout or not allowed or bool(d.get('error_type')) or (bool(failures) and not known_trunc) or any(
            e.get('external_state')=='unknown' or e.get('settlement')=='unknown' for e in d.get('events',[]))
        index.append(dict(name=name,sha256=sha256(path.read_bytes()).hexdigest(),reason=reason,worker_exit_code=code,hard_timeout=timeout,stop=stop))
        (ROOT/'index.json').write_text(json.dumps(index,indent=2)+'\n')
        print(name,d.get('status'),reason,'calls',d.get('calls'),flush=True)
        if stop: break


if __name__=='__main__':
    main()
