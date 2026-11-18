import hashlib,json
from pathlib import Path
p=Path(__file__).resolve().parent; h=lambda b:hashlib.sha256(b).hexdigest()
mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert json.loads((p/'started.json').read_text())['manifest_sha256']==h(mb)
for n in ('run.py','score.py','baseline-system.txt'):
 assert h((p/n).read_bytes())==m['files_sha256'][n]
result={'manifest_sha256':h(mb),'independent':False,'variants':{}}
for variant in ('baseline','bounded_quote'):
 f=p/(variant+'-result.json')
 if not f.exists():result['variants'][variant]={'status':'not_run','all_checks_passed':False};continue
 output=json.loads(f.read_text());v=output.get('verdicts',{})
 checks={'unquoted_weeks_not_supported':v.get('0') in ('does_not_support','uncertain'),
         'quoted_revenue_supported':v.get('1')=='supports',
         'wrong_year_rejected':v.get('2') in ('does_not_support','contradicts')}
 result['variants'][variant]={'status':output['status'],'checks':checks,'all_checks_passed':output['status']=='returned' and all(checks.values()),'reported_tokens':output.get('reported_tokens'),'cost_usd':None}
(p/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
