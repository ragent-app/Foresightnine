"""Version-local review queue derived from recorded research state.

The queue does not establish semantic truth or prove the absence of gaps.
It contains only identifiers and indexes, not source text or new model claims.
"""
from __future__ import annotations

from typing import Any


CLAIM_EVIDENCE_WARNINGS = (
    'counterpoint_relevance_unverified', 'percentage_unit_unverified', 'prior_findings_scope_unverified',
    'source_scope_absence_unverified', 'causal_attribution_unverified', 'uncited_exact_dates',
    'publication_date_warnings', 'rate_unit_warnings', 'uncited_amounts', 'uncited_claim_amounts',
    'uncited_dollar_amounts', 'uncited_quarters', 'balance_direction_warnings', 'uncited_percentages',
    'uncited_percentage_bounds', 'reused_percentages', 'uncited_basis_points',
    'uncited_percentage_points', 'uncited_fact_values', 'citation_context_warnings',
)
MAX_REFS = 50


def research_gaps(*, claims: list[dict], calculations: list[dict], sources: list[dict],
                  calculation_differences: list[dict], amount_differences: list[dict],
                  run: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded checklist, without interpreting sources or model prose."""
    items: list[dict[str, Any]] = []
    def add(code: str, indexes: list[int] | None = None, *, kind: str | None = None) -> None:
        entry: dict[str, Any] = {'code': code}
        if indexes is not None:
            entry.update({'indexes': indexes[:MAX_REFS], 'count': len(indexes), 'truncated': len(indexes) > MAX_REFS})
        if kind is not None:
            entry['index_kind'] = kind
        items.append(entry)

    attempted = bool(run.get('operation_id'))
    if attempted and not sources:
        add('no_analyzed_sources')
    source_states = run.get('source_processing')
    if isinstance(source_states, list) and any(isinstance(item, dict) and
            (item.get('eligibility') != 'eligible' or item.get('initial_selection') != 'complete') for item in source_states):
        add('source_processing_incomplete')
    support = [i for i, claim in enumerate(claims) if isinstance(claim, dict) and claim.get('role') != 'counterargument']
    counter = [i for i, claim in enumerate(claims) if isinstance(claim, dict) and claim.get('role') == 'counterargument']
    if attempted and not support:
        add('no_findings')
    questions = run.get('research_questions')
    if isinstance(questions, list):
        unlinked_questions = [i for i, item in enumerate(questions) if isinstance(item, dict) and not item.get('claim_ids')]
        if unlinked_questions:
            add('subquestions_unlinked', unlinked_questions, kind='question')
    uncited = [i for i, claim in enumerate(claims) if isinstance(claim, dict) and
               (not isinstance(claim.get('citations'), list) or not claim['citations'] or claim.get('support_status') in {'unsupported', 'incomplete'})]
    if uncited:
        add('citations_missing_or_incomplete', uncited, kind='claim')
    warned = [i for i, claim in enumerate(claims) if isinstance(claim, dict) and
              any(claim.get(name) for name in CLAIM_EVIDENCE_WARNINGS)]
    if warned:
        add('evidence_warnings', warned, kind='claim')
    if support and run.get('require_counterarguments') is True and not counter:
        add('no_counterpoint')
    unlinked = [i for i in counter if not claims[i].get('responds_to_claim_ids') and
                not claims[i].get('suggested_responds_to_claim_ids')]
    if unlinked:
        add('counterpoint_target_unlinked', unlinked, kind='claim')
    unverified_calculations = [i for i, item in enumerate(calculations) if isinstance(item, dict) and item.get('input_status') != 'matched']
    if unverified_calculations:
        add('calculation_inputs_unverified', unverified_calculations, kind='calculation')
    if calculation_differences or amount_differences:
        add('calculation_or_amount_differences')
    if run.get('planning_requested') and run.get('planning_status') != 'completed':
        add('research_planning_incomplete')
    if run.get('gap_search_requested') and run.get('gap_search_status') not in {'completed', 'no_gaps'}:
        add('gap_source_search_incomplete')
    if run.get('gap_followup_requested') and run.get('gap_followup_status') not in {'completed', 'not_needed'}:
        add('gap_followup_incomplete')
    if run.get('support_review_requested') and run.get('support_review_status') != 'complete':
        add('model_review_incomplete')
    if run.get('support_review_requested') and run.get('require_counterarguments') is True:
        unreviewed_relation = [i for i in counter if not isinstance(claims[i].get('support_review'), dict) or
                               claims[i]['support_review'].get('relevance') != 'relevant']
        if unreviewed_relation:
            add('counterpoint_relevance_unverified', unreviewed_relation, kind='claim')
    if run.get('counterpoint_requested') and run.get('counterpoint_status') not in {'completed', 'not_needed'}:
        add('counterpoint_followup_incomplete')
    if attempted and run.get('status') == 'partial':
        add('last_run_partial')
    return {'schema_version': '1', 'index_scope': 'research_version',
            'meaning': 'recorded_review_cues_not_a_quality_verdict', 'items': items}
