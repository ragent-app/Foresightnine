"""Offline paired request-construction replay; no provider calls."""
import copy
import importlib.util
import json
import subprocess
import sys
import types
from types import SimpleNamespace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / 'src/apps/api'))
from app.research_context_manager import build_model_turn_input as candidate
from app.research_sources import ResearchSource, SourcePermissions

from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import canonical_json_dumps
provider = AnthropicMessagesProvider(
    SimpleNamespace(base_url='https://example.com', model='fixture', api_key='unused', allow_private_network=False),
    SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=256),
    anthropic_version='2023-06-01', auth_scheme='bearer', max_tokens=256)

baseline_code = subprocess.check_output(['git', 'show', '82900b4:src/apps/api/app/research_context_manager.py'], cwd=ROOT, text=True)
mod = types.ModuleType('app._context_baseline_replay')
mod.__package__ = 'app'
exec(compile(baseline_code, '82900b4:research_context_manager.py', 'exec'), mod.__dict__)
old = mod.build_model_turn_input
now = datetime(2026, 4, 1, tzinfo=timezone.utc)
text = 'Counterargument: demand fell. ' + 'Evidence ' * 300
h = sha256(text.encode()).hexdigest()
def source(sid='s1', allowed=True, body=text):
    return ResearchSource(source_id=sid, owner_id='owner', title='Exhibit', text=body, kind='user_supplied', obtained_at=now, published_at=now, content_sha256=sha256(body.encode()).hexdigest(), permissions=SourcePermissions(store=True, model_input=allowed, display=True, export=True))
src = source()
quote = text[:2800]
cite = {'source_id':'s1', 'quotation':quote, 'character_start':0, 'character_end':len(quote)}
facts = [{'fact_id':f'f{i}', 'label':'metric', 'value':str(i), **cite} for i in range(4)]
base = {'user':json.dumps({'question':'What changed?', 'research_as_of':now.isoformat(), 'sources':[{'source_id':'s1','content_sha256':h,'eligibility':'eligible'}]}), 'history':[], 'read_ranges':[{'source_id':'s1','start':0,'end':len(text)}], 'facts':facts, 'claims':[{'claim_id':'c1','role':'counterargument','text':'Demand fell', 'citations':[cite]}], 'calculations':[{'calculation_id':'calc1','formula_id':'sum','inputs':[{'fact_id':f'f{i}'} for i in range(4)]}]}
def exchange(n, content):
    return {'assistant':{'role':'assistant','content':[{'type':'tool_use','id':str(n),'name':'read_sources','input':{}}]}, 'results':[{'tool_call_id':str(n), 'content':json.dumps(content), 'is_error':False}]}
read = exchange('read', {'results':[{'status':'read','source_id':'s1','start':0,'end':len(text),'text':text,'content_sha256':h}]})
filler = exchange('filler', {'status':'empty'})
filler2 = exchange('filler2', {'status':'empty'})
scenarios = {'first':[read,filler,filler2], 'middle':[filler,read,filler2], 'last':[filler,filler2,read], 'no_history':[], 'recovery':[read], 'different_source':[exchange('other', {'results':[{'status':'read','source_id':'s2','start':0,'end':len(text),'text':text,'content_sha256':h}]})], 'revoked':[read], 'revised':[read]}
results = []
for name, history in scenarios.items():
    for arm, builder in [('A',old),('B',candidate)]:
        state = copy.deepcopy(base); state['history'] = copy.deepcopy(history)
        sources = {'s1': source(allowed=False) if name == 'revoked' else source(body=text+' revised') if name == 'revised' else src}
        if name == 'different_source':
            sources['s2'] = source('s2')
            payload = json.loads(state['user'])
            payload['sources'].append({'source_id':'s2','content_sha256':h,'eligibility':'eligible'})
            state['user'] = json.dumps(payload)
        if name == 'recovery':
            state.update(failed_attempts=[{'reason':'provider_timeout','interruption':'response_body_after_frames'}], continuation_guidance='Continue.')
        total = 0; seen = set(); excerpts = []; omitted = []
        for _ in range(3):
            turn = builder(state=state, source_by_id=sources)
            wire = canonical_json_dumps(provider._wire(turn, 256)).encode()
            total += len(wire)
            payload = json.loads(turn.user)
            projected = payload.get('located_evidence', [])
            if turn.tail_note and '[Located evidence; semantic support is not verified]\n' in turn.tail_note:
                block = json.loads(turn.tail_note.rsplit('[Located evidence; semantic support is not verified]\n',1)[1]); projected = block['groups']; omitted.append(block['omitted_for_capacity'])
            else:
                omitted.append((payload.get('located_evidence_omitted') or {}).get('count',0))
            full = wire.decode()
            # Required group IDs count only if their complete original text is in the actual request.
            if quote in full:
                for group in projected:
                    if group.get('claim',{}).get('claim_id') == 'c1': seen.add('counterargument')
                    if group.get('calculation',{}).get('calculation_id') == 'calc1' and len(group.get('facts',[])) == 4: seen.add('calculation')
            excerpts.append(full.count(quote))
        results.append({'scenario':name,'arm':arm,'calls':3,'cumulative_serialized_bytes':total,'complete_groups_deduplicated':len(seen),'complete_group_ids':sorted(seen),'groups_per_decimal_kb':round(len(seen)/(total/1000),6), 'quote_occurrences_per_request':excerpts,'capacity_omissions_per_request':omitted})
out = {'baseline':'82900b4','baseline_context_sha256':sha256(baseline_code.encode()).hexdigest(), 'candidate_context_sha256':sha256((ROOT / 'src/apps/api/app/research_context_manager.py').read_bytes()).hexdigest(), 'candidate_runtime_sha256':sha256((ROOT / 'src/apps/api/app/research_runtime.py').read_bytes()).hexdigest(), 'candidate':'working tree','protocol':'Three repeated constructed native turns per scenario; identical tools, evidence and source permissions; bytes are canonical Messages protocol bodies constructed offline, not billed tokens. Two synthetic necessary groups; no semantic or provider quality claim.', 'results':results}
Path(__file__).with_name('paired-replay.json').write_text(json.dumps(out, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(out, ensure_ascii=False, indent=2))
