"""Start exactly the next registered arm; never retry or automatically continue."""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

KEYS = {'FORESIGHTNINE_RESEARCH_LLM_' + k for k in ('API_KEY', 'MODEL', 'BASE_URL', 'ALLOWED_HOSTS')}
KEYS |= {'FORESIGHTNINE_MESSAGES_' + k for k in ('CAPABILITY_BASE_URL', 'CAPABILITY_MODEL', 'NATIVE_TOOLS', 'CONTINUATION', 'VERSION', 'AUTH_SCHEME')}
KEYS.add('FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL')
ORDER = [('J1', x) for x in 'ABBA'] + [('J4', x) for x in 'BAAB'] + [('J2', x) for x in 'ABBA'] + [('J3', x) for x in 'BAAB']


def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: start_one.py BUNDLE ENV_FILE RECOVERY_EVIDENCE_FILE')
    bundle, envpath, recovery = (Path(x).resolve() for x in sys.argv[1:])
    if not recovery.is_file() or not recovery.read_text().strip():
        raise SystemExit('Independent, inspectable recovery evidence required; elapsed time alone is insufficient.')
    manifest = json.loads((bundle / 'freeze.json').read_text())
    actual = {str(p.relative_to(bundle)) for root in ('A', 'B', 'suite') for p in (bundle / root).rglob('*') if p.is_file()}
    frozen = {p for p in manifest['files'] if p.startswith(('A/', 'B/', 'suite/'))}
    if actual != frozen:
        raise SystemExit('Frozen bundle file set changed.')
    for rel, expected in manifest['files'].items():
        if hashlib.sha256((bundle / rel).read_bytes()).hexdigest() != expected:
            raise SystemExit('Frozen file changed: ' + rel)
    env = {}
    for line in envpath.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key in KEYS:
            env[key] = value.strip().strip('"').strip("'")
    if set(env) != KEYS or not all(env.values()):
        raise SystemExit('Experiment configuration incomplete.')
    if (env['FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL'] != 'anthropic_messages'
        or env['FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL'].rstrip('/') != env['FORESIGHTNINE_RESEARCH_LLM_BASE_URL'].rstrip('/')
        or env['FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL'] != env['FORESIGHTNINE_RESEARCH_LLM_MODEL']):
        raise SystemExit('Model and endpoint do not match Messages capability registration.')
    runs = bundle / 'runs'
    runs.mkdir(exist_ok=True)
    with (runs / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        index = len(list(runs.glob('*.started')))
        if index >= len(ORDER):
            raise SystemExit('All registered arms started.')
        for prior in range(index):
            old = json.loads((runs / f'{prior+1:02d}.started').read_text())
            endpoint_hash = hashlib.sha256(env['FORESIGHTNINE_RESEARCH_LLM_BASE_URL'].encode()).hexdigest()
            if old.get('model') != env['FORESIGHTNINE_RESEARCH_LLM_MODEL'] or old.get('endpoint_sha256') != endpoint_hash:
                raise SystemExit('Model or endpoint changed during batch.')
            result = runs / f'{prior+1:02d}.json'
            if not result.is_file():
                raise SystemExit('Started arm without result; reconcile before any further start.')
            row = json.loads(result.read_text())
            if row.get('usage', {}).get('unknown_usage_calls', 0):
                raise SystemExit('Prior usage or external state unknown; reconcile before continuing.')
            if row.get('error_type') or row.get('reason') in {'provider_request_rejected', 'provider_configuration_invalid', 'provider_secret_missing'}:
                raise SystemExit('Prior provider failure or unknown outcome; batch stopped.')
            for receipt in row.get('receipts', []):
                if receipt.get('transport', {}).get('submission_state') not in ('response_received',):
                    raise SystemExit('Prior transport outcome unknown; batch stopped.')
        if index >= 8 and (not (runs / 'first-batch-scoring.md').is_file() or not (runs / 'first-batch-scoring.md').read_text().strip()):
            raise SystemExit('First batch scoring required before extending to J2/J3.')
        case, arm = ORDER[index]
        started = runs / f'{index+1:02d}.started'
        with started.open('x') as f:
            json.dump({'case':case, 'arm':arm, 'at':datetime.now(timezone.utc).isoformat(),
                       'recovery_evidence_sha256':hashlib.sha256(recovery.read_bytes()).hexdigest(),
                       'model':env['FORESIGHTNINE_RESEARCH_LLM_MODEL'],
                       'endpoint_sha256':hashlib.sha256(env['FORESIGHTNINE_RESEARCH_LLM_BASE_URL'].encode()).hexdigest()}, f)
        env.update(PATH='/usr/bin:/bin', LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1', FORESIGHTNINE_RESEARCH_PRESEND_RETRIES='0',
                   PYTHONPATH=str(bundle / arm / 'src/apps/api'))
        try:
            result = subprocess.run([sys.executable, str(bundle / 'worker.py'), case, arm, str(runs / f'{index+1:02d}.json')],
                                    env=env, cwd=bundle / arm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=205)
        except subprocess.TimeoutExpired:
            raise SystemExit('Worker timed out; external outcome unknown. Reconcile; never retry this arm.')
        if result.returncode:
            raise SystemExit('Worker failed; reconcile outcome before any further start.')
        print(f'Started {case} {arm}; inspect its result and reconcile before the next arm.')


if __name__ == '__main__':
    main()
