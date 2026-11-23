"""Run one E1 arm with an exclusive admission marker and hard process deadline."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
p = argparse.ArgumentParser()
p.add_argument('case', choices=['D1', 'D2', 'D3', 'D4'])
p.add_argument('arm', choices=['A', 'B'])
p.add_argument('iteration', choices=['1', '2'])
a = p.parse_args()
out = HERE / 'e1-results' / f'{a.case}-{a.arm}-{a.iteration}.json'
out.parent.mkdir(exist_ok=True)
marker = out.with_suffix('.started')
marker.touch(exist_ok=False)
try:
    run = subprocess.run([sys.executable, str(HERE / 'paired_e1.py'), a.case, a.arm, str(out)],
                         timeout=180, check=False, capture_output=True, text=True)
    if run.returncode != 0 and not out.exists():
        out.write_text(json.dumps({'status': 'worker_failed', 'exit_code': run.returncode, 'external_outcome': 'unknown'}) + '\n')
except subprocess.TimeoutExpired:
    out.write_text(json.dumps({'status': 'hard_deadline', 'seconds': 180, 'external_outcome': 'unknown'}) + '\n')
