"""No network: exercise frozen runtime, prompt exposure and submission receipts."""
import importlib.util, json, sys, tempfile
from pathlib import Path
here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('worker',here/'worker.py'); w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
turns=[]
class Dummy:
    policy=w.prior.ProviderPolicy(max_output_tokens=2048,timeout_seconds=90)
    def __init__(self, actions): self.actions=iter(actions)
    def estimate_turn_input(self,turn): return 2000
    def complete_turn(self,turn,**kw):
        turns.append(turn)
        name,args,cid=next(self.actions)
        block={'type':'tool_use','id':cid,'name':name,'input':args}
        return ModelResponse({'role':'assistant','content':[block]},(ModelToolCall(cid,name,args),),ModelUsage(0,0,0),'tool_use','tool_calls')
def run(arm, actions):
    w.prior.provider=lambda:Dummy(actions)
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'result.json'; sys.argv=['worker','D1',arm,str(path)]; w.main(); result=json.loads(path.read_text())
    assert not result.get('error_type'),result
    assert result['search_calls']==0
    return result
read=lambda i:('read_sources',{'reads':[{'source_id':'exhibit'}],'rationale':'read'},str(i))
a=run('A',[read(i) for i in range(4)]); at=turns[:]; turns.clear()
b=run('B',[read(i) for i in range(4)]); bt=turns[:]; turns.clear()
assert len(at)==len(bt)==4
for x,y in zip(at[:3],bt[:3]): assert x==y
assert at[3].system==bt[3].system and at[3].user==bt[3].user and at[3].tools==bt[3].tools and at[3].history==bt[3].history
assert bt[3].tail_note == ((at[3].tail_note+'\n') if at[3].tail_note else '')+w.INSTRUCTION
assert [x['injected'] for x in b['prompts']]==[False,False,False,True]
assert a['prompt_exposure']=='not_applicable_control' and b['prompt_exposure']=='provider_entered'
assert all(x['before_sha256']==x['after_sha256'] for x in b['build_observations'][:3])
assert b['build_observations'][3]['before_sha256']!=b['build_observations'][3]['after_sha256']
partial={'report':'The exhibit has gaps.','claims':[],'limitations':['Not enough information.'],'stopping_reason':'out of material','status':'partial'}
c=run('B',[('submit_research',partial,'p')]); turns.clear()
assert c['reason']=='submitted' and c['status']=='partial' and c['state_submission']['status']=='partial',c
assert c['submit_handler_receipts'][0]['early_outcome']=={'status':'partial','reason':'submitted'}
assert c['submit_research_calls'][0]['result'] is None and c['prompt_exposure']=='not_sent'
invalid={'report':'x'}
d=run('A',[('submit_research',invalid,'bad'),read(1),read(2),read(3)]); turns.clear()
assert json.loads(d['submit_research_calls'][0]['result'])['status']=='invalid' and d['submission_acceptance']=='rejected_or_unexecuted',d
original_case=dict(w.CASES['cases'][0])
w.CASES['cases'][0].update(question='What was revenue?',snapshot='Revenue was 100 USD.')
completed={'report':'Revenue was 100 USD.','claims':[{'text':'Revenue was 100 USD.','source_id':'exhibit','quotation':'Revenue was 100 USD.','role':'support'}],'limitations':[],'stopping_reason':'answered','status':'completed'}
e=run('A',[read(0),('submit_research',completed,'complete')]); turns.clear()
w.CASES['cases'][0].update(original_case)
assert e['reason']=='submitted' and e['status']=='completed' and e['state_submission']['status']=='completed',e
assert e['report']=='Revenue was 100 USD.' and e['submit_handler_receipts'][0]['early_outcome']=={'status':'completed','reason':'submitted'}
print('offline same inputs, last-call exposure, completed and partial acceptance, invalid rejection passed')
class Truncated(Dummy):
    def complete_turn(self,turn,**kw):
        return w.messages.AnthropicMessagesProvider._build_response([
            {'type':'thinking','thinking':'NEVER_SAVE_THIS','signature':'secret'},
            {'type':'tool_use','id':'fake','name':'submit_research','input':partial}
        ], 'max_tokens', ModelUsage(1,2,3))
w.prior.provider=lambda:Truncated([])
with tempfile.TemporaryDirectory() as d:
    path=Path(d)/'result.json'; sys.argv=['worker','D1','A',str(path)]; w.main(); raw=path.read_text(); data=json.loads(raw)
assert 'NEVER_SAVE_THIS' not in raw and 'secret' not in raw
assert data['decoded_turns'][0]['finish_reason']=='max_tokens'
assert data['decoded_turns'][0]['blocks'][1]['name']=='submit_research'
assert not data['submit_handler_receipts'] and data.get('reason')=='provider_output_truncated',data
print('offline truncated response does not imply execution; structural redaction passed')
