"""Execute the frozen E1 sequence; never resume a started arm automatically."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

here = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument('--from-run', default='D1-A-1')
args = p.parse_args()
started = False
for case in ('D1', 'D2', 'D3', 'D4'):
    for arm, iteration in (('A', '1'), ('B', '1'), ('B', '2'), ('A', '2')):
        name = f'{case}-{arm}-{iteration}'
        if not started:
            if name != args.from_run:
                continue
            started = True
        target = here / 'e1-results' / f'{name}.json'
        launch = subprocess.run([sys.executable, str(here / 'launch_e1.py'), case, arm, iteration], check=False)
        if launch.returncode or not target.exists():
            print(f'STOP {case}-{arm}-{iteration}: launcher failed or outcome unknown', flush=True)
            sys.exit(1)
        data = json.loads(target.read_text())
        reason = str(data.get('reason') or '')
        failure = data.get('error_type') or data.get('status') in ('hard_deadline', 'worker_failed')
        failures = [e for e in data.get('events', []) if e.get('kind') == 'model_failure']
        known_truncation = reason == 'provider_output_truncated' and bool(failures) and all(e.get('external_state') == 'returned' and e.get('settlement') == 'settled' for e in failures)
        normal_budget = reason == 'provider_budget_exhausted' and data.get('calls') == 4
        failure = failure or (reason.startswith('provider_') and not (normal_budget or known_truncation))
        failure = failure or any(not (e.get('code') == 'provider_output_truncated' and e.get('external_state') == 'returned' and e.get('settlement') == 'settled') for e in failures)
        failure = failure or any(e.get('external_state') == 'unknown' or e.get('settlement') == 'unknown' for e in data.get('events', []))
        print(f'{case}-{arm}-{iteration}: {data.get("status")} calls={data.get("calls", "unknown")}', flush=True)
        if failure:
            print('STOP provider failure or unknown outcome', flush=True)
            sys.exit(1)
