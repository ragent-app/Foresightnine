import json, sys, time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.model_protocol import ModelResponse
from tests.test_research_runtime import context, turn
cases=json.loads(Path('/tmp/foresight-quality-private/cases.json').read_text())
# Fixed four preselected paired natural-worded questions, scripted writer (not an autonomous agent).
ids={'g01a','g01b','g04a','g04b','g09a','g09b','g13a','g13b'}
output=[]
for case in cases:
    if case['id'] not in ids: continue
    text=case['source']; claim=case['claim']
    source=ResearchSource(source_id='s1',owner_id='u',title='Synthetic exhibit',text=text,kind='user_supplied',obtained_at=datetime(2026,1,1,tzinfo=timezone.utc),content_sha256=sha256(text.encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
    class Scripted:
        def __init__(self): self.calls=0
        def complete(self,messages,tools,**kw):
            self.calls+=1
            if self.calls == 1: return turn('read_sources',{'reads':[{'source_id':'s1'}],'rationale':'Read exhibit'},'read')
            if self.calls == 2: return turn('submit_research',{'report':claim,'claims':[{'text':claim,'source_id':'s1','quotation':text,'character_start':0,'character_end':len(text),'role':'support'}],'limitations':[],'stopping_reason':'answered'},'submit')
            return ModelResponse({'role':'assistant','content':'No additional evidence.'},(),None,'stop')
    m=Scripted(); start=time.perf_counter()
    try:
        res=run_research_agent(question=case['question'],sources=[source],model=m,context=context())
        output.append({'id':case['id'],'expected':case['expected'],'status':res.status,'reason':res.reason,'report':res.report,'question_parts':res.question_parts,'calls':m.calls,'seconds':time.perf_counter()-start})
    except Exception as e:
        output.append({'id':case['id'],'expected':case['expected'],'error':repr(e),'calls':m.calls,'seconds':time.perf_counter()-start})
print(json.dumps(output,ensure_ascii=False,indent=2,default=str))
