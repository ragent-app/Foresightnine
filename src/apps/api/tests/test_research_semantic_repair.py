"""Offline workflow checks for evidence-bound, bounded semantic repair."""
import json

from app.model_protocol import ModelResponse, ModelUsage
from app.research_runtime import run_research_agent
from tests.test_research_runtime_attention import _ctx, _source, _tool_response

TEXT = 'Eligible accounts receive 5% interest. This excludes accounts opened after 2025.'
QUESTION = 'What interest applies and who is excluded?'
UNQUALIFIED = 'Eligible accounts receive 5% interest.'
QUALIFIED = TEXT


class RepairModel:
    def __init__(self, *, improve, actionable=True):
        self.improve = improve
        self.actionable = actionable
        self.writes = 0
        self.reviews = 0
        self.feedback = []

    def complete_turn(self, turn, **kwargs):
        if not turn.tools:
            self.reviews += 1
            items = json.loads(turn.user)['candidates']
            reviews = []
            for item in items:
                corrected = item['claim'] == QUALIFIED
                reviews.append({**{k: item[k] for k in ('review_key', 'requirement_id', 'claim_id')},
                                'evidence_ids': [x['evidence_id'] for x in item['evidence_context']],
                                'verdict': 'supported' if corrected else 'weaker',
                                'reason': 'The second sentence states an exclusion.',
                                'condition': None,
                                'correction_possible_from_read_evidence': self.actionable and not corrected,
                                'repair_evidence_ids': [] if corrected or not self.actionable else ['1']})
            return ModelResponse({'content': json.dumps({'reviews': reviews})}, (), ModelUsage(4, 3, 7), 'stop')
        self.writes += 1
        if self.writes == 1:
            return _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')])
        if self.writes > 2:
            self.feedback.append(json.loads(turn.history[-1].results[-1].content))
        text = QUALIFIED if self.improve and self.writes > 2 else UNQUALIFIED
        submit = {'report': text, 'claims': [{'text': text, 'role': 'support', 'source_id': 's1', 'quotation': TEXT}],
                  'limitations': [], 'stopping_reason': 'answered'}
        return _tool_response([('submit_research', submit, 'submit-' + str(self.writes))])


def test_reviewer_repair_from_already_read_context_reaches_corrected_delivery():
    model, states = RepairModel(improve=True), []
    result = run_research_agent(question=QUESTION, sources=[_source(text=TEXT)], model=model, context=_ctx(),
                                checkpoint=lambda state: states.append(json.loads(json.dumps(state))))
    assert result.status == 'completed' and QUALIFIED in result.report
    assert model.writes == 3 and model.reviews == 2
    assert model.feedback[0]['reason'] == 'semantic_repair_available'
    item = model.feedback[0]['feedback'][0]
    assert item['review_reason_untrusted'] == 'The second sentence states an exclusion.'
    assert item['read_evidence'][0]['source_id'] == 's1'
    assert item['read_evidence'][0]['end'] == len(TEXT)
    assert len(states[-1]['_semantic_repair_attempts']) == 1


def test_identical_requirement_and_source_version_are_not_retried():
    model, states = RepairModel(improve=False), []
    result = run_research_agent(question=QUESTION, sources=[_source(text=TEXT)], model=model, context=_ctx(),
                                checkpoint=lambda state: states.append(json.loads(json.dumps(state))))
    assert result.status == 'partial'
    assert model.writes == 3 and model.reviews == 1
    assert len(states[-1]['_semantic_repair_attempts']) == 1


def test_new_material_needed_does_not_trigger_repair():
    model = RepairModel(improve=True, actionable=False)
    result = run_research_agent(question=QUESTION, sources=[_source(text=TEXT)], model=model, context=_ctx())
    assert result.status == 'partial'
    assert model.writes == 2 and model.reviews == 1


def test_exhausted_budget_does_not_trigger_repair():
    model = RepairModel(improve=True)
    result = run_research_agent(question=QUESTION, sources=[_source(text=TEXT)], model=model, context=_ctx(max_calls=3))
    assert result.status == 'partial'
    assert model.writes == 2 and model.reviews == 1


