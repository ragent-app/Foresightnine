"""Freeze current common backend; restore just the old calculation definitions in arm A."""
import ast
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

EXP = Path(__file__).resolve().parent
SRC = EXP.parents[3]
BASE = '82900b4b3142b84c63eb644989d92ac6bde47bed'
NODES = {'research_tools_registry.py':'CalculationPlan','research_tool_executor.py':'_exec_calculate_financials'}


def definition(source, name):
    node = next(node for node in ast.parse(source).body if getattr(node,'name',None)==name)
    return node.lineno - 1, node.end_lineno


def main():
    dest = Path(sys.argv[1])
    dest.mkdir()
    frozen=EXP.parent/'e1-code-snapshot.tar.gz'
    manifest=json.loads((EXP.parent/'e1-code-freeze.json').read_text())
    with tarfile.open(frozen) as tar:
        tar.extractall(dest/'A',filter='data')
    for name,expected in manifest['files_sha256'].items():
        assert sha256((dest/'A'/name).read_bytes()).hexdigest()==expected,name
    backend=Path('apps/api/app')
    # Both policies run on the same original E1 transport, source/fact tools, prompts
    # and context builder. Only the delivered evaluator (and its two calculation
    # dependencies) is overlaid. Unrelated edits in the shared checkout stay out.
    for name in ('research_evidence_requirements.py','research_partial_answer.py','research_delivery.py'):
        shutil.copy2(SRC/backend/name,dest/'A'/backend/name)
    for name in ('research_calculations.py','research_report_grounding.py'):
        source=subprocess.check_output(['git','-C',str(SRC),'show',f'{BASE}:src/{backend}/{name}'])
        (dest/'A'/backend/name).write_bytes(source)
    shutil.copytree(dest/'A',dest/'B')
    for arm in ('A','B'):
        for name,node in NODES.items():
            target=dest/arm/backend/name
            original=target.read_text()
            from_code=(SRC/backend/name).read_text() if arm=='B' else subprocess.check_output(
                ['git','-C',str(SRC),'show',f'{BASE}:src/{backend}/{name}'],text=True)
            before,after=definition(from_code,node)
            start,end=definition(original,node)
            lines=from_code.splitlines(True)
            new=lines[before:after]
            if arm=='B' and node=='CalculationPlan':
                helper,h_end=definition(from_code,'_calculation_model_schema')
                new=['from pydantic.json_schema import SkipJsonSchema\n\n',*lines[helper:h_end],'\n\n',*new]
            target.write_text(''.join([*original.splitlines(True)[:start],*new,*original.splitlines(True)[end:]]))
    for name in ('worker.py','batch.py','protocol.md','read_audit.py','preflight.py','prepare.py', *sorted(p.name for p in EXP.glob('scor*') if p.is_file())):
        shutil.copyfile(EXP/name,dest/name)
    shutil.copyfile(EXP.parent/'last-call-submission/worker.py',dest/'observer.py')
    shutil.copyfile(EXP.parent.parent/'research-delivery-acceptance/paired.py',dest/'prior.py')
    for name in ('cases.json','e1-code-freeze.json'):
        shutil.copyfile(EXP.parent/name,dest/name)
    audit=EXP.parent/'repair-delivery'
    receipts=json.loads((audit/'api-results.json').read_text())
    frozen_runs={r['run']:r for r in json.loads((audit/'frozen-baseline.json').read_text())['rows']}
    replay=[]
    for receipt in receipts:
        name=receipt['run']
        entry=frozen_runs[name]
        raw=(SRC.parent/entry['path']).read_bytes()
        assert sha256(raw).hexdigest()==entry['sha256']
        run=json.loads(raw)
        parts=receipt['topic']['versions'][0]['content']['turns'][0]['question_parts']
        replay.append(dict(run=name,case=run['case'],source=run['source'],state=run['final_state_projection'],parts=parts))
    assert len(replay)==14
    (dest/'delivery-freeze.json').write_text(json.dumps(replay,ensure_ascii=False))
    hashes={str(p.relative_to(dest)):sha256(p.read_bytes()).hexdigest() for p in sorted(dest.rglob('*')) if p.is_file()}
    diffs=[name[2:] for name in hashes if name.startswith('A/') and hashes[name]!=hashes['B/'+name[2:]]]
    assert set(diffs)=={'apps/api/app/'+name for name in NODES},diffs
    (dest/'freeze.json').write_text(json.dumps({'base_commit':BASE,'different_files':diffs,'files':hashes},indent=2)+'\n')


if __name__ == '__main__':
    main()
