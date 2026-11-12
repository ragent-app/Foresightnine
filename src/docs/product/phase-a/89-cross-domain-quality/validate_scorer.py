"""Exercise the frozen scorer on fabricated outputs in a temporary directory."""
from pathlib import Path
from hashlib import sha256
import json,shutil,subprocess,sys,tempfile
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
with tempfile.TemporaryDirectory(prefix='foresight-scorer-') as tmp:
 d=Path(tmp)
 for name in ('manifest.json','references.json','prepare.py','run.py','score.py','validate_scorer.py','MSFT_REVENUE-input.json','FED_POLICY-input.json','FED_POLICY-source.txt'):
  shutil.copyfile(p/name,d/name)
 (d/'run-started.json').write_text(json.dumps({'manifest_sha256':h(mb)}))
 for entry in m['tasks']:
  id=entry['id'];t=json.loads((d/(id+'-input.json')).read_text());src=t['sources'][0];text=src['text'] if src['kind']=='text' else (d/(id+'-source.txt')).read_text()
  def cite(a,b):return {'source_id':src['source_id'],'quotation':text[a:b],'location':{'character_start':a,'character_end':b}}
  run={'status':'completed','model_calls':1,'search_calls':0,'total_tokens':100,'source_hashes':{src['source_id']:h(text.encode())},'prompt_sha256':m['prompt_sha256_by_task'][id]}
  if id=='MSFT_REVENUE':
   facts=[];quotes=[]
   for year,value in [('2022','198270000000'),('2023','211915000000')]:
    start=text.index('"end": "'+year+'-06-30"');end=text.index(value,start)+len(value);c=cite(start,end);quotes.append(c);facts.append({'value':value,'unit':'USD','period':'FY'+year,**c})
   content={'facts':facts,'claims':[{'text':'Results','citations':quotes}],'calculations':[{'formula_id':'growth_percent','value':'6.88','inputs':[{'value':'211915000000'},{'value':'198270000000'}]}]}
  else:
   a=text.index('3-3/4 to 4 percent');b=text.index('ongoing increases');content={'facts':[],'claims':[{'role':'support','text':'3.75 to 4 percent','citations':[cite(a,a+len('3-3/4 to 4 percent'))]},{'role':'support','text':'ongoing increases','citations':[cite(b,b+len('ongoing increases'))]}]}
  content['run']=run;(d/(id+'-result.json')).write_text(json.dumps({'manifest_sha256':h(mb),'operation_status':'completed','content':content}))
 subprocess.run([sys.executable,str(d/'score.py')],check=True,capture_output=True)
 good=json.loads((d/'score.json').read_text());assert good['passed']==2,good
 bad=json.loads((d/'MSFT_REVENUE-result.json').read_text());bad['content']['calculations'][0]['value']='9.99';(d/'MSFT_REVENUE-result.json').write_text(json.dumps(bad))
 subprocess.run([sys.executable,str(d/'score.py')],check=True,capture_output=True)
 changed=json.loads((d/'score.json').read_text());assert changed['passed']==1 and not changed['tasks']['MSFT_REVENUE']['expected_detail']['growth_calculation'],changed
 print('scorer fixtures: correct passes; wrong growth fails')
