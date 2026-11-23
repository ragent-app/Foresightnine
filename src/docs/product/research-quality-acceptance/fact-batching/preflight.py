"""Offline fixture: provider is a stub, with a search guard and prompt comparison."""
from contextlib import redirect_stdout
import importlib.util, json, os, sys, tempfile
from pathlib import Path
here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('worker',here/'worker.py'); w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
turns=[]
class Dummy:
    policy=w.prior.ProviderPolicy(max_output_tokens=2048,timeout_seconds=90)
    def estimate_turn_input(self,turn): return 2000
    def complete_turn(self,turn,**kw):
        turns.append(turn)
        args={'reads':[{'source_id':'exhibit'}],'rationale':'Read fixture'}
        b={'type':'tool_use','id':'offline','name':'read_sources','input':args}
        return ModelResponse({'role':'assistant','content':[b]},(ModelToolCall('offline','read_sources',args),),ModelUsage(0,0,0),'tool_use','tool_calls')
w.prior.provider=lambda:Dummy()
with tempfile.TemporaryDirectory() as d:
    for arm in ('A','B'):
        sys.argv=['worker','D1',arm,str(Path(d)/f'{arm}.json')]
        w.main()
        result=json.loads((Path(d)/f'{arm}.json').read_text())
        assert not result.get('error_type'),result
        assert result.get('search_calls') == 0
assert len(turns)>=2
# Four turns per arm; compare the first and verify only the system suffix changed.
a,b=turns[0],turns[len(turns)//2]
assert b.system == a.system+' '+w.INSTRUCTION
assert a.user == b.user and a.tools == b.tools and a.tail_note == b.tail_note
assert w.summarize_input({'facts':[{'value':123,'label':'text'}], 'not_schema':'secret'}) == {'facts':{'items':1,'item_shapes':[{'value':{'type':'int'},'label':{'chars':4}}]}}
print('offline prompt, schema, disabled search and structural redaction passed')
class Truncated(Dummy):
    def complete_turn(self,turn,**kw):
        return w.messages.AnthropicMessagesProvider._build_response([
            {'type':'thinking','thinking':'NEVER_SAVE_THIS','signature':'secret'},
            {'type':'tool_use','id':'fake','name':'record_facts','input':{'facts':[{'label':'Revenue','value':2}], 'rationale':'test'}}
        ], 'max_tokens', ModelUsage(1,2,3))
w.prior.provider=lambda:Truncated()
with tempfile.TemporaryDirectory() as d:
    sys.argv=['worker','D1','A',str(Path(d)/'result.json')]
    w.main()
    raw=(Path(d)/'result.json').read_text(); data=json.loads(raw)
    assert 'NEVER_SAVE_THIS' not in raw and 'secret' not in raw
    assert data['decoded_turns'][0]['finish_reason']=='max_tokens'
    assert data['decoded_turns'][0]['blocks'][1]['name']=='record_facts'
    assert data['decoded_turns'][0]['blocks'][1]['arguments_shape']['facts']['items']==1
print('offline truncated provider response structural capture passed')
