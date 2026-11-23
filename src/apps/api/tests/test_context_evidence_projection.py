"""Authorization and version boundaries across prior and resumed native turns."""
import json
from datetime import datetime, timezone
from hashlib import sha256
from app.research_context_manager import _prior_research, build_model_turn_input
from app.research_sources import ResearchSource, SourcePermissions

TEXT = 'Counterpoint: demand declined.'
HASH = sha256(TEXT.encode()).hexdigest()
NOW = datetime(2026, 4, 1, tzinfo=timezone.utc)


def source(*, allowed=True, text=TEXT):
    return ResearchSource(source_id='s1', owner_id='owner', title='exhibit', text=text, kind='user_supplied',
        obtained_at=NOW, published_at=NOW, content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=allowed, display=True, export=True))


def prior():
    cite = {'source_id':'s1', 'quotation':TEXT, 'character_start':0, 'character_end':len(TEXT)}
    return {'source_hashes':{'s1':HASH}, 'read_ranges':[dict(cite)], 'gaps':[{'code':'gap','message':TEXT}],
        'facts':[{'fact_id':'f1','label':'demand','value':'declined','unit':'text','period':'FY2025', **cite}],
        'claims':[{'claim_id':'c1','role':'counterargument','text':'Demand declined.', 'citations':[cite]}]}


def test_prior_and_runtime_never_replay_revoked_or_revised_text():
    eligible = source()
    summary, saved = _prior_research(prior(), [eligible], NOW)
    assert summary['facts'] and summary['claims'] and saved['prior_read_ranges']
    for changed in (source(allowed=False), source(text=TEXT+' revised')):
        denied_summary, denied_state = _prior_research(prior(), [changed], NOW)
        assert not denied_summary['facts'] and not denied_summary['claims'] and not denied_state['prior_facts']
        assert TEXT not in json.dumps(denied_summary)
        state = {'user':json.dumps({'question':'Q', 'research_as_of':NOW.isoformat(), 'sources':[{'source_id':'s1','content_sha256':HASH}], 'prior_research':summary}),
            'history':[{'assistant':{'role':'assistant','content':[]}, 'results':[{'tool_call_id':'r1','content':json.dumps({'text':TEXT}), 'is_error':False}]}],
            'read_ranges':[{'source_id':'s1','start':0,'end':len(TEXT)}], **saved}
        turn = build_model_turn_input(state=state, source_by_id={'s1':changed})
        assert TEXT not in turn.user and TEXT not in str(turn.history) and TEXT not in (turn.tail_note or '')
        assert not turn.history


def test_located_counterargument_restored_without_inventing_evidence():
    src = source()
    summary, saved = _prior_research(prior(), [src], NOW)
    state = {'user':json.dumps({'question':'Q','research_as_of':NOW.isoformat(), 'sources':[{'source_id':'s1','content_sha256':HASH}]}),
             'history':[], **saved}
    turn = build_model_turn_input(state=state, source_by_id={'s1':src})
    assert 'counterargument' in turn.user and TEXT in turn.user
    state['prior_read_ranges'] = []
    turn = build_model_turn_input(state=state, source_by_id={'s1':src})
    assert TEXT not in turn.user and (not turn.tail_note or TEXT not in turn.tail_note)


def test_calculation_projection_requires_both_inputs_and_survives_recovery():
    src = source()
    _, saved = _prior_research(prior(), [src], NOW)
    first = saved['prior_facts'][0]
    second = {**first, 'fact_id': 'f2', 'period': 'FY2024'}
    saved['prior_facts'].append(second)
    saved['prior_calculations'] = [{'calculation_id':'calc1', 'formula_id':'growth_percent',
                                    'value':'20', 'unit':'%', 'inputs':[{'fact_id':'f1'}, {'fact_id':'f2'}]}]
    state = {'user':json.dumps({'question':'Q', 'research_as_of':NOW.isoformat(), 'sources':[{'source_id':'s1','content_sha256':HASH}]}),
             'history':[], 'failed_attempts':[{'reason':'provider_timeout','interruption':'response_body_after_frames'}],
             'continuation_guidance':'Continue.', **saved}
    turn = build_model_turn_input(state=state, source_by_id={'s1':src})
    assert 'calc1' in turn.user and 'f1' in turn.user and 'f2' in turn.user
    state['prior_facts'] = [first]
    turn = build_model_turn_input(state=state, source_by_id={'s1':src})
    assert 'calc1' not in turn.user and 'f1' in turn.user


