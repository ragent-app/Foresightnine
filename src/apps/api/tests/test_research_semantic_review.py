from datetime import datetime, timezone
from types import SimpleNamespace
import json

from app.research_delivery import assess, assemble_parts
from app.research_semantic_review import candidates, parse_reviews
from app.research_runtime import _semantic_review_call, _recover_semantic_review
from app.model_protocol import ModelResponse, ModelUsage


def fixture(question='Can holders redeem?', text='Eligible holders may redeem at par.', claim='Eligible holders may redeem at par.'):
    source = SimpleNamespace(text=text, content_sha256='a' * 64, is_simulated=False, url=None,
                             permissions=SimpleNamespace(model_input=True, display=True),
                             published_at=datetime(2024, 1, 1, tzinfo=timezone.utc), revised_at=None)
    state = {'read_ranges': [{'source_id': 's', 'start': 0, 'end': len(text)}], 'events': []}
    claims = [{'claim_id': 'c', 'role': 'support', 'text': claim,
               'citations': [{'source_id': 's', 'quotation': text,
                              'location': {'character_start': 0, 'character_end': len(text)}}]}]
    return question, state, {'s': source}, claims


def response(items, verdict='supported', condition=None):
    return json.dumps({'reviews': [{**{k: item[k] for k in ('requirement_id', 'claim_id', 'review_key')},
                                    'evidence_ids': [str(i + 1) for i in range(len(item['evidence_context']))], 'verdict': verdict, 'condition': condition, 'reason': 'Checked the source.'}
                                   for item in items]})


def test_review_requires_exact_question_claim_and_current_source():
    q, state, sources, claims = fixture()
    rows = assess(q, state, sources, claims=claims)
    assert rows[0]['status'] == 'missing'
    items = candidates(rows, claims, state, sources, None)
    state['_semantic_reviews'] = parse_reviews(response(items), items)
    assert assess(q, state, sources, claims=claims)[0]['status'] == 'satisfied'
    assert assess('What is the market price?', state, sources, claims=claims)[0]['status'] == 'missing'
    sources['s'].content_sha256 = 'b' * 64
    assert assess(q, state, sources, claims=claims)[0]['status'] == 'missing'


def test_contradiction_and_conditional_need_exact_visible_condition():
    q, state, sources, claims = fixture(claim='Eligible holders may redeem at par.')
    rows = assess(q, state, sources, claims=claims)
    items = candidates(rows, claims, state, sources, None)
    state['_semantic_reviews'] = parse_reviews(response(items, 'contradicted'), items)
    assert assess(q, state, sources, claims=claims)[0]['status'] == 'missing'
    assert not parse_reviews(response(items, 'conditional', 'all holders'), items)
    state['_semantic_reviews'] = parse_reviews(response(items, 'conditional', 'Eligible holders'), items)
    rows = assess(q, state, sources, claims=claims)
    assert rows[0]['verdict'] == 'conditional'
    assert 'Eligible holders' in assemble_parts(q, state, sources, {'s'}, rows, claims)[0]['answer']


def test_review_call_is_isolated_accounted_and_cached():
    q, state, sources, claims = fixture()
    rows = assess(q, state, sources, claims=claims)
    class Context:
        diagnostic_context = {'operation_id': 'op'}
        private_diagnostics = {}
        model_call_receipts = {}
        def _persist_diagnostic(self, call_id): pass
        budget = SimpleNamespace(deadline=float('inf'))
        def prepare_model_call(self, *a): return 100, None
        def start_model(self, call, reservation): self.started = call; return (0, 0, 0)
        def finish_model(self, call, tokens, before): self.finished = (call, tokens)
        def cancel_event(self): return None
        def invoke_model(self, call, fn, turn, **kwargs): return fn(turn, **kwargs)
    class Model:
        def complete_turn(self, turn, **kwargs):
            assert not turn.history and not turn.tools
            items = json.loads(turn.user)['candidates']
            assert all('reasoning' not in x and 'qualification' not in x for x in items)
            return ModelResponse({'content': response(items)}, (), ModelUsage(3, 2, 5), 'stop')
    context, seen, saves = Context(), {'sig'}, []
    kwargs = dict(state=state, source_by_id=sources, as_of=None, model=Model(), context=context,
                  save=lambda: saves.append(state.get('_semantic_pending', {}).get('phase')), seen=seen,
                  rows=rows, claims=claims, signature='sig')
    _semantic_review_call(**kwargs)
    assert context.finished == (context.started, 5)
    assert saves[:2] == ['started_unknown', 'returned']
    assert assess(q, state, sources, claims=claims)[0]['status'] == 'satisfied'
    assert not state.get('_semantic_pending')
    _semantic_review_call(**kwargs)
    assert len([e for e in state['events'] if e['kind'] == 'semantic_review']) == 1
    assert _recover_semantic_review({'_semantic_pending': {'phase': 'started_unknown'}}, context, lambda: None) is False


