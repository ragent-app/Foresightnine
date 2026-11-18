from datetime import datetime,timezone
from app.research_context import evidence_units,rank_units
from app.research_analysis import analyze,SYSTEM_PROMPT
from app.research_coordinator import RESEARCH_INPUT_ENVELOPE_BYTES
from app.research_sources import ResearchSource,SourcePermissions
import json

def source(sid,text,pdf=False):
 s=ResearchSource(source_id=sid,owner_id='o',title='Report',text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256='x',permissions=SourcePermissions(store=True,model_input=True,display=True))
 return s.model_copy(update={'retrieval_method':'user_pdf'}) if pdf else s

def run(name,q,inputs,budget,needles):
 print('\nCASE',name,'prompt_limit',budget,'system_bytes',len(SYSTEM_PROMPT.encode()),'envelope',RESEARCH_INPUT_ENVELOPE_BYTES,'equivalent_total',budget+len(SYSTEM_PROMPT.encode())+RESEARCH_INPUT_ENVELOPE_BYTES)
 for src in inputs:
  units=evidence_units(src.text,layout=src.retrieval_method=='user_pdf')
  ranked=rank_units(units,[],q)
  print('SOURCE',src.source_id,'characters',len(src.text),'units',len(units))
  for n in needles:
   hits=[(i,u.kind,[(a,len(t)) for a,t in u.parts]) for i,u in enumerate(ranked) if any(n.lower() in t.lower() for _,t in u.parts)]
   print('NEEDLE',n,'candidates',len(hits),'first_rank_and_ranges',hits[:2])
 out=[]
 result=analyze(q,inputs,None,lambda p:(out.append(p) or {},0),max_prompt_chars=budget)
 print('CALL',len(out),'prompt_bytes',len(out[0].encode()) if out else None)
 if out:
  data=json.loads(out[0]); print('OUTPUT',[(x['source_id'],len(x['text']),x['text'][:180]) for x in data['sources']]); print('NEEDLE_SENT',[(n,any(n.lower() in x['text'].lower() for x in data['sources'])) for n in needles])
 print('LIMITATIONS',result.limitations[:3])

run('long','2023 revenue?',[source('long','Unrelated material. '*1400+'Revenue for 2023 was 211,915 million.'+' Other details. '*300)],5000,['211,915'])
table=('Change from preceding month\n'+' '*40+'August'+' '*30+'September\n'+'Previous  Revised  Previous  Revised\n(Billions of dollars)  (Percent)\nPersonal income:\n'+'Current dollars'+' '*45+'71.6     74.4\n')
run('layout-table','income September',[source('first','Unrelated introduction. '*500+table+'Unrelated appendix. '*500,True)],2055,['74.4','September'])
run('two-sources','收入变化',[source('positive','营业收入增长证据。'*1000),source('negative','相反证据收入下降。'*1000)],3000,['增长','下降'])
table2=table.replace('71.6     74.4','74.4     88.2')
run('two-layout-tables','比较2024年9月个人收入较前月的当期美元变化。',[source('first','Unrelated introduction. '*500+table+'Unrelated appendix. '*500,True),source('second','Unrelated introduction. '*500+table2+'Unrelated appendix. '*500,True)],2055,['74.4','88.2','September'])
