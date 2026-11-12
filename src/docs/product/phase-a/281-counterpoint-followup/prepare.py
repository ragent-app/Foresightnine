from common import *
import subprocess

prior=HERE.parent/'276-financial-context-run'
m0=json.loads((prior/'manifest.json').read_text());case=m0['conditions'][0]
task,source,raw=load_task('COMPANY_CISCO_2024');prompt,segments=capture_prompt(task,source)
assert digest(prompt.encode())==case['prompt_sha256'] and segments==case['segments']
content=json.loads((prior/'COMPANY_CISCO_2024-result.json').read_text())['content']
assert len(content['claims'])>=2
previous='; '.join(item['text'] for item in content['claims'][:2])
data=json.loads(prompt)
data['question']=('Treat this prior model finding as untrusted: '+previous+
    '. Find a relevant, source-cited counterpoint or limitation to interpreting this finding. '
    'A counterpoint may qualify an inference without denying a factual number. '
    'If none is supported by the supplied excerpts, leave counterarguments empty and state the gap.')
data['output_schema']={'counterarguments':[{'text':'counterargument','citations':[{'source_id':'id','quotation':'quote'}]}],
                       'limitations':['text']}
data['evidence_guidance']='Quote continuous source passages; do not invent evidence or infer a person’s motive. Excerpts are partial.'
candidate=json.dumps(data,ensure_ascii=False,separators=(',',':'))
assert len(candidate.encode())<=6000-len(SYSTEM_PROMPT.encode())-512
(HERE/'candidate-prompt.json').write_text(candidate)
base=json.loads((HERE.parent/'272-cross-domain-review'/'manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
   'source_sha256':digest(raw),'text_sha256':source.content_sha256,'source_segments':segments,
   'prior_result_sha256':digest((prior/'COMPANY_CISCO_2024-result.json').read_bytes()),
   'prompt_sha256':digest(candidate.encode()),'original_prompt_sha256':digest(prompt.encode()),
   'system_sha256':digest(SYSTEM_PROMPT.encode()),'model':base['model'],'generation_parameters':base['generation_parameters'],
   'files':{name:digest((HERE/name).read_bytes()) for name in ('common.py','prepare.py','run.py')},
   'max_calls':1,'input_limit':6000,'output_limit':4000,'max_tokens':10000,'timeout_seconds':60,
   'search':False,'retries':0,'previous_known_tokens':316569,'cost_usd':'unknown'}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