def test_submitted_claim_and_identical_checkpoint_review_once():
    class Model:
        def __init__(self):
            self.step = 0
            self.review_sizes = []
        def complete_turn(self, turn, **kwargs):
            if not turn.tools:
                items = json.loads(turn.user)['candidates']
                self.review_sizes.append(len(items))
                reviews = [{**{k: item[k] for k in ('review_key', 'requirement_id', 'claim_id')},
                            'evidence_ids': [x['evidence_id'] for x in item['evidence_context']],
                            'verdict': 'supported', 'reason': 'Supported.', 'condition': None} for item in items]
                return ModelResponse({'content': json.dumps({'reviews': reviews})}, (), ModelUsage(4, 3, 7), 'stop')
            self.step += 1
            if self.step == 1:
                return _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')])
            if self.step == 2:
                return _tool_response([('checkpoint_report_draft', {'clauses': [{'text': QUALIFIED, 'role': 'support',
                    'citations': [{'source_id': 's1', 'quotation': TEXT}]}]}, 'draft')])
            return _tool_response([('submit_research', {'report': QUALIFIED, 'claims': [{'text': QUALIFIED,
                'role': 'support', 'source_id': 's1', 'quotation': TEXT}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')])
    model = Model()
    result = run_research_agent(question=QUESTION, sources=[_source(text=TEXT)], model=model, context=_ctx())
    assert result.status == 'completed'
    assert model.review_sizes == [1]
    assert len(result.claims) == 1


def test_review_repair_key_does_not_change_when_writer_only_rephrases():
    from app.research_delivery import assess
    from app.research_semantic_review import candidates, parse_reviews, repair_feedback
    source = _source(text=TEXT)
    state = {'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(TEXT)}]}
    def evaluate(text):
        claims = [{'claim_id': text, 'role': 'support', 'text': text, 'citations': [{'source_id': 's1',
                   'quotation': TEXT, 'location': {'character_start': 0, 'character_end': len(TEXT)}}]}]
        rows = assess(QUESTION, state, {'s1': source}, claims=claims)
        items = candidates(rows, claims, state, {'s1': source}, None)
        raw = {'reviews': [{**{k: item[k] for k in ('review_key', 'requirement_id', 'claim_id')},
                            'evidence_ids': ['1'], 'verdict': 'weaker', 'reason': 'An exception is missing.',
                            'correction_possible_from_read_evidence': True, 'repair_evidence_ids': ['1']}
                           for item in items]}
        return rows, items, parse_reviews(json.dumps(raw), items)
    rows, items, reviews = evaluate(UNQUALIFIED)
    initial = repair_feedback(rows, items, reviews, set())
    assert len(initial) == 1
    rows, items, reviews = evaluate('Eligible accounts earn 5% interest.')
    assert not repair_feedback(rows, items, reviews, {initial[0]['attempt_key']})


def test_reviewer_cannot_request_repair_using_invalid_evidence_id():
    from app.research_delivery import assess
    from app.research_semantic_review import candidates, parse_reviews, repair_feedback
    source = _source(text=TEXT)
    state = {'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(TEXT)}]}
    claims = [{'claim_id': 'c', 'role': 'support', 'text': UNQUALIFIED, 'citations': [{'source_id': 's1',
               'quotation': TEXT, 'location': {'character_start': 0, 'character_end': len(TEXT)}}]}]
    rows = assess(QUESTION, state, {'s1': source}, claims=claims)
    items = candidates(rows, claims, state, {'s1': source}, None)
    raw = {'reviews': [{**{k: item[k] for k in ('review_key', 'requirement_id', 'claim_id')},
                        'evidence_ids': ['1'], 'verdict': 'weaker', 'reason': 'Some issue.',
                        'correction_possible_from_read_evidence': True, 'repair_evidence_ids': ['2']}
                       for item in items]}
    reviews = parse_reviews(json.dumps(raw), items)
    assert len(reviews) == 1 and not reviews[0]['correction_possible_from_read_evidence']
    assert not repair_feedback(rows, items, reviews, set())
