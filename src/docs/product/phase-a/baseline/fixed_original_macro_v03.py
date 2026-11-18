"""Narrow, deterministic source-document pilot for D19-D23; no model or network.

This is a task-specific extractor written after seeing the candidate documents,
not a blind or general research evaluator. Run one task at a time. It checks
the per-task source whitelist, content hashes and cutoff before parsing PDFs.
"""
import argparse
import datetime
import hashlib
import json
import re
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from verify_source_candidate import load_verified

ROOT = Path(__file__).resolve().parent
TASKS = ROOT.parent / 'original-source-candidate-v01'
OUTPUT = ROOT / 'original-macro-pilot-results'
ALLOWED = {'D19','D20','D21','D22','D23'}


def utc(value):
    parsed = datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return parsed.astimezone(datetime.timezone.utc)


def match(pages, pattern):
    found = []
    for page, text in enumerate(pages, 1):
        for item in re.finditer(pattern, ' '.join(text.split()), re.IGNORECASE):
            found.append((page, item))
    if len(found) != 1:
        raise ValueError(f'expected one match for {pattern}, found {len(found)}')
    return found[0]


def number(pages, pattern):
    page, found = match(pages, pattern)
    return {'value':found.group(1), 'page':page}


def main(task_id, manifest_sha256):
    if task_id not in ALLOWED:
        raise ValueError('unsupported task')
    target = OUTPUT / f'{task_id}-v03-2026-11-11.json'
    if target.exists():
        raise SystemExit('output exists; do not overwrite evidence')
    started = time.perf_counter()
    verified = load_verified(task_id, manifest_sha256)
    task = verified['task']
    docs = []
    calls = []
    for source in verified['sources']:
        info, content = source['metadata'], source['bytes']
        begin = time.perf_counter()
        result = subprocess.run(['pdftotext','-layout','-','-'], input=content, capture_output=True, timeout=20, check=True)
        calls.append({'tool':'pdftotext -layout stdin', 'source':info['filename'], 'exit_code':result.returncode,
                      'elapsed_seconds':round(time.perf_counter()-begin,6)})
        docs.append({'source_url':info['source_url'], 'sha256':info['sha256'], 'pages':result.stdout.decode('utf-8').split('\f')})
    annual = r'Real GDP increased ([0-9.]+) percent in (?:2023|2024)'
    quarter = r'real gross domestic product \(GDP\) increased at an annual rate of ([0-9.]+) percent in the fourth quarter of 2023'
    if task_id == 'D19':
        a = number(docs[0]['pages'],annual); q = number(docs[0]['pages'],quarter)
        findings = {'annual_2023_percent':a['value'],'annual_page':a['page'],
                    'q4_annualized_percent':q['value'],'q4_page':q['page'],
                    'distinction':'annual growth versus quarter-on-quarter annualized growth'}
    elif task_id == 'D20':
        a = number(docs[0]['pages'],quarter); b = number(docs[1]['pages'],quarter)
        findings = {'advance_percent':a['value'],'advance_page':a['page'],'second_percent':b['value'],'second_page':b['page'],
                    'revision_percentage_points':str(Decimal(b['value'])-Decimal(a['value'])),
                    'measure':'Q4 quarter-on-quarter annualized growth'}
    elif task_id == 'D21':
        a = number(docs[0]['pages'],annual)
        b = number(docs[1]['pages'],r'Real GDP increased [0-9.]+ percent in 2024 \(from the 2023 annual level to the 2024 annual level\), compared with an increase of ([0-9.]+) percent in 2023')
        findings = {'initial_2023_percent':a['value'],'initial_page':a['page'],'later_2023_percent':b['value'],'later_page':b['page'],
                    'revision_percentage_points':str(Decimal(b['value'])-Decimal(a['value'])),
                    'distinction':'separate publication vintages, not an estimate available at the earlier date'}
    elif task_id == 'D22':
        a = number(docs[0]['pages'],annual)
        findings = {'annual_2024_percent':a['value'],'page':a['page'],'publication_utc':task['sources'][0]['publication_utc']}
    else:
        page, found = match(docs[0]['pages'],r'lower the target range for the federal funds rate by (1/2) percentage point to (4-3/4) to (5) percent')
        findings = {'decrease_percentage_points':'0.5','lower_percent':'4.75','upper_percent':'5.00','page':page,
                    'measure':'target range, not an observed single market rate','text_pattern_groups':list(found.groups())}
    output = {'status':'exploratory-task-specific-no-human-grading', 'id':task_id,
              'run_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'task_sha256':verified['task_sha256'],'manifest_sha256':verified['manifest_sha256'],'as_of':task['as_of'],
              'sources':[{k:v for k,v in d.items() if k != 'pages'} for d in docs],
              'tool_calls':calls,'model_calls':0,'network_calls':0,'retries':0,'findings':findings,
              'elapsed_seconds':round(time.perf_counter()-started,6),
              'cost':{'model_provider_usd':0,'network_provider_usd':0,'human_compute_storage':'not measured'},
              'limitations':['Patterns were written after inspection of these same documents.',
                             'Candidate input and reference answers are not human-reviewed or frozen.',
                             'No retrieval, independent citation verification, user confirmation, or real-time monitoring.',
                             'Publication cutoff is not proof of historical byte-identical availability.']}
    OUTPUT.mkdir(exist_ok=True)
    with target.open('x') as stream:
        json.dump(output,stream,ensure_ascii=False,indent=2)
        stream.write('\n')
    print(json.dumps({'task':task_id,'findings':findings,'elapsed_seconds':output['elapsed_seconds']}))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('task_id', choices=sorted(ALLOWED))
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    main(args.task_id, args.manifest_sha256)
