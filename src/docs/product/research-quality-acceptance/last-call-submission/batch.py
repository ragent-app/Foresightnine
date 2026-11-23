"""One serial batch; started entries are irreversible and never replayed."""
import json, os, subprocess, sys
from pathlib import Path
here = Path(__file__).resolve().parent
for check in ('verify.py', 'preflight.py'):
    if subprocess.run([sys.executable, str(here/check)], check=False).returncode:
        print('STOP offline preflight failed', flush=True); sys.exit(1)
outdir = here / 'results'; outdir.mkdir(exist_ok=True)
for case in ('D1','D2','D3','D4'):
    for arm, iteration in (('A','1'),('B','1'),('B','2'),('A','2')):
        name = f'{case}-{arm}-{iteration}'
        out = outdir / (name + '.json'); marker = outdir / (name + '.started')
        try:
            with marker.open('x') as f: f.write('admitted; never replay\n')
        except FileExistsError:
            print('STOP existing started marker', name, flush=True); sys.exit(1)
        try:
            run = subprocess.run([sys.executable, str(here/'worker.py'), case, arm, str(out)], timeout=180, capture_output=True)
            if run.returncode != 0 and not out.exists():
                out.write_text(json.dumps({'status':'worker_failed','external_outcome':'unknown','exit_code':run.returncode})+'\n')
        except subprocess.TimeoutExpired:
            out.write_text(json.dumps({'status':'hard_deadline','seconds':180,'external_outcome':'unknown'})+'\n')
        if not out.exists(): print('STOP missing output',name,flush=True); sys.exit(1)
        d = json.loads(out.read_text()); reason = str(d.get('reason') or '')
        failures = [e for e in d.get('events',[]) if e.get('kind') == 'model_failure']
        trunc = reason == 'provider_output_truncated' and bool(failures) and all(e.get('external_state') == 'returned' and e.get('settlement') == 'settled' for e in failures)
        budget = reason == 'provider_budget_exhausted' and d.get('calls') == 4
        bad = d.get('error_type') or d.get('status') in ('hard_deadline','worker_failed') or (reason.startswith('provider_') and not (trunc or budget))
        bad = bad or any(not (e.get('code')=='provider_output_truncated' and e.get('external_state')=='returned' and e.get('settlement')=='settled') for e in failures)
        bad = bad or any(e.get('external_state')=='unknown' or e.get('settlement')=='unknown' for e in d.get('events',[]))
        print(name, d.get('status'), 'calls=', d.get('calls','unknown'), flush=True)
        if bad: print('STOP failure or uncertain settlement', flush=True); sys.exit(1)