def test_search_snippet_is_removed_after_revocation_even_without_read():
    state = {'user': json.dumps({'question':'Q', 'research_as_of':NOW.isoformat(),
                                'sources':[{'source_id':'s1', 'content_sha256':HASH, 'eligibility':'eligible'}]}),
             'history':[{'assistant':{'role':'assistant','content':[]},
                         'results':[{'tool_call_id':'search', 'content':json.dumps({'hits':[{'source_id':'s1','snippet':TEXT}]}), 'is_error':False}]}],
             'read_ranges':[]}
    turn = build_model_turn_input(state=state, source_by_id={'s1':source(allowed=False)})
    assert TEXT not in str(turn.history) and not turn.history


def test_revoked_source_keeps_independent_complete_group():
    good = source().model_copy(update={'source_id':'s2'})
    bad = source(allowed=False)
    fact = {'fact_id':'f1', 'label':'sales', 'value':'120', 'unit':'USD', 'period':'2025',
            'source_id':'s2', 'quotation':TEXT, 'character_start':0, 'character_end':len(TEXT)}
    second = {**fact, 'fact_id':'f2', 'period':'2024', 'value':'100'}
    state = {'user':json.dumps({'question':'Q','research_as_of':NOW.isoformat(), 'sources':[
        {'source_id':'s1','content_sha256':HASH,'eligibility':'eligible'}, {'source_id':'s2','content_sha256':HASH,'eligibility':'eligible'}]}),
        'history':[{'assistant':{'role':'assistant','content':[]}, 'results':[]}],
        'read_ranges':[{'source_id':'s1','start':0,'end':len(TEXT)}, {'source_id':'s2','start':0,'end':len(TEXT)}],
        'facts':[fact,second], 'calculations':[{'calculation_id':'calc1','formula_id':'growth_percent','value':'20','unit':'%',
          'inputs':[{'fact_id':'f1'},{'fact_id':'f2'}]}]}
    turn = build_model_turn_input(state=state, source_by_id={'s1':bad,'s2':good})
    assert not turn.history
    payload = json.loads(turn.user)
    group = next(x for x in payload['located_evidence'] if x.get('calculation', {}).get('calculation_id') == 'calc1')
    assert {row['fact_id'] for row in group['facts']} == {'f1','f2'}
    assert all(row['citation']['source_id'] == 's2' and row['citation']['quotation'] == TEXT for row in group['facts'])


def test_identical_text_from_another_source_does_not_erase_projected_quote():
    first = source()
    second = source().model_copy(update={'source_id':'s2'})
    fact = {'fact_id':'f2','label':'demand','value':'declined','unit':'text','period':'FY2025',
            'source_id':'s2','quotation':TEXT,'character_start':0,'character_end':len(TEXT)}
    result = {'results':[{'status':'read','source_id':'s1','start':0,'end':len(TEXT),'text':TEXT,'content_sha256':HASH}]}
    state = {'user':json.dumps({'question':TEXT,'research_as_of':NOW.isoformat(),'sources':[
        {'source_id':'s1','content_sha256':HASH,'eligibility':'eligible'}, {'source_id':'s2','content_sha256':HASH,'eligibility':'eligible'}]}),
        'history':[{'assistant':{'role':'assistant','content':[]},'results':[{'tool_call_id':'r1','content':json.dumps(result),'is_error':False}]}],
        'read_ranges':[{'source_id':'s2','start':0,'end':len(TEXT)}], 'facts':[fact]}
    turn = build_model_turn_input(state=state, source_by_id={'s1':first,'s2':second})
    assert '"source_id": "s2"' in turn.tail_note and '"quotation": "' + TEXT + '"' in turn.tail_note
    state['facts'][0]['source_id'] = 's1'
    state['read_ranges'][0]['source_id'] = 's1'
    turn = build_model_turn_input(state=state, source_by_id={'s1':first,'s2':second})
    assert '"in_existing_input": true' in turn.tail_note and '"quotation"' not in turn.tail_note
