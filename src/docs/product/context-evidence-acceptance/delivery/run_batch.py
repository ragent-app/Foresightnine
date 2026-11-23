"""Run the preregistered ABBA/BAAB comparison using immutable API source snapshots."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
KEYS = {'FORESIGHTNINE_RESEARCH_LLM_' + k for k in ('API_KEY', 'MODEL', 'BASE_URL', 'ALLOWED_HOSTS')}
KEYS |= {'FORESIGHTNINE_MESSAGES_' + k for k in ('CAPABILITY_BASE_URL','CAPABILITY_MODEL','NATIVE_TOOLS','CONTINUATION','VERSION','AUTH_SCHEME')}
KEYS.add('FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL')


def snapshot_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.glob('src/apps/api/app/**/*.py')):
        digest.update(str(path.relative_to(root)).encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    for key in ('baseline', 'candidate', 'environment', 'output'):
        parser.add_argument(key, type=Path)
    args = parser.parse_args()
    env = {}
    for line in args.environment.read_text().splitlines():
        key, _, value = line.partition('=')
        if key in KEYS:
            env[key] = value.strip().strip('"').strip("'")
    if set(env) != KEYS or not all(env.values()):
        raise ValueError('The dedicated experiment configuration is incomplete.')
    env.update(PATH='/usr/bin:/bin', LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1', FORESIGHTNINE_RESEARCH_PRESEND_RETRIES='0')
    roots = {'A': args.baseline.resolve(), 'B': args.candidate.resolve()}
    args.output.mkdir(exist_ok=False)
    files = [HERE / 'paired_live.py', HERE / 'run_batch.py', HERE / 'e-preregistration.md',
             HERE.parent / 'independent/replication/materials.json', HERE.parent / 'independent/replication/expectations.json']
    manifest = {'models': [env['FORESIGHTNINE_RESEARCH_LLM_MODEL']], 'roots': {k:snapshot_hash(v) for k,v in roots.items()},
                'files': {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}, 'wave_orders': ['AB','BA','BA','AB'],
                'started_at':datetime.now(timezone.utc).isoformat()}
    (args.output / 'freeze.json').write_text(json.dumps(manifest,indent=2)+'\n')
    def execute(job):
        case, arm, wave = job
        output = args.output / f'{wave}-{case}-{arm}.json'
        submitted = output.with_suffix('.started')
        with submitted.open('x') as fp:
            fp.write(datetime.now(timezone.utc).isoformat()+'\n')
        config = {**env, 'PYTHONPATH': str(roots[arm] / 'src/apps/api')}
        result = subprocess.run([sys.executable, HERE/'paired_live.py',case,arm,output],env=config,
                                cwd=roots[arm],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=205)
        if result.returncode:
            raise RuntimeError(f'Worker exited {result.returncode}; inspect {case}/{arm} before retrying.')
        row=json.loads(output.read_text())
        print(case, arm, row.get('status'), row.get('reason'), row.get('error_type'), flush=True)
        return row
    with ThreadPoolExecutor(max_workers=2) as workers:
        for wave, arms in enumerate(manifest['wave_orders'],1):
            outcomes=list(workers.map(execute, [(case,arm,wave) for case,arm in zip(('J1','J4'),arms,strict=True)]))
            if any('error_type' in r for r in outcomes) or all(r.get('reason') in {'provider_secret_missing','provider_configuration_invalid','provider_request_rejected'} for r in outcomes):
                raise RuntimeError('Frozen batch stopped after a configuration or worker failure.')


if __name__ == '__main__':
    main()
