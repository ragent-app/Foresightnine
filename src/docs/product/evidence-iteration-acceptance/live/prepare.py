"""Freeze an offline, inspectable paired-run bundle. This script never calls a model."""
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
SUITE = REPO / 'src/docs/product/context-evidence-acceptance/independent/replication'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: prepare.py NEW_BUNDLE_DIR PHASE4_ACCEPTANCE_RECORD')
    out, phase4 = map(lambda x: Path(x).resolve(), sys.argv[1:])
    if not phase4.is_file() or not phase4.read_text().strip():
        raise SystemExit('Phase 4 acceptance record required before candidate freeze.')
    out.mkdir(parents=True, exist_ok=False)
    # Extract only the tracked API tree at the registered baseline.
    archive = out / 'baseline.tar'
    with archive.open('wb') as f:
        subprocess.run(['git', '-C', str(REPO), 'archive', '82900b4', 'src/apps/api',
                        'src/packages/contracts/schemas', 'src/packages/asset-registry/registry',
                        'src/infra/deployment/provider-manifest.v1.json'], stdout=f, check=True)
    baseline = out / 'A'
    baseline.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(baseline, filter='data')
    archive.unlink()
    # Both arms start at the identical tracked baseline. The frozen patch
    # contains only this iteration's API changes, including the default decision.
    candidate = out / 'B'
    shutil.copytree(baseline, candidate)
    patch = HERE / 'candidate.patch.gz'
    subprocess.run(['git', 'apply', '--whitespace=error', '-'], cwd=candidate,
                   input=gzip.decompress(patch.read_bytes()), check=True)
    shutil.copy2(patch, out / patch.name)
    suite = out / 'suite'
    suite.mkdir()
    expected = json.loads((SUITE / 'freeze.json').read_text())
    for name in ('materials.json', 'expectations.json', 'requests.json'):
        source = SUITE / name
        if digest(source) != expected[name]:
            raise ValueError(f'Original frozen suite changed: {name}')
        shutil.copy2(source, suite / name)
    shutil.copy2(SUITE / 'freeze.json', suite / 'freeze.json')
    for name in ('worker.py', 'start_one.py', 'protocol.md'):
        shutil.copy2(HERE / name, out / name)
    shutil.copy2(phase4, out / 'phase4-acceptance-record')
    files = {str(p.relative_to(out)): digest(p) for p in sorted(out.rglob('*')) if p.is_file()}
    (out / 'freeze.json').write_text(json.dumps({'baseline':'82900b4', 'frozen_at':datetime.now(timezone.utc).isoformat(), 'files':files}, indent=2)+'\n')
    print(f'Frozen {len(files)} files in {out}; no model request sent.')


if __name__ == '__main__':
    main()