def test_adjacent_exception_is_reviewed_and_response_cannot_swap_evidence():
    q, state, sources, claims = fixture(text='Eligible holders may redeem. Retail holders are excluded.')
    quote = 'Eligible holders may redeem.'
    claims[0]['citations'][0]['quotation'] = quote
    claims[0]['citations'][0]['location']['character_end'] = len(quote)
    items = candidates(assess(q, state, sources, claims=claims), claims, state, sources, None)
    assert 'Retail holders are excluded' in items[0]['evidence_context'][0]['text']
    raw = json.loads(response(items))
    raw['reviews'][0]['evidence_ids'] = ['2']
    assert not parse_reviews(json.dumps(raw), items)


def test_instruction_change_invalidates_cached_verdict():
    q, state, sources, claims = fixture()
    items = candidates(assess(q, state, sources, claims=claims), claims, state, sources, None)
    state['_semantic_reviews'] = parse_reviews(response(items), items)
    state['user_instructions'] = [{'sequence': 1, 'text': 'I mean retail holders.'}]
    assert assess(q, state, sources, claims=claims)[0]['status'] == 'missing'


def test_requirement_plan_preserves_original_and_invalidates_stale_versions():
    q, state, sources, claims = fixture()
    state['requirement_plan'] = [{'requirement_id': 'r1', 'instruction_sequence': 0, 'revision': 1,
                                  'source': {'sequence': 0, 'excerpt': q}, 'deliverable': 'Who is eligible?',
                                  'evidence_needed': 'issuer terms', 'priority': 'core',
                                  'user_conditions': [], 'assumptions': []}]
    rows = assess(q, state, sources, claims=claims)
    assert [r['question'] for r in rows] == [q, 'Who is eligible?']
    items = candidates(rows, claims, state, sources, None)
    state['_semantic_reviews'] = parse_reviews(response(items), items)
    assert all(r['status'] == 'satisfied' for r in assess(q, state, sources, claims=claims))
    state['user_instructions'] = [{'sequence': 1, 'text': 'Only retail holders.'}]
    rows = assess(q, state, sources, claims=claims)
    assert len(rows) == 1 and rows[0]['status'] == 'missing'


def test_returned_review_recovers_without_resending_model():
    q, state, sources, claims = fixture()
    rows = assess(q, state, sources, claims=claims)
    items = candidates(rows, claims, state, sources, None)
    state['pending'] = {'kind': 'tool_batch', 'call_id': 'writer-call', 'cursor': 2}
    state['_semantic_pending'] = {'phase': 'returned', 'call_id': 'review-call', 'before': [0, 0, 0],
                                  'usage': 5, 'content': response(items), 'items': items}
    class Context:
        model_call_receipts = {}
        def finish_model(self, call_id, tokens, before): self.finished = (call_id, tokens, before)
    class ForbiddenModel:
        def complete_turn(self, *args, **kwargs): raise AssertionError('must not resend')
    context = Context()
    assert _recover_semantic_review(state, context, lambda: None)
    assert context.finished == ('review-call', 5, (0, 0, 0))
    assert state['_semantic_replay_action'] == 'writer-call:2'
    _semantic_review_call(state=state, source_by_id=sources, as_of=None, model=ForbiddenModel(),
                          context=context, save=lambda: None, seen=set(), rows=rows, claims=claims, signature='x')
    assert assess(q, state, sources, claims=claims)[0]['status'] == 'satisfied'


def test_source_free_explanation_and_assumption_receipts_are_bound_to_user_input():
    from hashlib import sha256
    q = 'What is duration? Give an example.'
    claim = {'claim_id': 'c', 'role': 'support', 'text': 'Concept explanation: duration measures sensitivity to interest rates.', 'citations': []}
    def receipt(kind, text, seq):
        return {'kind': kind, 'source_id': None, 'sequence': seq, 'content_sha256': sha256(text.encode()).hexdigest(),
                'character_start': 0, 'character_end': len(text), 'quotation': text}
    state = {'question': q, 'read_ranges': [], '_claim_contexts': {'c': [receipt('user', q, 0), receipt('concept', claim['text'], None)]}}
    rows = assess(q, state, {}, claims=[claim])
    items = candidates(rows, [claim], state, {}, None)
    state['_semantic_reviews'] = parse_reviews(response(items), items)
    assert assess(q, state, {}, claims=[claim])[0]['status'] == 'satisfied'
    state['question'] = 'A different question'
    # Changed user provenance invalidates the original review.
    assert assess(q, state, {}, claims=[claim])[0]['status'] == 'missing'


def test_assumption_requires_conditional_verdict():
    from hashlib import sha256
    q = 'What if rates rise?'
    text = 'Assumption: rates rise; bond prices may fall.'
    claim = {'claim_id': 'c', 'role': 'support', 'text': text, 'citations': []}
    state = {'question': q, 'read_ranges': [], '_claim_contexts': {'c': [
        {'kind': 'assumption', 'source_id': None, 'sequence': None, 'content_sha256': sha256(text.encode()).hexdigest(),
         'character_start': 0, 'character_end': len(text), 'quotation': text}]}}
    items = candidates(assess(q, state, {}, claims=[claim]), [claim], state, {}, None)
    assert not parse_reviews(response(items), items)
    state['_semantic_reviews'] = parse_reviews(response(items, 'conditional', 'rates rise'), items)
    assert assess(q, state, {}, claims=[claim])[0]['status'] == 'satisfied'


