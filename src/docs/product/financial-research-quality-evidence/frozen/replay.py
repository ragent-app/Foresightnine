import json, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.research_delivery import assess, incomplete
from app.research_sources import ResearchSource, SourcePermissions
cases=json.loads(Path('/tmp/foresight-quality-private/cases.json').read_text())
result=[]
for item in cases:
    text=item['source']
    source=ResearchSource(source_id='fixture',owner_id='fixture',title='Synthetic exhibit',text=text,kind='user_supplied',obtained_at=datetime(2026,1,1,tzinfo=timezone.utc),content_sha256=sha256(text.encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
    state={'read_ranges':[{'source_id':'fixture','start':0,'end':len(text)}]}
    claim={'claim_id':'candidate','text':item['claim'],'role':'support','citations':[{'source_id':'fixture','quotation':text,'character_start':0,'character_end':len(text)}]}
    started=time.perf_counter()
    rows=assess(item['question'],state,{'fixture':source},claims=[claim])
    elapsed=time.perf_counter()-started
    answered=not incomplete(rows)
    result.append({'id':item['id'],'kind':item['kind'],'expected':item['expected'],'answered':answered,'rows':rows,'elapsed_seconds':elapsed})
counts=defaultdict(lambda: {'false_accept':0,'negative':0,'false_reject':0,'positive':0})
for r in result:
    d=counts[r['kind']]
    if r['expected']:
        d['positive']+=1; d['false_reject']+=not r['answered']
    else:
        d['negative']+=1; d['false_accept']+=r['answered']
print(json.dumps({'results':result,'by_kind':counts,'total_seconds':sum(x['elapsed_seconds'] for x in result)},ensure_ascii=False,indent=2))
