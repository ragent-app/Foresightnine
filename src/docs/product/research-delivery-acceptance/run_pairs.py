"""Run the preregistered AB/BA sequence from an isolated code snapshot."""
import json
from pathlib import Path
import subprocess
import sys


def endpoint_blocked(row):
    codes = {401, 402, 403, 404, 429, 502, 503, 504}
    for call in row['diagnostics']:
        if call['status_code'] in codes:
            return True
        if call['error_code'] in {'insufficient_quota', 'authentication_error', 'permission_error'}:
            return True
        transport = call.get('transport') or {}
        if any(s.get('stage') in ('dns', 'tcp', 'tls') and s.get('result') in ('failed', 'timeout', 'error')
               for s in transport.get('stages', [])):
            return True
        if row['reason'] in ('provider_transport_error', 'model_unavailable') and transport.get('submission_state') == 'not_started':
            return True
    return False


def main():
    dest = Path(sys.argv[1])
    dest.mkdir(exist_ok=False)
    plan = [(case, arm, pair) for case in ('recovery', 'facts') for pair, order in enumerate(('AB','BA'),1) for arm in order]
    rows = []
    for case, arm, pair in plan:
        output = dest / f'{case}-{pair}-{arm}.json'
        subprocess.run([sys.executable, str(Path(__file__).with_name('paired.py')), case, arm, str(output)], check=True)
        row = json.loads(output.read_text())
        rows.append({'run':output.name,'status':row['status'],'reason':row['reason'],'calls':row['actual_calls'],
                     'reported_tokens':row['usage']['reported_tokens'], 'elapsed_seconds':row['elapsed_seconds']})
        stopped = endpoint_blocked(row)
        (dest/'index.json').write_text(json.dumps({'runs':rows, 'planned':len(plan), 'endpoint_blocked':stopped}, indent=2)+'\n')
        print(rows[-1], flush=True)
        if stopped:
            break


if __name__ == '__main__':
    main()
