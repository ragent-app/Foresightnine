from common import *
import re
mb=(HERE/'manifest.json').read_bytes();m=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==v for f,v in m['files'].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m['source_files'].items())
assert json.loads((HERE/'run-started.json').read_text())['manifest_sha256']==mh
results=[]
for condition in m['conditions']:
 name=condition['task'];path=HERE/(name+'-result.json')
 if not path.exists():results.append({'task':name,'status':'not_run','mechanical_pass':False});continue
 out=json.loads(path.read_text());assert out['manifest_sha256']==mh
 task,sources=get_task(name);source=sources[0];content=out.get('content',{});claims=content.get('claims',[])
 def valid(c):
  loc=c.get('location',{});a,b=loc.get('character_start'),loc.get('character_end')
  return (c.get('source_id')==source.source_id and type(a) is int and type(b) is int and
          0<=a<b<=len(source.text) and source.text[a:b]==c.get('quotation') and not c.get('partial_token'))
 texts=' '.join(c.get('text','') for c in claims)
 quotes=' '.join(q.get('quotation','') for c in claims for q in c.get('citations',[]) if valid(q))
 def both(pattern):return bool(re.search(pattern,texts,re.I|re.S) and re.search(pattern,quotes,re.I|re.S))
 if name=='BANK_2020':
  patterns={'bank':r'jpmorgan\s+chase','metric':r'common\s+equity\s+tier\s+1|cet1','actual':r'\b12\.4\b',
            'ending':r'\b10\.8\b','minimum':r'\b10\.0\b','actual_label':r'actual','ending_label':r'ending|end',
            'minimum_label':r'minimum','scenario':r'severely\s+adverse'}
 elif name=='BEA_2020':
  patterns={'nominal':r'[-−]\s*2\.7','real':r'[-−]\s*3\.9','saving':r'16\.3','pce':r'personal\s+consumption|pce',
            'year':r'2020','current':r'current\s+dollars','chained':r'chained\s+dollars','saving_label':r'sav(?:ing|ings)'}
 else:
  patterns={'lower':r'1\s*/\s*2|0\.5(?:0)?','upper':r'3\s*/\s*4|0\.75',
            'chair':r'yellen','target':r'target\s+range','inflation':r'inflation'}
 checks={k:both(v) for k,v in patterns.items()}
 checks['all_claims_located']=bool(claims) and all(c.get('citations') and c.get('support_status')!='incomplete' and all(valid(q) for q in c['citations']) for c in claims)
 warnings=('publication_date_warnings','rate_unit_warnings','uncited_amounts','uncited_claim_amounts','uncited_dollar_amounts',
           'uncited_quarters','balance_direction_warnings','uncited_percentages','uncited_percentage_bounds','reused_percentages',
           'uncited_basis_points','uncited_percentage_points','uncited_fact_values','citation_context_warnings')
 checks['no_known_claim_warnings']=not any(c.get(k) for c in claims for k in warnings)
 fact_warnings=('currency_context_unverified','currency_quote_missing','currency_conflict_unverified','direction_context_unverified',
                'table_alignment_unverified','table_period_conflict','source_value_warnings')
 checks['no_known_fact_warnings']=not any(f.get(k) or f.get('quotation_match')=='pdf_line_hyphen_restored' for f in content.get('facts',[]) for k in fact_warnings)
 checks['required_counterargument']=not task['require_counterarguments'] or any(c.get('role')=='counterargument' and c.get('citations') and all(valid(q) for q in c['citations']) for c in claims)
 limits=content.get('limitations',[])
 checks['no_omitted_content']=not any('omitted' in x for x in limits if isinstance(x,str))
 run=content.get('run',{});checks['bounded_call']=run.get('model_calls')==1 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000
 checks['partial_disclosed']=run.get('status')=='completed' or (run.get('status')=='partial' and bool(limits))
 results.append({'task':name,'status':out.get('status'),'research_status':run.get('status'),'mechanical_checks':checks,
                 'mechanical_pass':out.get('status')=='completed' and all(checks.values()),'reported_tokens':run.get('total_tokens'),
                 'semantic_support':'not established by mechanical checks'})
result={'results':results,'mechanical_pass':len(results)==3 and all(x['mechanical_pass'] for x in results),
        'total_reported_tokens':sum(x['reported_tokens'] for x in results if type(x.get('reported_tokens')) is int),
        'unknown_usage':any(type(x.get('reported_tokens')) is not int for x in results),
        'semantic_support':'requires separate assessment; matching numbers and terms is insufficient'}
(HERE/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
