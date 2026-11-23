#!/usr/bin/env python3
"""Replay the frozen old submission trace and a selected current test suite.

Includes the original start snapshot for a faithful old worktree. Does not call
external services. Output is a compact JSON record, not a model-quality score.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
CASES_SHA256 = '7e62e0cd385e9923ea1030b1fd9825b2fba41ade816d6601e8e57e288cdcd316'


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    return {'command': argv[1:], 'exit_code': result.returncode, 'output': result.stdout}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', help='override the bundled start snapshot directory')
    parser.add_argument('--current-test', action='append', default=[],
                        help='pytest node ID to run against current tree; may repeat')
    args = parser.parse_args()
    if hashlib.sha256((HERE / 'cases.json').read_bytes()).hexdigest() != CASES_SHA256:
        raise SystemExit('Frozen cases hash mismatch')
    extracted = tempfile.TemporaryDirectory(prefix='fn-quality-snapshot-') if not args.snapshot else None
    if extracted:
        with tarfile.open(HERE / 'start-snapshot.tar.gz') as source:
            source.extractall(extracted.name, filter='data')
    snapshot = Path(args.snapshot or extracted.name)
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    for name, expected in manifest.items():
        if hashlib.sha256((snapshot / name).read_bytes()).hexdigest() != expected:
            raise SystemExit(f'Snapshot hash mismatch: {name}')
    python = str(ROOT / 'src/apps/api/.venv/bin/python')
    with tempfile.TemporaryDirectory(prefix='fn-quality-replay-') as temp:
        archive = subprocess.run(['git', 'archive', '879de1d'], cwd=ROOT, check=True,
                                 stdout=subprocess.PIPE).stdout
        archive_path = Path(temp) / 'base.tar'
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as source:
            source.extractall(temp, filter='data')
        for name in manifest:
            dest = Path(temp) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / name, dest)
        old = run([python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                   'tests/test_research_runtime_attention.py::test_presubmit_gate_nudges_once_for_unmet_method_pack_then_accepts'],
                  Path(temp) / 'src/apps/api')
        current = None
        if args.current_test:
            current = run([python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *args.current_test],
                          ROOT / 'src/apps/api')
        print(json.dumps({'base_commit': '879de1d', 'cases_sha256': CASES_SHA256,
                          'old_trace': old, 'current_tests': current}, ensure_ascii=False, indent=2))
        return 0 if old['exit_code'] == 0 and (current is None or current['exit_code'] == 0) else 1


if __name__ == '__main__':
    sys.exit(main())
