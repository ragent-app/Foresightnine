"""Compare old input check and manifest pinning using temporary copies only."""
import datetime
import hashlib
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / 'candidate-integrity-probe-2026-11-11.json'


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def expect_failure(fn, needle):
    try:
        fn()
    except ValueError as exc:
        if needle not in str(exc):
            raise
        return str(exc)
    raise AssertionError('modified input unexpectedly accepted')


def main():
    if OUTPUT.exists():
        raise SystemExit('probe record exists; do not overwrite')
    verifier = module('candidate_verifier','verify_source_candidate.py')
    legacy = module('old_candidate_extractor','fixed_original_macro_v02.py')
    original = HERE.parent / 'original-source-candidate-v01'
    pin = hashlib.sha256((original / 'manifest.json').read_bytes()).hexdigest()
    for task_id in ('D19','D20','D21','D22','D23'):
        verifier.load_verified(task_id,pin)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'candidate'
        shutil.copytree(original,root)
        verifier.ROOT = root
        legacy.TASKS = root
        legacy.OUTPUT = Path(tmp) / 'output'
        task_path = root / 'D19' / 'task.json'
        original_task = task_path.read_bytes()
        altered = json.loads(original_task)
        altered['question'] += ' [synthetic alteration]'
        task_path.write_text(json.dumps(altered,ensure_ascii=False,indent=2)+'\n')
        # Legacy script verifies PDF against task metadata, but not task against
        # the previously recorded manifest. Its output is kept only in tmp.
        legacy.main('D19')
        old_accepted_changed_question = (legacy.OUTPUT / 'D19-v02-2026-11-11.json').exists()
        task_rejected = expect_failure(lambda:verifier.load_verified('D19',pin),'task hash mismatch')
        manifest_path = root / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        next(item for item in manifest['tasks'] if item['id']=='D19')['task_sha256'] = hashlib.sha256(task_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        manifest_rejected = expect_failure(lambda:verifier.load_verified('D19',pin),'manifest hash mismatch')
        shutil.copyfile(original/'manifest.json',manifest_path)
        task_path.write_bytes(original_task)
        pdf = root/'D19'/json.loads(original_task)['sources'][0]['filename']
        with pdf.open('ab') as stream:
            stream.write(b'\nsynthetic tamper\n')
        source_rejected = expect_failure(lambda:verifier.load_verified('D19',pin),'source hash mismatch')
    record = {'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'manifest_sha256':pin,'method':'temporary candidate copies; no original material changed; no model or network',
              'legacy_v02_accepted_changed_question':old_accepted_changed_question,
              'pinned_verifier_rejections':{'modified_task':task_rejected,'modified_task_and_manifest':manifest_rejected,'modified_pdf':source_rejected},
              'limitations':['An externally retained trustworthy pin and operating-system isolation remain necessary.',
                             'Hashes do not establish historical authenticity, permission or evaluation quality.',
                             'The old successful run was a synthetic input-integrity probe, not a model experiment.']}
    with OUTPUT.open('x') as stream:
        json.dump(record,stream,indent=2)
        stream.write('\n')
    print(json.dumps(record,indent=2))


if __name__ == '__main__':
    main()
