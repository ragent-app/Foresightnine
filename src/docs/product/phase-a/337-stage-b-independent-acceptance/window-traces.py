from datetime import datetime, timezone
import json
from app.research_analysis import analyze
from app.research_sources import ResearchSource, SourcePermissions

def check(name, text, question, budget):
    source=ResearchSource(source_id='s',owner_id='o',title='Report',text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256='x',permissions=SourcePermissions(store=True,model_input=True,display=True))
    prompts=[]
    result=analyze(question,[source],None,lambda p:(prompts.append(json.loads(p)) or {},0),max_prompt_chars=budget)
    print(name,'budget',budget,'bytes',result.prompt_bytes,'sent',[(s['character_start'],s['character_end'],text[s['character_start']:s['character_end']]) for s in result.segments], 'limits',result.limitations[:2])
extra='Revenue and debt both increased for the company.'
check('extra','Revenue increased in the year. '+'Other notes. '*70+extra+' Other notes. '*120,'Explain revenue and debt.',2000)
text='Revenue increased. '+'Other unrelated text. '*100+'Debt declined. '+'Other unrelated text. '*100+'Margin improved. '+'Other unrelated text. '*100
check('multi',text,'Explain revenue and debt and margin.',5000)
check('multi_small',text,'Explain revenue and debt and margin.',1500)
