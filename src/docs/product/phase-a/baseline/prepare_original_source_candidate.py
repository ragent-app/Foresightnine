"""Create five separate macro tasks with original official PDF copies.

Candidate only: not an authorized model experiment, blind set, or human baseline.
Source scope is checked but filesystem/process isolation remains the evaluator's
responsibility. Never overwrite existing candidate material.
"""
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
OUTPUT = PARENT / 'original-source-candidate-v01'
SOURCES = {
    'macro-bea-2023-advance.json':'bea-gdp-2023-q4-advance.pdf',
    'macro-bea-2023-second.json':'bea-gdp-2023-q4-second.pdf',
    'macro-bea-2024-advance.json':'bea-gdp-2024-q4-advance.pdf',
    'macro-fed-2024-09.json':'fed-fomc-2024-09-18.pdf',
}


def utc(text):
    result = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return result.astimezone(datetime.timezone.utc)


def main():
    if OUTPUT.exists():
        raise SystemExit('output exists; preserve candidate and any human work')
    bank_bytes = (ROOT / 'macro_tasks_v01.json').read_bytes()
    bank = json.loads(bank_bytes)
    expected = ['D19', 'D20', 'D21', 'D22', 'D23']
    if [item['id'] for item in bank['tasks']] != expected:
        raise ValueError('unexpected task set')
    prepared = []
    for task in bank['tasks']:
        sources = []
        for name in task['sources']:
            if name not in SOURCES:
                raise ValueError('source outside allowlist')
            metadata = json.loads((ROOT / name).read_text())
            filename = SOURCES[name]
            if metadata.get('source_snapshot') != '../source-snapshots/' + filename:
                raise ValueError('source location differs from allowlist')
            raw = (PARENT / 'source-snapshots' / filename).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if not raw.startswith(b'%PDF-') or digest != metadata.get('source_sha256'):
                raise ValueError('missing PDF or original hash mismatch')
            if utc(metadata['source_publication']) > utc(task['as_of']):
                raise ValueError('document release later than task cutoff')
            sources.append({'filename':filename, 'raw':raw, 'sha256':digest,
                            'source_url':metadata['source'], 'publication_utc':metadata['source_publication']})
        prepared.append((task, sources))
    OUTPUT.mkdir()
    entries = []
    for task, sources in prepared:
        folder = OUTPUT / task['id']
        folder.mkdir()
        meta_sources = []
        for source in sources:
            with (folder / source['filename']).open('xb') as stream:
                stream.write(source['raw'])
            meta_sources.append({k:source[k] for k in ('filename','sha256','source_url','publication_utc')})
        task_doc = {'id':task['id'], 'question':task['question'], 'as_of':task['as_of'],
                    'sources':meta_sources, 'status':'candidate-not-reviewed-not-frozen',
                    'notes':['Read the allowed PDFs as source documents; extracted answers are not supplied.',
                             'Present-day copies do not prove past byte-for-byte availability or absence of subsequent amendments.']}
        raw_task = (json.dumps(task_doc, ensure_ascii=False, indent=2) + '\n').encode()
        with (folder / 'task.json').open('xb') as stream:
            stream.write(raw_task)
        entries.append({'id':task['id'], 'task_sha256':hashlib.sha256(raw_task).hexdigest(),
                        'source_sha256':[item['sha256'] for item in meta_sources]})
    manifest = {'version':'original-source-candidate-v01','status':'draft-not-frozen-not-reviewed',
                'created_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'task_bank_sha256':hashlib.sha256(bank_bytes).hexdigest(),'tasks':entries,
                'limitations':['PDFs were retrieved in 2026; historical byte identity and specific rights remain to be verified.',
                               'Separate directories are not a security or privacy boundary.',
                               'Public candidate questions are not a confidential or independent blind test.']}
    with (OUTPUT / 'manifest.json').open('x') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f'Created {len(entries)} isolated-by-directory candidate task packages at {OUTPUT}')


if __name__ == '__main__':
    main()
