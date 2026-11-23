import os,sys,json,time,uuid
from types import SimpleNamespace
sys.path.insert(0,'/home/ubuntu/Foresightnine/src/apps/api')
os.environ['FORESIGHTNINE_LOCAL_AUTH_MODE']='mock'
from fastapi.testclient import TestClient
import app.main as api
from app.model_protocol import ModelResponse,ModelUsage,ModelToolCall
from tests.test_research_runtime import turn as legacy_turn
def turn(name,args,ident):
    r=legacy_turn(name,args,ident)
    return ModelResponse(r.message,(ModelToolCall(ident,name,args),),r.usage,r.finish_reason,'tool_calls')
cases=json.load(open('/tmp/foresight-quality-private/cases.json'))
map_by_case={v['id']:k for k,v in json.load(open('/tmp/foresight-quality-private/blind-mapping.json')).items()}
verdicts={v['id']:v for v in json.load(open('/tmp/foresight-quality-blind-verdicts.json'))}
ids={'g01a','g01b','g04a','g04b','g09a','g09b','g13a','g13b'}
client=TestClient(api.app)
auth=client.post('/v1/auth/local/register',json={'email':uuid.uuid4().hex+'@example.test','password':'correct horse battery staple!'})
assert auth.status_code==201,auth.text
headers={'Authorization':'Bearer '+auth.json()['access_token']}
results=[]
for item in cases:
    if item['id'] not in ids: continue
    response=client.post('/v2/research/sources',headers=headers,json={'title':'Synthetic exhibit','text':item['source'],'permissions':{'store':True,'display':True,'model_input':True,'export':True}})
    assert response.status_code==201,response.text
    sid=response.json()['source_id']
    response=client.post('/v2/research/topics',headers=headers,json={'question':item['question'],'source_ids':[sid]})
    assert response.status_code==201,response.text
    topic=response.json(); path='/v2/research/topics/'+topic['topic_id']; verdict=verdicts[map_by_case[item['id']]]
    class Provider:
        name='offline-fixed-writer-blind-review'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=None,max_output_tokens=1000)
        def __init__(self):self.writer=0;self.review=0
        def complete_turn(self,request,**kw):
            if not request.tools:
                self.review+=1; items=json.loads(request.user)['candidates']; reviews=[]
                for x in items:
                    reviews.append({'requirement_id':x['requirement_id'],'claim_id':x['claim_id'],'review_key':x['review_key'],'evidence_ids':[z['evidence_id'] for z in x['evidence_context']],'verdict':'supported' if verdict['supported'] else 'insufficient','reason':verdict['reason'],'condition':None})
                return ModelResponse({'content':json.dumps({'reviews':reviews})},(),ModelUsage(3,2,5),'stop')
            self.writer+=1
            if self.writer==1:return turn('read_sources',{'reads':[{'source_id':sid}],'rationale':'Read the document.'},'read')
            if self.writer==2:return turn('submit_research',{'report':item['claim'],'claims':[{'text':item['claim'],'source_id':sid,'quotation':item['source'],'role':'support'}],'limitations':[],'stopping_reason':'answered'},'submit')
            return ModelResponse({'role':'assistant','content':'No further evidence.'},(),ModelUsage(1,1,2),'stop')
    provider=Provider(); api.challenge_provider=provider
    started=client.post(path+'/runs',headers={**headers,'Idempotency-Key':uuid.uuid4().hex},json={'parent_version_id':topic['versions'][0]['version_id'],'discover_sources':False,'max_model_calls':5})
    assert started.status_code==202,started.text
    op=started.json()['operation']['operation_id']
    for n in range(500):
        state=client.get(path+'/runs/'+op,headers=headers).json()['operation']
        if state['status'] in ('completed','failed','cancelled'): break
        time.sleep(.01)
    else: raise RuntimeError('timeout')
    versions=client.get(path,headers=headers).json()['versions']; version=versions[-1]['content']; research=version.get('turns',[])[-1] if version.get('turns') else {}
    export=client.get(path+'/versions/'+versions[-1]['version_id']+'/export',headers=headers)
    exported=export.json() if export.status_code==200 else {'http_status':export.status_code}
    results.append({'id':item['id'],'expected':item['expected'],'blind_supported':verdict['supported'],'candidate':item['claim'],'version':version,'export':exported,'operation':state,'checkpoint':api.RESEARCH_RUN_INPUTS[op].get('runtime_checkpoint', {}),'status':research.get('status'),'report':research.get('report'),'question_parts':research.get('question_parts'),'writer_calls':provider.writer,'review_calls':provider.review})
print(json.dumps(results,ensure_ascii=False,indent=2))
