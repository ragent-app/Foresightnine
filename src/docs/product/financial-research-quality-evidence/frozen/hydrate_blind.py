import json,sys,time
from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1])
from app.research_delivery import assess,incomplete
from app.research_semantic_review import candidates,parse_reviews
from app.research_sources import ResearchSource,SourcePermissions
from app.research_run_insights import assess_core_requirements
cases=json.load(open('/tmp/foresight-quality-private/cases.json'))
map_by_case={v['id']:k for k,v in json.load(open('/tmp/foresight-quality-private/blind-mapping.json')).items()}
verdicts={v['id']:v for v in json.load(open('/tmp/foresight-quality-blind-verdicts.json'))}
results=[]
for item in cases:
    text=item['source']; s=ResearchSource(source_id='fixture',owner_id='fixture',title='Synthetic exhibit',text=text,kind='user_supplied',obtained_at=datetime(2026,1,1,tzinfo=timezone.utc),content_sha256=sha256(text.encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
    state={'read_ranges':[{'source_id':'fixture','start':0,'end':len(text)}]}
    claim={'claim_id':'candidate','text':item['claim'],'role':'support','citations':[{'source_id':'fixture','quotation':text,'character_start':0,'character_end':len(text)}]}
    sources={'fixture':s}; rows=assess_core_requirements(item['question'],state,sources,claims=[claim]); items=candidates(rows,[claim],state,sources,None)
    verdict=verdicts[map_by_case[item['id']]]
    raw=[{'requirement_id':x['requirement_id'],'claim_id':x['claim_id'],'review_key':x['review_key'],'evidence_ids':[str(i+1) for i in range(len(x['evidence']))], 'verdict':'supported' if verdict['supported'] else 'insufficient','reason':verdict['reason'],'condition':None} for x in items]
    hydrated=parse_reviews(json.dumps({'reviews':raw}),items)
    state['_semantic_reviews']=hydrated
    actual=assess(item['question'],state,sources,claims=[claim])
    results.append({'id':item['id'],'kind':item['kind'],'expected':item['expected'],'blind_supported':verdict['supported'],'candidate_count':len(items),'hydrated':len(hydrated),'answered':not incomplete(actual),'rows':actual})
print(json.dumps(results,ensure_ascii=False,indent=2))
