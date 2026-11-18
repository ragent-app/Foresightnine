"""Fixed mechanical scoring for a known, non-blind two-task sample."""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json,sys
from pathlib import Path
folder=Path(__file__).resolve().parent
root=folder.parents[3]
manifest_bytes=(folder/'manifest.json').read_bytes(); manifest=json.loads(manifest_bytes)
h=lambda x:sha256(x).hexdigest()
assert h(manifest_bytes)==json.loads((folder/'run-started.json').read_text())['manifest_sha256']
assert h((folder/'references.json').read_bytes())==manifest['references_sha256']
assert h((folder/'score.py').read_bytes())==manifest['scorer_sha256']
refs=json.loads((folder/'references.json').read_bytes())
sys.path.insert(0,str(root/'apps/api'))
from app.research_uploads import _extract_text

def dec(v):
 try:
  result=Decimal(str(v)); return result if result.is_finite() else None
 except (InvalidOperation,ValueError): return None

def unit_ok(f,kind):
 unit=str(f.get('unit','')).lower(); cur=str(f.get('currency') or '').lower()
 if kind=='percent': return (unit=='%' or 'percent' in unit) and not cur
 return 'million' in unit and ('usd' in unit or cur=='usd')

def located(entry,text):
 loc=entry.get('location');q=entry.get('quotation');
 if not isinstance(loc,dict) or not isinstance(q,str):return False
 a,b=loc.get('character_start'),loc.get('character_end')
 return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==q

report={'manifest_sha256':h(manifest_bytes),'claim_semantics':'unverifiable','source_history':'unverified',
        'sample':'small known candidate; not blinded or representative', 'tasks':{}}
for item in manifest['inputs']:
 id=item['id'];blob=(folder/(id+'-input.json')).read_bytes();assert h(blob)==item['sha256'];task=json.loads(blob)
 if task['source_kind']=='original_pdf':
  pdf=(folder/task['source_file']).read_bytes();assert h(pdf)==task['source_sha256'];text=_extract_text(pdf)
 else:text=task['source_text']
 path=folder/(id+'-result.json')
 if not path.exists():report['tasks'][id]={'mechanical_pass':False,'status':'missing'};continue
 result=json.loads(path.read_text());assert result['manifest_sha256']==report['manifest_sha256']
 content=result.get('content') or {};run=content.get('run') or {};facts=content.get('facts') or [];claims=content.get('claims') or []
 factchecks=[]
 for expected in refs[id]['facts']:
  matches=[f for f in facts if dec(f.get('value'))==dec(expected['value'])]
  factchecks.append({'value':expected['value'],'period':expected['period'],'valid':any(expected['period'] in str(f.get('period','')) and unit_ok(f,refs[id]['unit']) and located(f,text) for f in matches)})
 support=[c for c in claims if c.get('role')=='support']
 claims_have_locations=bool(support) and all(bool(c.get('citations')) and all(located(cit,text) for cit in c['citations']) for c in support)
 expected=refs[id]['calculation'];calculation_ok=True
 if expected:
  calculation_ok=any(c.get('formula_id')==expected['formula_id'] and dec(c.get('value'))==dec(expected['value']) and c.get('unit')==expected['unit'] and [dec(x.get('value')) for x in c.get('inputs',[])]==[dec(x) for x in expected['inputs']] for c in content.get('calculations',[]))
 budget_ok=(result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and run.get('total_tokens') is not None and run['total_tokens']<=manifest['max_tokens_per_task'])
 no_detected_calculation_difference=not content.get('calculation_differences')
 passed=budget_ok and all(c['valid'] for c in factchecks) and claims_have_locations and calculation_ok and no_detected_calculation_difference
 report['tasks'][id]={'operation_status':result.get('operation_status'),'research_status':run.get('status'),'facts':factchecks,'support_claims_have_locations':claims_have_locations,
  'calculation_ok':calculation_ok,'no_detected_calculation_difference':no_detected_calculation_difference,'budget_ok':budget_ok,'reported_tokens':run.get('total_tokens'),
  'mechanical_pass':passed,'semantic_support':'unverifiable','cost_usd':'unknown'}
report['mechanical_tasks_passed']=sum(v['mechanical_pass'] for v in report['tasks'].values())
report['mechanical_task_count']=len(manifest['inputs'])
(folder/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
