"""Copy a predeclared, hash-verified code manifest for a future evaluation.

Only API Python source, project SQL migrations and the declared evaluation runner are accepted.  This
utility never reconstructs an old working tree or copies data/configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[5]


def freeze(manifest: Path, destination: Path) -> None:
    hashes = json.loads(manifest.read_text())['sha256']
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('empty source manifest')
    files = []
    for name, expected in hashes.items():
        path = Path(name)
        parts = path.parts
        allowed = (len(parts) == 5 and parts[:4] == ('src', 'apps', 'api', 'app') and path.suffix == '.py') or (
            len(parts) == 6 and parts[:4] == ('src', 'docs', 'product', 'phase-a') and parts[-1] == 'runner.py') or (
            len(parts) == 4 and parts[:3] == ('src', 'infra', 'migrations') and path.suffix == '.sql')
        source = ROOT / path
        if not allowed or source.is_symlink() or not source.is_file() or source.resolve() != source.absolute():
            raise ValueError(f'path not permitted: {name}')
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'source changed: {name}')
        files.append((path, data))
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for path, data in files:
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (destination / 'manifest.json').write_text(json.dumps({'sha256': hashes}, indent=2) + '\n')
    except Exception:
        shutil.rmtree(destination)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    freeze(args.manifest, args.destination)
