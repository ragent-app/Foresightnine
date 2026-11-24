"""A writer's report that cannot be delivered as a reviewed answer is kept as a labelled draft."""
import json
from hashlib import sha256
from datetime import datetime, timezone

from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_delivery import assess
from app.research_runtime import displayable_draft, latest_draft, run_research_agent
from app.research_semantic_review import parse_reviews
from app.research_sources import ResearchSource, SourcePermissions

TEXT = 'NVIDIA said Jensen Huang returns as CEO effective immediately. The stock closed at 165.05 USD.'
QUOTE = 'The stock closed at 165.05 USD.'
CLAIM = 'The stock closed at 165.05 USD.'
REPORT = ('## Why the stock moved\n\nThe most direct catalyst was the CEO returning, together with a broad rebound.'
          '\n\nThe stock closed at 165.05 USD. [[claim:{claim}]]')


def source():
    return ResearchSource(source_id='s1', owner_id='owner', title='Release', text=TEXT, kind='user_supplied',
                          obtained_at=datetime.now(timezone.utc), content_sha256=sha256(TEXT.encode()).hexdigest(),
                          permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))


def context():
    return ResearchExecutionContext(ResearchExecutionBudget(None, None, None, float('inf')), False,
                                    lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None)


def call(name, args, ident):
    tool = ModelToolCall(ident, name, json.dumps(args))
    return ModelResponse({'role': 'assistant', 'content': None, 'tool_calls': [{'id': ident, 'type': 'function',
                          'function': {'name': name, 'arguments': tool.arguments}}]}, (tool,), ModelUsage(1, 1, 2), 'tool_calls')


STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), ModelUsage(1, 1, 2), 'stop')


class Model:
    """Scripted writer; the reviewer gives one fixed verdict per candidate."""

    def __init__(self, turns, verdict):
        self.turns, self.verdict, self.reviews = iter(turns), verdict, 0

    def complete(self, messages, tools, **kwargs):
        if not tools and 'candidates' in json.loads(messages[1]['content']):
            self.reviews += 1
            payload = json.loads(messages[1]['content'])
            rows = [{**{k: c[k] for k in ('requirement_id', 'claim_id', 'review_key')},
                     'evidence_ids': [e['evidence_id'] for e in c['evidence_context']],
                     'verdict': self.verdict, 'reason': 'Supported; the causes are not analysed.', 'condition': None}
                    for c in payload['candidates']]
            return ModelResponse({'role': 'assistant', 'content': json.dumps({'reviews': rows})}, (), ModelUsage(4, 3, 7), 'stop')
        return next(self.turns, STOP)


def submit(report):
    return call('submit_research', {'report': report, 'claims': [{
        'text': CLAIM, 'source_id': 's1', 'quotation': QUOTE, 'character_start': TEXT.index(QUOTE),
        'character_end': TEXT.index(QUOTE) + len(QUOTE), 'role': 'support'}], 'limitations': [],
        'stopping_reason': 'answered', 'status': 'completed'}, 'b')


def run(verdict, report):
    model = Model([call('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'release'}, 'a'), submit(report)], verdict)
    return run_research_agent(question='Why did the stock move today?', sources=[source()], model=model, context=context()), model


def test_supported_premise_for_part_of_the_question_is_kept_and_the_writer_draft_is_shown():
    result, model = run('partial', REPORT.replace(' [[claim:{claim}]]', ''))
    assert model.reviews >= 1
    assert result.status == 'partial' and result.report == ''
    assert len(result.claims) == 1, 'a supported claim that answers part of the question is delivered'
    assert 'most direct catalyst' in result.draft_report
    part = result.question_parts[0]
    assert part['status'] == 'partial' and CLAIM in part['answer']


def test_rejected_candidate_is_dropped_but_the_writer_wording_is_still_shown_without_its_anchor():
    result, _ = run('weaker', REPORT.format(claim='claim-0123456789abcdef0123456789abcdef'))
    assert result.status == 'partial' and result.report == '' and result.claims == []
    assert 'most direct catalyst' in result.draft_report
    assert '[[claim:' not in result.draft_report, 'no anchor to a claim that was not delivered'


def test_draft_is_stale_after_a_newer_instruction_and_unknown_anchors_are_removed():
    state = {'_writer_draft': {'report': 'Old answer.', 'instruction_sequence': 0},
             'user_instructions': [{'sequence': 1, 'text': 'Also compare peers.'}]}
    assert latest_draft(state) == ''
    state['_writer_draft']['instruction_sequence'] = 1
    assert latest_draft(state) == 'Old answer.'
    text = 'A [[claim:c1]] B [[claim:c2]]\n\n[[table:t1]]\n\n[[chart:x]]'
    kept = displayable_draft(text, [{'claim_id': 'c1'}], {'tables': [{'table_id': 't1'}], 'charts': []})
    assert kept == 'A [[claim:c1]] B\n\n[[table:t1]]'


def test_partial_verdict_marks_the_requirement_partial_and_never_satisfied():
    claims = [{'claim_id': 'claim-1', 'role': 'support', 'text': CLAIM, 'citations': [{
        'source_id': 's1', 'quotation': QUOTE, 'location': {'character_start': TEXT.index(QUOTE),
                                                            'character_end': TEXT.index(QUOTE) + len(QUOTE)}}]}]
    state = {'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(TEXT)}], 'facts': [], 'calculations': []}
    sources = {'s1': source()}
    rows = assess('Why did the stock move today?', state, sources, claims=claims)
    from app.research_semantic_review import candidates
    items = candidates(rows, claims, state, sources, None)
    assert items
    state['_semantic_reviews'] = parse_reviews(json.dumps({'reviews': [{
        'requirement_id': i['requirement_id'], 'claim_id': i['claim_id'], 'review_key': i['review_key'],
        'evidence_ids': ['1'], 'verdict': 'partial', 'reason': 'Price only.', 'condition': None} for i in items]}), items)
    assert state['_semantic_reviews'][0]['verdict'] == 'partial'
    rows = assess('Why did the stock move today?', state, sources, claims=claims)
    row = next(r for r in rows if r.get('semantic_required'))
    assert row['status'] == 'partial' and row['references'] == [{'claim_id': 'claim-1'}]
