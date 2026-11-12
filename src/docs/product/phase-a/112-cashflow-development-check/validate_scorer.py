import score
from hashlib import sha256
text='January 29, 2023 January 30, 2022 (in millions) Net cash provided by operating activities 5,641 9,108'
task={'sources':[{'source_id':'s','text':text}]}
quote={'source_id':'s','quotation':text,'location':{'character_start':0,'character_end':len(text)}}
def f(id,v,year):return {'fact_id':id,'value':v,'unit':'USD millions','currency':'USD','period':year,**quote}
c={'facts':[f('a','5641','2023'),f('b','9108','2022')],'claims':[{'text':'Cash flow fell','role':'support','citations':[quote]}],
 'calculations':[{'formula_id':'growth_percent','value':'-38.07','inputs':[{'value':'5641'},{'value':'9108'}]}],
 'run':{'status':'partial','model_calls':1,'search_calls':0,'total_tokens':10,'source_hashes':{'s':sha256(text.encode()).hexdigest()},'prompt_sha256':'prompt'}}
r={'operation_status':'completed','content':c}
assert score.assess(task,r,'prompt')['narrow_mechanical_pass']
c['calculations'][0]['value']='38.07';assert not score.assess(task,r,'prompt')['narrow_mechanical_pass'];c['calculations'][0]['value']='-38.07'
c['claims'][0]['citations'][0]['location']['character_start']=1;assert not score.assess(task,r,'prompt')['narrow_mechanical_pass']
print('scorer fixtures: correct passes, wrong direction and mislocated quotation fail')
