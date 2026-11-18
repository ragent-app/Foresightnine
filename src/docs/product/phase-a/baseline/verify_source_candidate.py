"""Load candidate PDFs only if an independently supplied manifest hash matches.

This checks integrity of a known candidate, not authorization, historical
authenticity, true filesystem isolation, or independent review.
"""
import argparse
import datetime
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / 'original-source-candidate-v01'
SUPPORTED = {'D19','D20','D21','D22','D23'}


def sha(content):
    return hashlib.sha256(content).hexdigest()


def utc(value):
    result = datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return result.astimezone(datetime.timezone.utc)


def local_file(directory, name):
    if Path(name).name != name:
        raise ValueError('invalid file name')
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise ValueError('file missing or outside allowed directory')
    return path.read_bytes()


def load_verified(task_id, expected_manifest_sha256):
    if task_id not in SUPPORTED or not re.fullmatch(r'[0-9a-f]{64}', expected_manifest_sha256):
        raise ValueError('unsupported task or invalid externally supplied manifest hash')
    manifest_bytes = local_file(ROOT, 'manifest.json')
    if sha(manifest_bytes) != expected_manifest_sha256:
        raise ValueError('candidate manifest hash mismatch')
    manifest = json.loads(manifest_bytes)
    entries = [entry for entry in manifest['tasks'] if entry.get('id') == task_id]
    if len(entries) != 1:
        raise ValueError('manifest task missing or ambiguous')
    folder = ROOT / task_id
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != ROOT.resolve():
        raise ValueError('task directory outside candidate root')
    task_bytes = local_file(folder, 'task.json')
    if sha(task_bytes) != entries[0]['task_sha256']:
        raise ValueError('task hash mismatch')
    task = json.loads(task_bytes)
    if task.get('id') != task_id:
        raise ValueError('task ID mismatch')
    recorded = entries[0]['source_sha256']
    if not isinstance(recorded, list) or len(recorded) != len(task['sources']):
        raise ValueError('source count mismatch')
    sources = []
    seen = set()
    for info, expected_source in zip(task['sources'], recorded):
        name = info['filename']
        if name in seen:
            raise ValueError('duplicate source file')
        seen.add(name)
        if utc(info['publication_utc']) > utc(task['as_of']):
            raise ValueError('source after cutoff')
        data = local_file(folder, name)
        if not data.startswith(b'%PDF-') or sha(data) != expected_source or info['sha256'] != expected_source:
            raise ValueError('source hash mismatch')
        sources.append({'metadata':info,'bytes':data})
    return {'manifest_sha256':sha(manifest_bytes), 'task_sha256':sha(task_bytes), 'task':task, 'sources':sources}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('task_id', choices=sorted(SUPPORTED))
    parser.add_argument('--manifest-sha256', required=True, help='expected hash from an independently retained experiment record')
    args = parser.parse_args()
    result = load_verified(args.task_id, args.manifest_sha256)
    print(json.dumps({'id':args.task_id, 'manifest_sha256':result['manifest_sha256'],
                      'task_sha256':result['task_sha256'], 'source_count':len(result['sources']),
                      'limitations':['Hash pin is not provenance or permission.', 'Directory checks are not OS sandboxing.']}))
