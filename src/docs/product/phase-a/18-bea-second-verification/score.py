"""Fixed mechanical scoring for a known, non-blind development verification."""
from decimal import Decimal,InvalidOperation
from hashlib import sha256
import json,sys
from pathlib import Path
folder=Path(__file__).resolve().parent;root=folder.parents[3]
h=lambda b:sha256(b).hexdigest()
mb=(folder/'manifest.json').read_bytes();m=json.loads(mb);assert h(mb)==json.loads((folder/'run-started.json').read_text())['manifest_sha256']
assert h((folder/'references.json').read_bytes())==m['references_sha256'] and h((folder/'score.py').read_bytes())==m['scorer_sha256']
entry=m['inputs'][0];blob=(folder/'G23-input.json').read_bytes();assert h(blob)==entry['sha256'];task=json.loads(blob)
pdf=(folder/task['source_file']).read_bytes();assert h(pdf)==task['source_sha256']
sys.path.insert(0,str(root/'apps/api'))
from app.research_uploads import _extract_text
source=_extract_text(pdf);result=json.loads((folder/'G23-result.json').read_text());assert result['manifest_sha256']==h(mb)
c=result.get('content') or {};run=c.get('run') or {};facts=c.get('facts') or []
def num(v):
 try:return Decimal(str(v))
 except (InvalidOperation,ValueError):return None
def locate(entry):
 x=entry.get('location');q=entry.get('quotation')
 if not isinstance(x,dict) or not isinstance(q,str):return False
 a,b=x.get('character_start'),x.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q
checks=[]
for expected in json.loads((folder/'references.json').read_text())['G23']['facts']:
 def match(f):
  kind=(str(f.get('period',''))+' '+str(f.get('label',''))).lower();q=any(x in kind for x in ('quarter','q4','fourth','季度'))
  return (num(f.get('value'))==num(expected['value']) and expected['year'] in kind and
          (q if expected['period_kind']=='quarter' else not q) and ('percent' in str(f.get('unit','')).lower() or f.get('unit')=='%') and locate(f))
 checks.append({'expected_value':expected['value'],'period_kind':expected['period_kind'],'valid':any(match(f) for f in facts)})
support=[x for x in c.get('claims',[]) if x.get('role')=='support'];locations=bool(support) and all(x.get('citations') and all(locate(cit) for cit in x['citations']) for x in support)
segments=run.get('selected_segments') or [];two_windows=len(segments)>=2 and all(segments[i]['character_end']<=segments[i+1]['character_start'] for i in range(len(segments)-1))
budget=result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and run.get('total_tokens') is not None and run['total_tokens']<=m['max_tokens_per_task']
report={'task':'G23','status':result.get('operation_status'),'research_status':run.get('status'),'facts':checks,'support_claims_have_locations':bool(locations),'two_nonoverlapping_windows':two_windows,'budget_ok':budget,'reported_tokens':run.get('total_tokens'),'cost_usd':'unknown','semantic_support':'unverifiable','historical_availability':'unverified','sample':'related-source development verification, not independent', 'mechanical_pass':budget and bool(locations) and two_windows and all(x['valid'] for x in checks)}
(folder/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
