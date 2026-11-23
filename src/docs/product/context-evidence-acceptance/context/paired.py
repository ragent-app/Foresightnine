"""C: deterministic old/new native input serialization; offline, no model calls."""
import json
import subprocess
import types
import inspect
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import canonical_json_dumps
from app.research_context_manager import build_model_turn_input as candidate
from app.research_sources import ResearchSource, SourcePermissions

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[4]
raw=(HERE.parent/'evidence/frozen-cases.json').read_bytes()
data=json.loads(raw)
old_source=subprocess.check_output(['git','show','3e1ca4d:src/apps/api/app/research_context_manager.py'],cwd=ROOT)
old=types.ModuleType('app._frozen_context_baseline')
old.__package__='app'
exec(compile(old_source,'frozen_context_baseline.py','exec'),old.__dict__)
settings=SimpleNamespace(base_url='https://example.com',model='fixture-model',api_key='unused',allow_private_network=False)
provider=AnthropicMessagesProvider(settings,SimpleNamespace(timeout_seconds=10,max_input_tokens=None,max_output_tokens=256),anthropic_version='2023-06-01',auth_scheme='bearer',max_tokens=256)
now=datetime(2026,4,1,tzinfo=timezone.utc)
candidate_source=Path(inspect.getsourcefile(candidate)).read_bytes()
results={'fixture_sha256':sha256(raw).hexdigest(),'baseline_context_sha256':sha256(old_source).hexdigest(),
         'candidate_context_sha256':sha256(candidate_source).hexdigest(), 'wire_protocol':'anthropic_messages',
         'model':'fixture-model','output_cap':256,'input_capacity_bytes':120000,'network_calls':0,'cases':{}}
for case in ('F1','F3','M1','M2'):
    if case.startswith('F'):
        rows=[data['financial']['statement']]+([data['financial']['increment_note']] if case=='F1' else [])
        question=data['financial']['question']
    else:
        variant=next(r for r in data['market']['variants'] if r['id']==case)
        rows=[{'id':'prices-'+case,'published':'2026-02-05','text':'Adjusted close, USD, synthetic exchange X: '+ '; '.join(f'{asset} {date} {value}' for asset,date,value in variant['observations'])+'.'}]
        question=data['market']['question']
    sources={r['id']:ResearchSource(source_id=r['id'],owner_id='fixture',title=r['id'],text=r['text'],kind='user_supplied',published_at=datetime.fromisoformat(r['published']).replace(tzinfo=timezone.utc),obtained_at=datetime(2026,3,1,tzinfo=timezone.utc),content_sha256=sha256(r['text'].encode()).hexdigest(),permissions=SourcePermissions(store=True,model_input=True,display=True,export=True)) for r in rows}
    facts=[{'fact_id':'fact-'+str(i),'label':'located exhibit','value':str(i),'unit':'text','period':'fixture','source_id':s.source_id,'quotation':s.text,'character_start':0,'character_end':len(s.text)} for i,s in enumerate(sources.values())]
    ranges=[{'source_id':s.source_id,'start':0,'end':len(s.text)} for s in sources.values()]
    source_inventory=[{'source_id':s.source_id,'content_sha256':s.content_sha256,'eligibility':'eligible'} for s in sources.values()]
    for path in ('normal','followup','recovery'):
        state={'user':json.dumps({'question':question,'sources':source_inventory,'research_as_of':now.isoformat()},ensure_ascii=False),'history':[],
               'read_ranges':ranges if path=='normal' else [], 'facts':facts if path=='normal' else [],
               'prior_read_ranges':ranges if path!='normal' else [],'prior_facts':facts if path!='normal' else [],
               'prior_claims':[], 'calculations':[], 'prior_calculations':[], 'failed_attempts':[]}
        if path=='recovery':
            state['failed_attempts']=[{'reason':'provider_timeout','interruption':'response_body_after_frames'}]
            state['continuation_guidance']='Continue from the already read exhibit.'
        arms={}
        for label,fn in (('A',old.build_model_turn_input),('B',candidate)):
            turn=fn(state=state,source_by_id=sources)
            wire=provider._wire(turn,256)
            body=canonical_json_dumps(wire).encode()
            text=body.decode()
            arms[label]={'wire_sha256':sha256(body).hexdigest(),'wire_bytes':len(body),'fits_fixed_capacity':len(body)<=120000,
                         'located_sources':{sid:src.text in text for sid,src in sources.items()},
                         'benchmark_end_present':'Benchmark 2026-02-04 110' in text}
        results['cases'][case+'-'+path]={'available_source_ids':list(sources),'arms':arms,
          'missing_benchmark_end':case=='M2'}
print(json.dumps(results,ensure_ascii=False,sort_keys=True,indent=2))
