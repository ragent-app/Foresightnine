"""Prepare new-source evaluation inputs and freeze mechanical criteria before calls."""
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import gzip,json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_fetch import _TextParser
from app.research_uploads import _extract_text
from app.research_analysis import SYSTEM_PROMPT
from app.research_verification import REVIEW_SYSTEM_PROMPT,REVIEW_METHOD
h=lambda b:sha256(b).hexdigest()
html=gzip.decompress((p/'cisco-2024-sec.html.gz').read_bytes());parser=_TextParser();parser.feed(html.decode('utf-8'));parser.close();source='\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
assert all(x in source for x in ('53,803','56,998')) and len(source.encode())<2_000_000
tasks=[{'id':'CISCO_REV','question':'使用所附 Cisco 年度申报材料，列示 FY2024 与 FY2023 的总收入、期间及单位，计算同比变化，并指出一条有引用依据的反方或限制。', 'source_kind':'derived_text','source_text':source,'source_title':'Cisco FY2024 Form 10-K, filed 2024-09-05, SEC HTML text extraction; table separators generated','source_url':'https://www.sec.gov/Archives/edgar/data/858877/000085887724000017/csco-20240727.htm'},
{'id':'BEA_Q1','question':'根据所附 BEA 第三次估计，2023 年第一季度实际 GDP 折年增长率是多少？第二次估计是多少？说明修订和可验证的局限。','source_kind':'original_pdf','source_file':'bea-gdp-q1-2023.pdf','source_title':'BEA GDP third estimate first quarter 2023','published_at':'2023-06-29T12:30:00Z','text_order':'reading'},
{'id':'FED_DISS','question':'根据所附美联储 2019 年 7 月声明，谁对本次货币政策行动投反对票？他们倾向于什么政策？引用资料并说明局限。','source_kind':'original_pdf','source_file':'fed-2019-07.pdf','source_title':'Federal Reserve July 2019 FOMC statement','published_at':'2019-07-31T18:00:00Z','text_order':'reading'}]
refs={'CISCO_REV':{'facts':[{'value':'53803','period':'2024','unit':'million','literal':'53,803'},{'value':'56998','period':'2023','unit':'million','literal':'56,998'}], 'calculation':{'formula_id':'growth_percent','inputs':['53803','56998'],'value':'-5.61'},'allowed_percentages':['-5.61','-6','64.7','62.7']},
'BEA_Q1':{'facts':[{'value':'2.0','period':'2023','unit':'percent','literal':'2.0'},{'value':'1.3','period':'2023','unit':'percent','literal':'1.3'}],'allowed_percentages':['2.0','1.3','0.7','2.6']},
'FED_DISS':{'facts':[],'name_checks':[{'claim_aliases':['george','乔治'],'quote_term':'Esther L. George'},{'claim_aliases':['rosengren','罗森格伦','罗森格林'],'quote_term':'Rosengren'}],'allowed_percentages':['0.25','2','2.25','2.5']}}
entries=[]
for task in tasks:
 if task['source_kind']=='original_pdf':
  raw=(p/task['source_file']).read_bytes();text=_extract_text(raw,text_order='reading');(p/(task['id']+'-source.txt')).write_text(text);task['source_sha256']=h(raw);task['source_text_sha256']=h(text.encode())
 file=p/(task['id']+'-input.json');file.write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n');entries.append({'id':task['id'],'source_id':'source_'+uuid4().hex,'sha256':h(file.read_bytes())})
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n')
old=json.loads((root/'docs/product/phase-a/45-reserved-cross-domain-rerun/manifest.json').read_text())
m={**old,'assessment':'New held-out public source snapshots and tasks not used in prior model debugging, but evaluator knows references and no independent human review was performed. Three tasks cannot establish overall quality.',
 'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'inputs':entries,'runner_sha256':h((p/'run.py').read_bytes()),'scorer_sha256':h((p/'score.py').read_bytes()),'matching_sha256':h((p/'matching.py').read_bytes()),'references_sha256':h((p/'references.json').read_bytes()),'prepare_sha256':h((p/'prepare.py').read_bytes()),'source_html_sha256':h(html),'source_html_gzip_sha256':h((p/'cisco-2024-sec.html.gz').read_bytes()),'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
 'max_calls_per_task':2,'max_calls_total':6,'max_tokens_per_task':10000,'max_tokens_total':30000,'review_calls':3,'search_calls':0,'retry_external_calls':0,
 'pass_rule':'All 3 tasks must satisfy the frozen mechanical scorer, including two calls, complete model-review coverage, all review verdicts supports, expected facts, source locations and budget. This narrow gate cannot establish independent semantic correctness or full stage-B acceptance.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'inputs':[(e['id'],e['sha256']) for e in entries],'max_tokens_total':m['max_tokens_total']},ensure_ascii=False))
