"""Validate the pinned runtime, protocol and first-turn single-factor difference without I/O."""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def env_for(arm):
    return {**os.environ,'PYTHONPATH':str(ROOT/arm/'apps/api')+os.pathsep+str(ROOT),
            'PYTHONDONTWRITEBYTECODE':'1','FORESIGHTNINE_RESEARCH_PRESEND_RETRIES':'0'}


def child(arm):
    import observer
    import prior
    from app import research_runtime as runtime
    assert Path(runtime.__file__).resolve().is_relative_to(ROOT/arm)
    manifest = json.loads((ROOT/'e1-code-freeze.json').read_text())
    assert os.environ['FORESIGHTNINE_RESEARCH_LLM_MODEL'] == manifest['model']
    ident = {key:os.environ.get(key) for key in manifest['config_fields']}
    assert manifest['non_secret_model_config_sha256'] in {
        sha256(json.dumps(ident, sort_keys=True, separators=seps).encode()).hexdigest()
        for seps in [(',',':'),(', ',': ')]}
    assert sha256((ROOT/'cases.json').read_bytes()).hexdigest() == manifest['data_sha256']
    p=prior.provider()
    assert p.policy.max_output_tokens==2048 and p.policy.timeout_seconds==90
    assert prior.LIMITS == dict(calls=4,tokens=160000,seconds=180,per_request_seconds=90,output=2048)
    source_tools=[asdict(t) for t in runtime.TOOLS]
    tool=next(t for t in source_tools if t['name']=='calculate_financials')
    schema=tool['input_schema']
    def properties(obj):
        if isinstance(obj,dict):
            props=obj.get('properties',{})
            if 'formula' in props and 'fact_ids' in props: yield obj
            for val in obj.values(): yield from properties(val)
        elif isinstance(obj,list):
            for val in obj: yield from properties(val)
    calc=next(properties(schema))
    if arm=='A':
        assert 'first_fact_id' in calc['properties'] and 'second_fact_id' in calc['properties']
    else:
        assert 'first_fact_id' not in calc['properties'] and 'second_fact_id' not in calc['properties'] and 'fact_ids' in calc['required']
    for t in source_tools:
        if t['name']=='calculate_financials':
            t.pop('input_schema')
    from app.research_sources import ResearchSource, SourcePermissions
    from app.research_runtime import run_research_agent
    class Captured(BaseException):
        pass
    initial={}
    class Capture:
        def __getattr__(self,key):
            return getattr(p,key)
        def complete_turn(self,turn,**kwargs):
            wire=p._wire(turn,cap=kwargs.get('max_output_tokens'))
            target=next(t for t in wire['tools'] if t['name']=='calculate_financials')
            target['input_schema']='<controlled>'
            initial[case['id']]=sha256(json.dumps(wire,sort_keys=True).encode()).hexdigest()
            raise Captured()
    for case in observer.CASES['cases']:
        if case['id'] not in ('D1','D2','D3','D4'):
            continue
        src=ResearchSource(source_id='exhibit',owner_id='isolated',title='Synthetic exhibit',text=case['snapshot'],
            kind='user_supplied',obtained_at=datetime(2026,11,23,tzinfo=timezone.utc),
            content_sha256=sha256(case['snapshot'].encode()).hexdigest(),
            permissions=SourcePermissions(store=True,model_input=True,display=True,export=True))
        try:
            run_research_agent(question=case['question'],sources=[src],model=Capture(),context=prior.context(4),clock={'utc_date':'2026-11-23'})
        except Captured:
            pass
    from app.research_delivery import assess,assemble_parts
    from app.research_partial_answer import merge_checkpoint_parts
    def canonical_parts(rows):
        return [([p.get(k) for k in ('question','status','answer','reason')],
                 [{k:v for k,v in ref.items() if v is not None} for ref in p['references']]) for p in rows]
    for item in json.loads((ROOT/'delivery-freeze.json').read_text()):
        case=next(c for c in observer.CASES['cases'] if c['id']==item['case'])
        src=ResearchSource(source_id='exhibit',owner_id='isolated',title='Synthetic exhibit',text=case['snapshot'],kind='user_supplied',
            obtained_at=datetime(2026,11,23,tzinfo=timezone.utc),content_sha256=item['source']['content_sha256'],
            permissions=SourcePermissions(**item['source']['permissions']))
        state={k:([] if v is None and k!='submission' else v) for k,v in item['state'].items()}
        state['_sources']=[item['source']]
        sources={'exhibit':src}
        claims=state.get('claims') or []
        question=case['question']
        rows=assess(question,state,sources,claims=claims)
        parts=assemble_parts(question,state,sources,{'exhibit'},rows,claims)
        parts=merge_checkpoint_parts(question,state,{'exhibit'},parts,sources)
        assert canonical_parts(parts)==canonical_parts(item['parts']),item['run']+' frozen delivery drift'
    assert len(initial)==4
    return dict(initial=initial,common=sha256(json.dumps(source_tools, sort_keys=True).encode()).hexdigest(),
                schema=sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest())


def main():
    for name,expected in json.loads((ROOT/'freeze.json').read_text())['files'].items():
        assert sha256((ROOT/name).read_bytes()).hexdigest()==expected,name
    rows={a:json.loads(subprocess.check_output([sys.executable,__file__,a],env=env_for(a),cwd=ROOT)) for a in 'AB'}
    assert rows['A']['initial']==rows['B']['initial']
    assert rows['A']['common']==rows['B']['common'] and rows['A']['schema']!=rows['B']['schema']
    (ROOT/'preflight-result.json').write_text(json.dumps(rows,indent=2)+'\n')


if __name__=='__main__':
    if len(sys.argv)==2:
        print(json.dumps(child(sys.argv[1])))
    else:
        main()