def test_source_free_concept_full_submit_uses_accounted_reviewer():
    from tests.test_research_runtime_attention import _ctx, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    text = 'Concept explanation: duration measures sensitivity to interest rates.'
    submit = {'report': text, 'claims': [], 'source_free_answers': [{'basis': 'concept', 'text': text}],
              'limitations': [], 'stopping_reason': 'answered'}
    def review(turn, _):
        items = json.loads(turn.user)['candidates']
        return ModelResponse({'content': response(items)}, (), ModelUsage(5, 3, 8), 'stop')
    model = NativeTurnRecorder([_tool_response([('submit_research', submit, 'submit')]), review])
    state = []
    result = run_research_agent(question='What is duration?', sources=[], model=model, context=_ctx(),
                                checkpoint=lambda s: state.append(json.loads(json.dumps(s))))
    assert result.reason == 'submitted'
    assert result.status == 'completed'
    assert result.report == text
    assert any(e['kind'] == 'semantic_review' and e['usage'] == 7 for e in result.events)
    assert {e['kind'] for e in state[-1]['_semantic_reviews'][0]['evidence']} == {'user', 'concept'}


def test_mixed_user_and_public_receipts_are_submitted():
    from hashlib import sha256
    from tests.test_research_runtime_attention import _ctx, _source, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    q = 'My tuition is due next year. What does the issuer say about redemption?'
    source_text = 'Eligible holders may redeem at par.'
    src = _source(text=source_text)
    src.content_sha256 = sha256(source_text.encode()).hexdigest()
    text = 'My tuition is due next year. Eligible holders may redeem at par.'
    model = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read terms'}, 'read')]),
        _tool_response([('submit_research', {'report': text, 'claims': [{'text': text, 'role': 'support',
                         'source_id': 's1', 'quotation': source_text,
                         'user_inputs': [{'sequence': 0, 'quotation': 'My tuition is due next year.'}]}],
                         'limitations': [], 'stopping_reason': 'answered'}, 'submit')])])
    saved = []
    result = run_research_agent(question=q, sources=[src], model=model, context=_ctx(),
                                checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert result.reason == 'submitted'
    evidence = saved[-1]['_semantic_reviews'][0]['evidence']
    assert {e['kind'] for e in evidence} == {'source', 'user'}
    assert next(e for e in evidence if e['kind'] == 'user')['quotation'] == 'My tuition is due next year.'


def test_verifier_packet_redacts_identity_but_keeps_private_receipt_in_account():
    from tests.test_research_runtime_attention import _ctx, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    text = 'Concept explanation: duration measures rate sensitivity.'
    q = 'My name is Jane Smith. What is duration?'
    submit = {'report': text, 'claims': [], 'source_free_answers': [{'basis': 'concept', 'text': text}],
              'limitations': [], 'stopping_reason': 'answered'}
    model = NativeTurnRecorder([_tool_response([('submit_research', submit, 'submit')])])
    saved = []
    result = run_research_agent(question=q, sources=[], model=model, context=_ctx(),
                                checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert result.status == 'completed'
    assert 'Jane Smith' not in model.review_turns[0].user
    assert any(e['kind'] == 'user' and 'Jane Smith' in e['quotation']
               for e in saved[-1]['_semantic_reviews'][0]['evidence'])


def test_report_binding_preserves_negation_and_signed_values():
    from hashlib import sha256
    from tests.test_research_runtime_attention import _ctx, _source, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    for quote, false_report in [('Interest is not paid.', 'Interest is paid.'), ('The result is -10.', 'The result is 10.')]:
        src = _source(text=quote)
        src.content_sha256 = sha256(quote.encode()).hexdigest()
        model = NativeTurnRecorder([
            _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')]),
            _tool_response([('submit_research', {'report': false_report, 'claims': [{'text': quote, 'role': 'support',
                             'source_id': 's1', 'quotation': quote}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')])])
        out = run_research_agent(question='What is the result?', sources=[src], model=model, context=_ctx())
        assert out.status == 'completed'
        assert quote in out.report and false_report not in out.report


def test_report_cannot_drop_adjacent_exception_or_split_decimals():
    from tests.test_research_runtime_attention import _ctx, _source, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    quote = 'The yield is 3.5%. This applies only to accounts opened in 2025.'
    src = _source(text=quote)
    model = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('submit_research', {'report': 'The yield is 3.5%.', 'claims': [{'text': quote,
                         'role': 'support', 'source_id': 's1', 'quotation': quote}],
                         'limitations': [], 'stopping_reason': 'answered'}, 'submit')])])
    out = run_research_agent(question='What is the yield and its conditions?', sources=[src], model=model, context=_ctx())
    assert out.status == 'completed'
    assert quote in out.report
