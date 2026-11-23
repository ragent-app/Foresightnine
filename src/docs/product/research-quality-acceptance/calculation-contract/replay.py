import copy, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from pydantic import ValidationError
from app.research_tools_registry import CalculateRequest, TOOLS
from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import ModelTurnInput
from app.research_tool_executor import _exec_calculate_financials
from app.research_calculations import calculate, calculation_input_status
from app.research_sources import ResearchSource, SourcePermissions

HERE = Path(__file__).resolve().parent
PRIOR = HERE.parent / 'last-call-submission'
cases = json.loads((HERE.parent / 'cases.json').read_text())
case = next(c for c in cases['cases'] if c['id'] == 'D1')
snapshot = case['snapshot']
source = ResearchSource(source_id='exhibit', owner_id='isolated', title='Synthetic exhibit', text=snapshot, kind='user_supplied', obtained_at=datetime(2026,11,23,tzinfo=timezone.utc), content_sha256=hashlib.sha256(snapshot.encode()).hexdigest(), permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))

def execute(request, facts, sources=None, hashes=None):
    state = {'facts': facts, 'calculations': [], 'source_hashes': hashes or {}}
    ctx = SimpleNamespace(state=state, source_by_id=sources if sources is not None else {'exhibit': source}, as_of=None, deps=SimpleNamespace(calculate=calculate, calculation_input_status=calculation_input_status))
    try:
        result = _exec_calculate_financials(ctx, SimpleNamespace(req=CalculateRequest.model_validate(request))).result
        return result['calculations'], state
    except (ValueError, ValidationError) as e:
        return str(e), state

output = {'batches': [], 'negative': {}, 'schema': {}}
for arm in ['A-1','A-2','B-1','B-2']:
    path = PRIOR / 'results' / f'D1-{arm}.json'
    raw = path.read_bytes(); trace = json.loads(raw)
    call = next(x for x in trace['tool_io'] if x['turn'] == 3 and x['tool'] == 'calculate_financials')
    facts = []
    for x in trace['tool_io']:
        if x['turn'] < 3 and x['tool'] == 'record_facts':
            facts.extend(json.loads(x['result'])['facts'])
    req = call['input']; rows, state = execute(req, facts)
    assert isinstance(rows, list), rows
    assert len(rows) == len(req['calculations']) == len(state['calculations'])
    assert Decimal(rows[0]['value']) == 20
    assert Decimal(rows[1]['value']) == 50
    if arm != 'A-1': assert Decimal(rows[2]['value']) == 1 and rows[2]['unit'] == 'USD per share'
    for spec, row in zip(req['calculations'], rows):
        ids = spec.get('fact_ids') or [spec['first_fact_id'],spec['second_fact_id']]
        assert [f['fact_id'] for f in row['inputs']] == ids
        assert all(any(f['fact_id'] == fid and f['quotation'] == snapshot[f['character_start']:f['character_end']] for f in facts) for fid in ids)
    # Compare old valid pair and ordered-array forms through the same real calculator.
    for form in ['pair', 'array']:
        single = copy.deepcopy(req)
        for spec in single['calculations']:
            ids = spec.get('fact_ids') or [spec['first_fact_id'], spec['second_fact_id']]
            spec.pop('fact_ids',None); spec.pop('first_fact_id',None); spec.pop('second_fact_id',None)
            if form == 'pair': spec.update(first_fact_id=ids[0], second_fact_id=ids[1])
            else: spec['fact_ids'] = ids
        equivalent, _ = execute(single, facts)
        assert isinstance(equivalent,list), equivalent
        assert [[r[k] for k in ('formula_id','value','unit','currency','inputs')] for r in equivalent] == [[r[k] for k in ('formula_id','value','unit','currency','inputs')] for r in rows]
    output['batches'].append({'trace':path.name,'trace_sha256':hashlib.sha256(raw).hexdigest(),'call_id':call['id'],'original_input':req,'results':rows})
    if arm == 'A-1':
        base, basefacts = req, facts
        old_call = next(x for x in trace['tool_io'] if x['turn'] == 4 and x['tool'] == 'calculate_financials')
        old_rows = json.loads(old_call['result'])['calculations']
        assert [[r[k] for k in ('formula_id','value','unit','currency','inputs')] for r in old_rows] == [[r[k] for k in ('formula_id','value','unit','currency','inputs')] for r in rows]
        output['old_receipt_comparison'] = {'trace':path.name, 'call_id':old_call['id'], 'matches':['formula_id','value','unit','currency','ordered_inputs']}

for name in ['conflict','reversed','half_pair','missing_fact','stale_source','missing_source','changed_quote','mixed_invalid','bad_periods']:
    req=copy.deepcopy(base); spec=req['calculations'][0]; sources=None; hashes=None; negative_facts=copy.deepcopy(basefacts)
    if name == 'conflict': spec['first_fact_id']='other'
    elif name == 'reversed': spec['fact_ids'].reverse()
    elif name == 'half_pair': spec.pop('second_fact_id')
    elif name == 'missing_fact': spec['fact_ids']=['missing',spec['fact_ids'][1]]; spec['first_fact_id']='missing'
    elif name == 'stale_source': hashes={'exhibit':'stale'}
    elif name == 'missing_source': sources={}
    elif name == 'changed_quote': negative_facts[0]['quotation'] = 'altered'
    elif name == 'mixed_invalid': req['calculations'][1]['first_fact_id']='other'
    elif name == 'bad_periods': spec['periods']=1
    error, state=execute(req,negative_facts,sources,hashes)
    assert isinstance(error,str) and not state['calculations'], (name,error,state)
    output['negative'][name]={'error':error, 'written':len(state['calculations'])}
schema=next(t for t in TOOLS if t.name=='calculate_financials').input_schema
plan=schema['$defs']['CalculationPlan']; assert 'fact_ids' in plan['required'] and plan['properties']['fact_ids']['type']=='array'; assert 'first_fact_id' not in plan['properties'] and 'second_fact_id' not in plan['properties']
provider = AnthropicMessagesProvider(SimpleNamespace(model='configured-model', base_url='https://example.com'), SimpleNamespace(max_input_tokens=None), anthropic_version='2023-06-01', auth_scheme='bearer', max_tokens=100)
wire = provider._wire(ModelTurnInput('system', 'question', (next(t for t in TOOLS if t.name=='calculate_financials'),), ()))
assert wire['tools'][0]['input_schema'] == schema
output['schema']=plan
output['messages_wire_tool']=wire['tools'][0]
print(json.dumps(output,ensure_ascii=False,indent=2,default=str))
