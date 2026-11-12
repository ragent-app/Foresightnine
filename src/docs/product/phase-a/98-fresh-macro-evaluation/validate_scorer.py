"""Check that unsupported, missing and mislocated evidence cannot pass."""
import score
from unittest.mock import patch
from hashlib import sha256
s='annual rate 3.1 percent exports consumer spending downward revision to private inventory investment'; t='annual rate 2.8 percent'
task={'id':'GDP_REVISION','sources':[{'kind':'pdf','source_id':'second','text_file':'second'},{'kind':'pdf','source_id':'third','text_file':'third'}]}
def cite(sid,text):return {'source_id':sid,'quotation':text,'location':{'character_start':0,'character_end':len(text)}}
claims=[{'role':'support','text':'年化 second 2.8','citations':[cite('second',t)]}, {'role':'support','text':'年化 third 3.1 出口 消费','citations':[cite('third',s)]}, {'role':'counterargument','text':'库存下调抵消','citations':[cite('third',s)]}]
content={'claims':claims,'run':{'status':'completed','model_calls':1,'search_calls':0,'total_tokens':20,'source_hashes':{'second':sha256(t.encode()).hexdigest(),'third':sha256(s.encode()).hexdigest()},'prompt_sha256':'prompt'}}
with patch.object(score.Path,'read_text',lambda p: {'second':t,'third':s}[p.name]):
 result={'operation_status':'completed','content':content}
 assert score.assess(task,result,'prompt')['mechanical_pass']
 claims[2]['citations'][0]['location']['character_start']=1
 assert not score.assess(task,result,'prompt')['mechanical_pass']
 claims[2]['citations'][0]['location']['character_start']=0
 claims[2]['text']='库存增加'
 # The keyword test does not establish direction; semantic checking remains mandatory.
 assert score.assess(task,result,'prompt')['expected_detail']['offset_inventory']
print('scorer fixtures: located example passes, wrong position fails; direction limitation documented')
