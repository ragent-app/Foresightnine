"""Local-only tests for bounded acquisition-decision context and accounting."""
import json
from types import SimpleNamespace

from app.phase3 import ProviderFailure
from app.research_acquisition import SYSTEM, build_context, execute_decision
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext, research_prompt_fits


def test_context_pages_cover_candidates_ranges_and_feedback_without_cutting_json():
    requirements = [{'requirement_id': f'r{i}', 'question': f'Question {i}'} for i in range(8)]
    candidates = [{'candidate_id': f'lead:{i}', 'title': 'candidate ' + str(i) + ' x' * 80} for i in range(45)]
    ranges = [{'source_id': f's{i}', 'character_start': 0, 'character_end': 10, 'preview': 'text ' * 20} for i in range(40)]
    feedback = [{'requirement_id': f'r{i}', 'issues': ['unverified'], 'claim_summaries': [{'text': 'claim ' * 10}]} for i in range(8)]
    all_seen = {name: set() for name in ('requirements', 'candidates', 'ranges', 'feedback')}
    for page in range(45):
        prompt, omitted, visible = build_context(question='Research question', target=requirements[0],
            requirements=requirements, candidates=candidates, ranges=ranges, feedback=feedback,
            capabilities=['discover', 'read'], input_limit=6000, page=page)
        decoded = json.loads(prompt)
        assert research_prompt_fits(prompt, SYSTEM, 6000)
        assert decoded['page'] == page and decoded['omitted'] == omitted
        assert decoded['feedback'][0]['requirement_id'] == 'r0'
        for name in all_seen:
            all_seen[name].update(visible[name])
    assert len(all_seen['requirements']) == 8
    assert len(all_seen['candidates']) == 45
    assert len(all_seen['ranges']) == 40
    assert len(all_seen['feedback']) == 8


def test_unknown_external_call_does_not_report_unconfirmed_usage():
    settled = []
    ctx = ResearchExecutionContext(ResearchExecutionBudget(None, None, None, float('inf')), False,
        lambda: None, lambda: None, lambda *args: None, lambda call, tokens: settled.append((call, tokens)), lambda: None)
    provider = SimpleNamespace(policy=SimpleNamespace(max_input_tokens=6000, max_output_tokens=1500, timeout_seconds=5))
    def generate(prompt, *, cancel_event, system, timeout_seconds):
        raise ProviderFailure('provider_transport_error', 'unknown', retryable=True, reported_tokens=12)
    result = execute_decision(question='Q', target={'requirement_id': 'user_request'}, requirements=[], candidates=[],
        ranges=[], feedback=[], capabilities=['discover'], provider=provider, generate=generate, context=ctx, call_id='d1')
    assert result.status == 'failed' and result.usage is None
    # Unknown submission cannot be settled as provider-confirmed usage.
    assert settled == [('d1', None)]
    assert ctx.model_call_receipts['d1']['provider'] == 'unknown'
    assert ctx.model_call_receipts['d1']['settlement'] == 'unknown'
