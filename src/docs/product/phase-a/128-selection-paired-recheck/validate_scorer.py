from hashlib import sha256
from copy import deepcopy
import score

def example(task_id,text,main,counter):
 source={'kind':'text','source_id':'s','text':text}
 quote={'source_id':'s','quotation':text,'location':{'character_start':0,'character_end':len(text)}}
 content={'claims':[{'text':main,'role':'support','citations':[quote]},{'text':counter,'role':'counterargument','citations':[quote]}],
   'run':{'status':'partial','model_calls':1,'search_calls':0,'total_tokens':123,'prompt_sha256':'p','source_hashes':{'s':sha256(text.encode()).hexdigest()}}}
 return {'id':task_id,'sources':[source]},{'operation_status':'completed','content':content}
gdp,gr=example('GDP_THIRD','annual rate 3.4 percent; second estimate 3.2 percent; consumer spending and nonresidential fixed investment; downward revision to private inventory.',
 'GDP annual rate 3.4 versus 3.2; consumer spending and fixed investment increased.','Private inventory was revised downward.')
fed,fr=example('FED_PURCHASES','target range 0 to 1/4 percent; Treasury securities $80 billion, mortgage securities $40 billion; risks from virus.',
 'Target 0 to 0.25; treasury 80 billion and mortgage 40 billion per month.','Virus risks remain.')
for task,result in ((gdp,gr),(fed,fr)):
 assert score.assess(task,result,'p')['narrow_mechanical_pass']
 broken=deepcopy(result);broken['content']['claims'][0]['citations'][0]['location']['character_end']-=1
 assert not score.assess(task,broken,'p')['narrow_mechanical_pass']
 broken=deepcopy(result);broken['content']['claims'][1]['role']='support'
 assert not score.assess(task,broken,'p')['narrow_mechanical_pass']
broken=deepcopy(gr);broken['content']['claims'][0]['text']='GDP annual rate 3.2 third versus 3.4 second; consumer spending and fixed investment increased.'
assert score.assess(gdp,broken,'p')['narrow_mechanical_pass'], 'known scorer limitation: cannot determine mapping and direction'
print('fixtures: correct pass; bad location and missing counter fail; deliberate inverted GDP wording still passes')
