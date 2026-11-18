"""Bounded evidence-gap follow-up over already authorized research sources."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .operations import ApiProblem
from .phase3 import ProviderFailure
from .research_analysis import SYSTEM_PROMPT, AnalysisInterrupted, AnalysisResult, InvalidAnalysis, analyze
from .research_artifacts import prepare_run_claims
from .research_calculations import calculation_input_status
from .research_coordinator import ResearchExecutionContext, research_prompt_fits
from .research_discovery import execute_source_search
from .research_evidence import flag_partial_source_absence
from .research_gap_followup import build_gap_focus, merge_research_questions, remap_followup_artifacts
from .research_runs import ResearchRunRequest
from .research_requirements import combined_requirements, unresolved_requirement_indexes
from .research_sources import ResearchSource
from .research_workflow import ResearchWorkflow


def _merge_followup_evidence(analysis: AnalysisResult, extra: AnalysisResult) -> tuple[list[dict], list[dict]]:
    observations = [*analysis.absence_observations, *extra.absence_observations]
    segments = list(analysis.segments)
    for segment in extra.segments:
        if segment not in segments:
            segments.append(segment)
    return observations, segments


def _merge_followup_needs(analysis: AnalysisResult, extra: AnalysisResult, questions: list[dict]) -> list[dict]:
    needs = list(analysis.evidence_needs)
    for item in extra.evidence_needs:
        if not isinstance(item, dict):
            continue
        index = item.get('question_index')
        target = None
        if type(index) is int and 0 <= index < len(extra.research_questions):
            question = extra.research_questions[index].get('question')
            matches = [i for i, row in enumerate(questions) if isinstance(row, dict) and row.get('question') == question]
            if len(matches) == 1:
                target = matches[0]
        needs.append({**item, 'question_index': target,
                      **({'question_origin': 'unlinked_followup'} if target is None else {})})
    return needs[:50]


@dataclass
class GapStageResult:
    analysis: AnalysisResult
    claims: list[dict[str, Any]]
    status: str = 'not_requested'
    search_status: str = 'not_requested'
    search_hash: str | None = None
    tokens: int | None = None
    prompt_sha256: str | None = None
    targets: list[int] = field(default_factory=list)
    input_targets: list[int] = field(default_factory=list)
    question_targets: list[int] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    input_fact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def execute_gap_followup(*, request: ResearchRunRequest, analysis: AnalysisResult, claims: list[dict[str, Any]],
                         leads: list[dict[str, Any]], question: str, scope: str, sources: list[ResearchSource],
                         as_of: datetime | None, max_prompt_chars: int, operation_id: str,
                         provider: Any, generate: Any, evidence_provider: Any, search_provider_name: str,
                         convert_candidate: Callable[[object, str], Any], discovered: list[dict[str, Any]],
                         search_receipts: list[dict[str, Any]], context: ResearchExecutionContext,
                         planning_requirements: tuple[dict, ...] = (),
                         workflow: ResearchWorkflow, checkpoint: Callable[[GapStageResult], None]) -> GapStageResult:
    """Search for leads only with consent; analyze only the existing authorized sources."""
    result = GapStageResult(analysis, claims, search_status='not_started' if request.gap_search else 'not_requested')
    if not request.gap_followup:
        return result
    result.status = 'not_needed' if context.calls and analysis.claims else 'unavailable'
    requirement_gaps = unresolved_requirement_indexes(analysis, planning_requirements)
    target = build_gap_focus(question, claims, analysis.research_questions, requirement_gaps=requirement_gaps) if context.calls and sources else None
    if target is None:
        if request.gap_search and result.status == 'not_needed':
            result.search_status = 'no_gaps'
        return result
    result.targets = list(target.claim_indexes)
    result.input_targets = list(target.claim_indexes)
    result.question_targets = list(target.question_indexes)
    policy = getattr(provider, 'policy', None)
    input_limit, output_limit = getattr(policy, 'max_input_tokens', None), getattr(policy, 'max_output_tokens', None)
    timeout = getattr(policy, 'timeout_seconds', None)
    if not isinstance(input_limit, int) or not isinstance(output_limit, int) or not isinstance(timeout, (int, float)):
        result.status = 'unavailable'
        return result
    per_call = input_limit + output_limit
    reason = context.model_exhaustion(per_call)
    if reason:
        result.status = 'unavailable' if reason == 'provider_limit' else 'budget_exhausted'
        return result
    attempted = False

    def generate_gap(prompt: str) -> tuple[dict[str, Any], int | None]:
        nonlocal attempted
        context.ensure_running()
        if request.gap_search:
            result.search_status = 'pending'
        checkpoint(result)
        if request.gap_search:
            existing_urls = {item.get('url') for item in discovered}
            for source in sources:
                if source.url:
                    parts = urlsplit(str(source.url))
                    existing_urls.add(urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')))
            discovery = workflow.run('gap_search', lambda: execute_source_search(provider=evidence_provider,
                provider_name=search_provider_name, query=target.search_query, convert=convert_candidate,
                as_of=as_of, context=context, existing_urls=existing_urls,
                capacity=max(0, 10 - len(discovered)), stage='gap'), lambda item: item.status)
            result.search_status = discovery.status
            search_receipts.append({'stage': 'gap', 'status': discovery.status, 'attempted': discovery.attempted,
                                    'reason': discovery.reason, 'retryable': discovery.retryable, 'omitted': discovery.omitted})
            if discovery.attempted:
                result.search_hash = sha256(target.search_query.encode('utf-8')).hexdigest()
            discovered.extend(discovery.candidates)
            if discovery.status in {'completed', 'partial'}:
                result.notes.append('Additional gap-search results are unverified leads; they are not evidence or model input.')
            if discovery.omitted:
                result.notes.append('Some gap-search candidates were invalid or exceeded the display limit and were omitted.')
            if discovery.status == 'failed':
                result.notes.append('Additional gap-focused source search failed or was rate limited.')
            checkpoint(result)
        remaining = context.budget.deadline - context.now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        if not research_prompt_fits(prompt, SYSTEM_PROMPT, input_limit):
            raise ProviderFailure('provider_budget_exhausted', 'Research input budget is exhausted.', retryable=False)
        call_id = f'{operation_id}:gap-followup'
        before = context.start_model(call_id, per_call)
        attempted = True
        result.prompt_sha256 = sha256(prompt.encode('utf-8')).hexdigest()
        try:
            raw, reported = context.invoke_model(call_id, generate, prompt, cancel_event=context.cancel_event(), system=SYSTEM_PROMPT,
                                     timeout_seconds=min(float(timeout), remaining))
            result.tokens = reported if type(reported) is int and reported >= 0 else None
            return raw, result.tokens
        except ProviderFailure as error:
            result.tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            context.finish_model(call_id, result.tokens, before)

    try:
        targeted_questions = [analysis.research_questions[index]['question'] for index in target.question_indexes]
        targeted_requirements = tuple(item for item in planning_requirements if isinstance(item, dict) and
                                      item.get('question') in targeted_questions)
        extra = analyze(target.question, sources, as_of, generate_gap, max_prompt_chars=max_prompt_chars,
                        should_stop=lambda: context.now() >= context.budget.deadline, scope=scope,
                        planning_steps=targeted_questions, planning_requirements=targeted_requirements,
                        require_counterarguments=request.require_counterarguments or request.counterpoint_followup)
        if not attempted:
            result.status = 'input_unavailable'
            return result
        old_count = len(analysis.claims)
        fact_id_map: dict[str, str] = {}
        calculation_index_map: dict[int, int] = {}
        facts, calculations, omissions = remap_followup_artifacts(extra.facts, extra.calculations,
            operation_id=operation_id, existing_fact_ids={fact['fact_id'] for fact in analysis.facts if isinstance(fact.get('fact_id'), str)},
            existing_facts=analysis.facts, max_facts=max(0, 2000 - len(analysis.facts)),
            max_calculations=max(0, 2000 - len(analysis.calculations)), fact_id_map=fact_id_map,
            calculation_index_map=calculation_index_map)
        for calculation in calculations:
            calculation['input_status'] = calculation_input_status(calculation, [*analysis.facts, *facts])
        support_room = max(0, 30 - old_count)
        extra_claims = [{**claim, 'suggested_calculations': [
            {'calculation_index': len(analysis.calculations) + calculation_index_map[item['calculation_index']]}
            for item in claim.get('suggested_calculations', []) if isinstance(item, dict) and
            type(item.get('calculation_index')) is int and item['calculation_index'] in calculation_index_map]}
            for claim in extra.claims[:support_room]]
        result.targets = [i if i < old_count else i + len(extra_claims) for i in result.input_targets]
        index_map = {i: old_count + i for i in range(len(extra_claims))}
        counter_room = max(0, 20 - len(analysis.counterarguments))
        extra_counterpoints = []
        for original in extra.counterarguments[:counter_room]:
            item = dict(original)
            item['suggested_calculations'] = [
                {'calculation_index': len(analysis.calculations) + calculation_index_map[ref['calculation_index']]}
                for ref in item.get('suggested_calculations', []) if isinstance(ref, dict) and
                type(ref.get('calculation_index')) is int and ref['calculation_index'] in calculation_index_map]
            targets = item.get('suggested_responds_to_claim_indexes', [])
            if isinstance(targets, list):
                mapped = [index_map[i] for i in targets if type(i) is int and i in index_map]
                if mapped:
                    item['suggested_responds_to_claim_indexes'] = mapped
                else:
                    item.pop('suggested_responds_to_claim_indexes', None)
            extra_counterpoints.append(item)
        result.segments = list(extra.segments)
        result.input_fact_ids = list(extra.available_fact_ids)
        result.status = 'completed' if extra_claims or extra_counterpoints or facts or calculations else 'no_evidence'
        if len(extra.claims) > support_room or len(extra.counterarguments) > counter_room or omissions:
            result.status = 'partial'
        # Observation references were assigned to the follow-up's own
        # retained claims before this merge. Preserve those opaque references;
        # never interpret their original model indexes against earlier claims.
        # Both lists contain actual model-input ranges, not retrieved leads.
        # Preserve the original source/offset metadata and deduplicate only
        # identical records; distinct context groups must remain distinct.
        combined_absences, _ = _merge_followup_evidence(analysis, extra)
        merged_questions = merge_research_questions(analysis.research_questions, extra.research_questions, index_map)
        merged_links = list(analysis.requirement_links)
        for link in extra.requirement_links:
            if not isinstance(link, dict) or not isinstance(link.get('question'), str):
                continue
            matches = [i for i, row in enumerate(merged_questions) if isinstance(row, dict) and
                       row.get('question') == link['question']]
            if len(matches) != 1:
                continue
            merged_links.append({**link, 'question_index': matches[0],
                'fact_ids': [fact_id_map[item] for item in link.get('fact_ids', []) if item in fact_id_map],
                'calculation_indexes': [len(analysis.calculations) + calculation_index_map[item]
                                        for item in link.get('calculation_indexes', []) if item in calculation_index_map]})
        result.analysis = replace(analysis, claims=[*analysis.claims, *extra_claims],
            counterarguments=[*analysis.counterarguments, *extra_counterpoints], facts=[*analysis.facts, *facts],
            calculations=[*analysis.calculations, *calculations],
            absence_observations=combined_absences[:50],
            evidence_needs=_merge_followup_needs(analysis, extra, merged_questions),
            requirement_links=merged_links[:50],
            discovered_requirements=list(combined_requirements(analysis.discovered_requirements, extra)),
            selection_diagnostics=[*analysis.selection_diagnostics,
                *[{**entry, 'stage': 'gap_followup'} for entry in extra.selection_diagnostics]][:100],
            research_questions=merged_questions,
            source_ids=list(dict.fromkeys([*analysis.source_ids, *extra.source_ids])),
            limitations=list(dict.fromkeys([*analysis.limitations, *extra.limitations,
                'The evidence-gap follow-up is model generated and is not independent verification.',
                *(['Some absence observations were omitted because the observation limit was reached.'] if len(combined_absences) > 50 else []),
                *(['Some follow-up facts or calculations were omitted because their identifiers or limits could not be reconciled.'] if omissions else [])])))
        result.claims = prepare_run_claims(result.analysis, sources, as_of, leads)
        new_indexes = [*range(old_count, old_count + len(extra_claims)),
            *range(len(result.analysis.claims) + len(result.analysis.counterarguments) - len(extra_counterpoints),
                   len(result.analysis.claims) + len(result.analysis.counterarguments))]
        for index in new_indexes:
            result.claims[index] = flag_partial_source_absence([result.claims[index]], sources, result.segments)[0]
    except (ProviderFailure, InvalidAnalysis, AnalysisInterrupted, ApiProblem) as error:
        context.ensure_running()
        if isinstance(error, InvalidAnalysis):
            context.mark_model_processing_error(f'{operation_id}:gap-followup', 'contract_validation')
        result.status = 'failed'
        result.error = error.code if isinstance(error, ProviderFailure) else 'gap_followup_unavailable'
    return result
