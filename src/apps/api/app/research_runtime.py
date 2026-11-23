"""Financial research loop using native model tools and run-scoped authority."""
from __future__ import annotations

from .research_partial_answer import merge_checkpoint_parts

from .research_delivery import assess as assess_delivery, assemble_parts, incomplete as incomplete_delivery
from .research_report_grounding import recover_draft_claims, eligible_prior_claims

from dataclasses import asdict, dataclass, field
from datetime import datetime
import inspect
import json
import logging
import math
import os
from time import sleep
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from uuid import uuid4

from . import research_run_insights as insights
from .chat_research_adapter import ChatResearchAdapter
from .issuer_documents import IssuerDocuments
from .model_protocol import ModelUsage
from .phase3 import ProviderFailure
from .research_calculations import calculate, calculation_input_status
from .research_charts import build_research_chart
from .research_context_manager import (
    DEFAULT_L1_COMPACT_TRIGGER_CHARS,
    DEFAULT_L1_KEEP_RECENT,
    L1_HARD_LIMIT_FACTOR,
    L1_LOW_WATER_RATIO,
    MAX_CONTEXT_RECEIPTS,
    MIN_COMPACTION_SAVINGS_CHARS,
    PRIOR_LIMITS,
    PRIOR_RESEARCH_TOKEN_BUDGET,
    _acquired_url_set,
    _build_failure_awareness_note,
    _canonical_url_safe,
    _coerce_model_usage,
    _compaction_salience_tier,
    _default_cache_usage,
    _extract_assistant_tool_calls,
    _extract_compaction_coverage,
    _extract_failure_metadata,
    _failure_awareness_payload,
    _history_serialized_chars,
    _infer_tool_name,
    _is_empty_or_low_yield_result,
    _l1_compaction_trigger_chars,
    _prior_research,
    _prior_research_token_budget,
    _prior_tokens,
    _record_cache_usage,
    _record_failed_attempt,
    _result_lead_urls,
    _sanitize_bounded_nonneg_int,
    _sanitize_failed_usage,
    _sanitize_timeout_kind,
    _strip_consumed_thinking,
    _turn_batch_usage,
    _user_with_failure_awareness,
    build_model_turn_input,
    compact_runtime_history,
)
from .research_diagnostics import safe_usage_diagnostics
from .research_fin_tools import FinDataToolbox
from .research_library import ResearchLibrary
from .research_library_tool import run_search_library, sync_library
from .research_live_stream import DeltaCoalescer
from .research_market_analytics import analyze_market_series
from .research_news_tool import DiscoverCallableProvider, run_news_search
from .research_ocr import OcrOutcome
from .research_source_policy import source_eligibility
from .research_sources import ResearchSource
from .research_statement_extraction import extract_statements
from .research_tables import build_research_table
from .research_tool_executor import (
    GAP_CODES,
    LOW_OCR_CONFIDENCE,
    MAX_LISTED_QUOTATION_FAILURES,
    MAX_QUESTION_LEADS,
    QuotationBatchError,
    QuotationLocateError,
    TOOL_FAILURE_GUIDANCE,
    ToolDependencies,
    ToolExecutionContext,
    _acquisition_failure_reason,
    _build_acquire_result,
    _document_pages,
    _duplicate_acquisition_result,
    _extract_source_outline,
    _fit_summary,
    _ocr_page_meta,
    _ocr_page_needs_check,
    _outline_source,
    _quotation_batch_error,
    _quotation_failure,
    _read_pages,
    _read_sources,
    _result_source_ids,
    _search_source,
    _search_source_text,
    _user_available_capabilities,
    _user_question_leads,
    _with_guidance,
    build_tool_batch_pending,
    build_turn_baseline,
    execute_tool_batch,
    gap,
    invalid_reason,
    locate_quotation,
    provider_gap,
)
from .research_tools_registry import (
    ACQUISITION_TOOLS as _ACQUISITION_TOOLS,
    BULK_COMPACT_TOOLS as _BULK_COMPACT_TOOLS,
    COVERAGE_UPDATING_TOOLS as _COVERAGE_UPDATING_TOOLS,
    DEFAULT_OUTLINE_ENTRIES,
    DEFAULT_SEARCH_HITS,
    FIN_TOOLS as _FIN_TOOLS,
    MAX_OUTLINE_ENTRIES,
    MAX_OUTLINE_TITLE_CHARS,
    MAX_READ_SOURCES_BATCH_CHARS,
    MAX_READ_SOURCES_TURN_CHARS,
    MAX_READ_SOURCE_SPAN_CHARS,
    MAX_SEARCH_HITS,
    MAX_SEARCH_SNIPPET_CHARS,
    MAX_TURN_TOOL_RESULT_CHARS,
    PROTECTED_COMPACT_TOOLS as _PROTECTED_COMPACT_TOOLS,
    REPORT_FORMAT_GUIDANCE,
    SEARCH_SNIPPET_TARGET_CHARS,
    SYSTEM,
    TOOLS,
    TOOLS_SERIALIZED_CHARS as _TOOLS_SERIALIZED_CHARS,
    AcquireRequest,
    ActivateMethodPackRequest,
    CalculateRequest,
    CalculationPlan,
    Claim,
    FactsRequest,
    OutlineSourceRequest,
    ProposedFact,
    ReadPagesRequest,
    ReadRequest,
    RequestOcrRequest,
    SearchSourceRequest,
    SourceRead,
    SubmitRequest,
    clip_text as _clip,
    is_source_eligible as _is_source_eligible,
    safe_host as _safe_host,
    summarize_tool_activity,
    utc_now_iso as _now_iso,
)

_logger = logging.getLogger(__name__)

# Stages that end before any request byte is written: a failure here is known not to have
# reached the provider, so retrying cannot duplicate a provider call or its charge.
PRE_SEND_STAGES = ('dns', 'tcp', 'tls')

_PARTIAL_REASON_LABELS = {
    'no_submission': '模型没有提交结果就停止了',
    'no_progress': '模型重复相同操作，证据没有变化，已停止',
    'native_tools_unavailable': '当前模型不支持研究工具调用',
    'external_result_unknown': '有一次外部请求的结果未知，需要核对后才能继续',
    'incomplete_model_response': '模型回复不完整，无法安全执行',
    'settlement_failed': '模型回复已保存，用量结算待核对',
    'provider_cancelled': '已取消',
    'acquisition_result_unknown': '资料获取结果未知',
    'search_result_unknown': '搜索结果未知，需要核对',
    'provider_timeout': '模型服务请求超时',
    'provider_transport_error': '与模型服务的连接中断',
    'provider_output_truncated': '模型输出达到单次长度上限而被截断',
    'provider_context_limit': '输入与对话上下文超出模型窗口上限',
    'provider_request_rejected': '模型服务拒绝了本次请求',
    'provider_output_refused': '模型服务拒绝生成该内容',
    'provider_unavailable': '模型服务暂时不可用',
    'provider_rate_limited': '模型服务请求过于频繁，请稍后重试',
    'provider_budget_exhausted': '已达到本次研究或账户的用量/时间预算上限',
    'provider_budget_unknown': '无法确认本次模型调用的用量边界',
    'provider_secret_missing': '当前环境未配置模型服务密钥',
    'provider_configuration_invalid': '模型服务配置无效',
    'invalid_provider_output': '模型服务返回了格式无效的响应',
    'provider_no_result': '模型请求没有返回可用结果',
    'token_budget_exhausted': 'Token 预算已用完',
    'submitted': '模型已提交部分结果',
}


def presend_retry_settings() -> tuple[int, float]:
    """Retries and base backoff (seconds) for model requests that failed before being sent.

    Configured per deployment; invalid values fall back to the defaults (4 retries, 1 s doubling,
    each wait capped at 8 s). Requests that may have been sent are never retried automatically.
    """
    try:
        retries = int(os.environ.get('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '4'))
        base = float(os.environ.get('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', '1'))
    except ValueError:
        return 4, 1.0
    if not 0 <= retries <= 8 or not math.isfinite(base) or not 0 <= base <= 10:
        return 4, 1.0
    return retries, base


@dataclass
class RuntimeOutcome:
    status: str
    reason: str
    report: str
    claims: list[dict]
    facts: list[dict]
    calculations: list[dict]
    gaps: list[dict]
    source_ids: list[str]
    messages: list[dict]
    events: list[dict]
    stopping_note: str | None = None
    partial_notes: str | None = None
    charts: list[dict] = field(default_factory=list)
    reconciliation: dict | None = None
    counter_evidence: dict | None = None
    coverage_map: dict | None = None
    method_packs: list[dict] = field(default_factory=list)
    attributions: list[dict] = field(default_factory=list)
    cache_usage: dict[str, Any] | None = None
    tables: list[dict] = field(default_factory=list)
    question_parts: list[dict] = field(default_factory=list)


def _extract_model_note(message: Any) -> str | None:
    if not isinstance(message, Mapping):
        return None
    content = message.get('content')
    if isinstance(content, str):
        text = content.strip()
        return text[:600] if text else None
    if isinstance(content, list):
        parts = [
            block['text'].strip()
            for block in content
            if isinstance(block, Mapping)
            and block.get('type') == 'text'
            and isinstance(block.get('text'), str)
            and block['text'].strip()
        ]
        if parts:
            joined = '\n'.join(parts).strip()
            return joined[:600] if joined else None
    return None


def _default_partial_notes(
    *,
    state: Mapping[str, Any],
    reason: str,
    eligible_sources: list[ResearchSource],
) -> str:
    reason_label = _PARTIAL_REASON_LABELS.get(reason)
    reason_text = f'{reason_label}（{reason}）' if reason_label else reason
    acquired_ids = [sid for sid in (state.get('acquired_source_ids') or []) if isinstance(sid, str) and sid]
    read_ranges = [r for r in (state.get('read_ranges') or []) if isinstance(r, Mapping)]
    read_source_ids = [r.get('source_id') for r in read_ranges if isinstance(r.get('source_id'), str) and r.get('source_id')]
    collected_count = len(dict.fromkeys([*(s.source_id for s in eligible_sources), *acquired_ids, *read_source_ids]))
    acquired_count = len(dict.fromkeys(acquired_ids))
    read_range_count = len(read_ranges)
    facts_count = len(state.get('facts') or []) if isinstance(state.get('facts'), list) else 0
    calcs_count = len(state.get('calculations') or []) if isinstance(state.get('calculations'), list) else 0
    source_part = (
        f'已收集资料 {collected_count} 份（本轮新获取 {acquired_count} 份）'
        if acquired_count > 0
        else f'已收集资料 {collected_count} 份'
    )
    return (
        f'部分结果已保留：停止原因：{reason_text}；{source_part}，'
        f'已读片段 {read_range_count} 段，已记录事实 {facts_count} 条，已完成计算 {calcs_count} 项。'
    )


def _initialize_runtime_state(
    *,
    question: str,
    sources: list[ResearchSource],
    model: Any,
    discover: Callable[[str], Any] | None,
    acquire: Callable[[str, str], Any] | None,
    fin_data: FinDataToolbox | None,
    capabilities: dict[str, Any] | None,
    initial_state: dict | None,
    as_of: datetime | None,
    prior: dict | None,
    focus: dict | None,
    clock: dict | None,
    ocr_pages: Callable[[ResearchSource, list[int]], OcrOutcome] | None,
    news_provider_list: list[Any],
    issuer_docs: IssuerDocuments | None,
) -> dict[str, Any]:
    if initial_state:
        state = json.loads(json.dumps(initial_state))
        for list_key in ('failed_attempts', 'charts', 'tables', 'analyzed_market_source_ids', 'method_packs', 'counter_searches'):
            if not isinstance(state.get(list_key), list):
                state[list_key] = []
        return state

    inventory = [
        {
            'source_id': s.source_id,
            'eligibility': code,
            **(
                {
                    'title': s.title,
                    'published_at': s.published_at.isoformat() if s.published_at else None,
                    'revised_at': s.revised_at.isoformat() if s.revised_at else None,
                    'total_characters': len(s.text),
                    'content_sha256': s.content_sha256,
                }
                if code == 'eligible'
                else {}
            ),
        }
        for s, code in source_eligibility(sources, as_of)
    ]
    available_capabilities = (
        dict(capabilities)
        if capabilities is not None
        else {
            'search_available': discover is not None,
            'acquire_available': acquire is not None,
        }
    )
    default_fin_caps = {
        'market_data_available': False,
        'market_data_key_required': False,
        'sec_financials_available': False,
        'filings_available': False,
        'cn_market_data_available': False,
        'cn_financials_available': False,
        'cn_financials_limited': False,
        'cn_data_provider': None,
        'cn_market_data_provider': None,
        'cn_providers_unreachable': [],
        'hk_market_data_available': False,
    }
    for flag, value in (fin_data.capabilities() if fin_data is not None else default_fin_caps).items():
        available_capabilities.setdefault(flag, value)
    available_capabilities.setdefault('sec_available', bool(available_capabilities.get('sec_financials_available')))
    available_capabilities.setdefault('search_library_available', True)
    available_capabilities.setdefault('news_search_available', bool(news_provider_list))
    if news_provider_list:
        available_capabilities.setdefault('news_search_providers', [str(getattr(p, 'name', 'unknown')) for p in news_provider_list])
    if ocr_pages is not None:
        available_capabilities.setdefault('pdf_ocr_on_demand', True)
    available_capabilities.setdefault('issuer_documents_available', issuer_docs is not None)
    # Method packs go right after the research clock in the first user message, never into SYSTEM/TOOLS.
    pack_texts, pack_records = insights.method_pack_texts(question, available_capabilities)
    payload: dict[str, Any] = {
        'question': question,
        'sources': inventory,
        'research_as_of': as_of.isoformat() if as_of is not None else None,
        **({'research_clock': clock} if clock is not None else {}),
        **({'method_packs': pack_texts} if pack_texts else {}),
        'available_capabilities': available_capabilities,
    }
    prior_state: dict[str, Any] = {}
    focus_ranges: list[dict[str, Any]] = []
    if focus is not None:
        payload['focus_citation'] = focus
        focus_ranges = [{'source_id': focus['source_id'], 'start': focus['character_start'], 'end': focus['character_end']}]
    if prior is not None:
        payload['prior_research'], prior_state = _prior_research(
            prior,
            sources,
            as_of,
            token_budget=_prior_research_token_budget(model),
        )
    return {
        'user': json.dumps(payload, ensure_ascii=False),
        'history': [],
        'events': [],
        'read_ranges': [],
        'facts': [],
        'calculations': [],
        'charts': [],
        'tables': [],
        'leads': _user_question_leads(question),
        'acquired_source_ids': [],
        'analyzed_market_source_ids': [],
        'failed_attempts': [],
        'pending': None,
        'focus_ranges': focus_ranges,
        'method_packs': pack_records,
        **prior_state,
    }


def _can_afford_presubmit_followup(context: Any) -> bool:
    if getattr(context, 'cost_limited', False):
        return False
    budget = getattr(context, 'budget', None)
    if budget is not None:
        max_calls = getattr(budget, 'max_model_calls', None)
        if type(max_calls) is int and context.calls >= max_calls:
            return False
        max_tot = getattr(budget, 'max_total_tokens', None)
        if type(max_tot) is int:
            acc = getattr(context, 'accounted_tokens', None)
            if acc is None or (max_tot - acc) <= 200:
                return False
        deadline = getattr(budget, 'deadline', None)
        if isinstance(deadline, (int, float)) and math.isfinite(deadline) and context.now() >= deadline:
            return False
    exhaustion = getattr(context, 'model_exhaustion', None)
    if callable(exhaustion):
        try:
            if exhaustion(None, required_calls=1) is not None:
                return False
        except TypeError:
            pass
    return True


def _model_accepts_stream_event(model: Any) -> bool:
    try:
        params = inspect.signature(model.complete_turn).parameters
    except (TypeError, ValueError):
        return False
    return 'on_stream_event' in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _invoke_model_turn(
    *,
    state: dict[str, Any],
    source_by_id: Mapping[str, ResearchSource],
    model: Any,
    context: Any,
    library_box: list[ResearchLibrary],
    fin_data: FinDataToolbox | None,
    on_submission: Callable[[], None] | None,
    on_stream_event: Callable[[dict[str, Any]], None] | None,
    emit_reset: Callable[[int | None, str], None],
    save: Callable[[], None],
    outcome_fn: Callable[..., RuntimeOutcome],
    presend_retries: int,
    presend_base: float,
    submission_retries: int,
    stream_call_seq: int,
    active_call_seq: int | None,
) -> tuple[str, RuntimeOutcome | None, int, int, int | None]:
    """Run L1 compaction, build ModelTurnInput, reserve budget, and invoke the model once."""
    if not callable(getattr(model, 'complete_turn', None)):
        return 'outcome', outcome_fn('partial', 'native_tools_unavailable', gaps=[gap('native_tools_unavailable')]), submission_retries, stream_call_seq, active_call_seq

    compact_runtime_history(
        state,
        keep_recent=DEFAULT_L1_KEEP_RECENT,
        trigger_chars=_l1_compaction_trigger_chars(
            state,
            model,
            context,
            system_chars=len(SYSTEM),
            tools_chars=_TOOLS_SERIALIZED_CHARS,
        ),
        force=False,
    )
    turn = build_model_turn_input(
        state=state,
        source_by_id=source_by_id,
        system_prompt=SYSTEM,
        tools=TOOLS,
        library=library_box[0] if library_box else None,
        has_fin_data=fin_data is not None,
    )
    tail_note = getattr(turn, 'tail_note', None)
    wire = json.dumps({'user': turn.user, **({'tail_note': tail_note} if tail_note else {})}, ensure_ascii=False)
    try:
        estimator = getattr(model, 'estimate_turn_input', None)
        if callable(estimator):
            class NativeBudgetProvider:
                def __getattr__(self, name: str) -> Any:
                    return getattr(model, name)

                def estimate_research_input(self, prompt: str, system: str, contract: Any) -> int:
                    return estimator(turn)

            budget_provider = NativeBudgetProvider()
        else:
            budget_provider = model
        reservation, output_cap = context.prepare_model_call(budget_provider, wire, SYSTEM)
        call_id = str(context.diagnostic_context.get('operation_id') or 'research') + ':research-agent:' + uuid4().hex
        before = context.start_model(call_id, reservation)
    except ProviderFailure as error:
        _record_failed_attempt(state, call_id=None, reason=error.code, error=error)
        save()
        return 'outcome', outcome_fn('partial', error.code, gaps=[provider_gap(error)]), submission_retries, stream_call_seq, active_call_seq

    stream_call_seq += 1
    active_call_seq = stream_call_seq
    model_started_at = _now_iso()
    pending_model: dict[str, Any] = {
        'kind': 'model',
        'call_id': call_id,
        'phase': 'retrying' if submission_retries > 0 else 'thinking',
        'started_at': model_started_at,
    }
    if submission_retries > 0:
        pending_model['retry_attempt'] = submission_retries
    state['pending'] = pending_model
    try:
        save()
    except BaseException:
        context.abort_model_before_call(call_id, before)
        raise

    tokens = None
    coalescer: DeltaCoalescer | None = None
    try:
        kwargs: dict[str, Any] = {'cancel_event': context.cancel_event()}
        if math.isfinite(context.budget.deadline):
            kwargs['timeout_seconds'] = max(0.001, context.budget.deadline - context.now())
        if output_cap is not None:
            kwargs['max_output_tokens'] = output_cap
        if on_submission is not None:
            kwargs['on_submission'] = on_submission
        if on_stream_event is not None and _model_accepts_stream_event(model):
            coalescer = DeltaCoalescer(call_seq=active_call_seq, sink=on_stream_event)
            kwargs['on_stream_event'] = coalescer.push_event
        try:
            response = context.invoke_model(call_id, model.complete_turn, turn, **kwargs)
        finally:
            if coalescer is not None:
                coalescer.close()
        usage = getattr(response, 'usage', None)
        tokens = getattr(usage, 'total_tokens', None) if usage is not None else None
    except ProviderFailure as error:
        received = getattr(error, 'provider_response_received', False) is True
        tokens = error.reported_tokens if received else None
        call_diag = context.private_diagnostics.get(call_id, {})
        diagnostic = call_diag.get('transport', {})
        diag_finish = call_diag.get('stop_reason') or call_diag.get('finish') or context.private_finish.get(call_id)
        diag_usage = tokens if type(tokens) is int and tokens >= 0 else call_diag.get('usage')
        not_started = not received and diagnostic.get('submission_state') == 'not_started'
        cancel_ev = kwargs.get('cancel_event')
        is_cancelled = error.code == 'provider_cancelled' or (cancel_ev is not None and cancel_ev.is_set())
        if not_started:
            context.abort_model_before_call(call_id, before)
            state['events'].append({
                'kind': 'model_failure',
                'call_id': call_id,
                'code': error.code,
                'external_state': 'not_started',
                'usage': None,
            })
            state['pending'] = None
            # Retry connection failures before the request was sent (DNS, TCP or TLS).
            # The prior tool results and history stay intact, and accounting is restored.
            if (
                error.retryable
                and error.code in ('provider_transport_error', 'provider_timeout')
                and submission_retries < presend_retries
                and any(
                    stage['stage'] in PRE_SEND_STAGES and stage['result'] in ('error', 'timeout')
                    for stage in diagnostic.get('stages', ())
                )
                and not any(stage['stage'] not in PRE_SEND_STAGES for stage in diagnostic.get('stages', ()))
            ):
                save()
                delay = min(presend_base * (2 ** submission_retries), max(presend_base, 8.0))
                if delay < context.budget.deadline - context.now():
                    emit_reset(active_call_seq, 'retrying')
                    active_call_seq = None
                    submission_retries += 1
                    if cancel_ev is None:
                        sleep(delay)
                    elif cancel_ev.wait(delay):
                        context.ensure_running()
                        return 'outcome', outcome_fn('partial', 'provider_cancelled', gaps=[gap('cancelled')]), submission_retries, stream_call_seq, active_call_seq
                    return 'retry', None, submission_retries, stream_call_seq, active_call_seq
            if error.code in ('provider_output_truncated', 'provider_context_limit', 'provider_timeout') or (not error.retryable and not is_cancelled):
                _record_failed_attempt(
                    state,
                    call_id=call_id,
                    reason=error.code,
                    finish_reason=diag_finish,
                    usage=diag_usage,
                    error=error,
                    call_diag=call_diag,
                )
            save()
            emit_reset(active_call_seq, 'cancelled' if is_cancelled else 'failed')
            active_call_seq = None
            return 'outcome', outcome_fn('partial', error.code, gaps=[provider_gap(error)]), submission_retries, stream_call_seq, active_call_seq
        if received:
            failure_meta = _extract_failure_metadata(error=error, call_diag=call_diag, reason=error.code)
            state['pending'] = {
                'kind': 'model_returned',
                'call_id': call_id,
                'usage': tokens,
                'before': list(before),
                'started_at': model_started_at,
                'finish_reason': diag_finish,
                'usage_detail': diag_usage,
                'error': {'code': error.code, 'message': str(error), 'retryable': error.retryable, **failure_meta},
            }
            save()
            return 'ok', None, submission_retries, stream_call_seq, active_call_seq

        context.finish_model(call_id, None, before)
        is_trunc_or_ctx = error.code in ('provider_output_truncated', 'provider_context_limit')
        failure_meta = _extract_failure_metadata(error=error, call_diag=call_diag, reason=error.code)
        should_record = is_trunc_or_ctx or error.code == 'provider_timeout' or failure_meta.get('interruption') == 'response_body_after_frames' or (not error.retryable and not is_cancelled)
        if should_record:
            _record_failed_attempt(
                state,
                call_id=call_id,
                reason=error.code,
                finish_reason=diag_finish,
                usage=diag_usage,
                error=error,
                call_diag=call_diag,
            )
        if is_trunc_or_ctx:
            state['events'].append({
                'kind': 'model_failure',
                'call_id': call_id,
                'code': error.code,
                'external_state': 'unknown',
                'usage': None,
            })
            state['pending'] = None
            save()
        elif should_record:
            save()
        emit_reset(active_call_seq, 'cancelled' if is_cancelled else 'failed')
        active_call_seq = None
        return 'outcome', outcome_fn('partial', error.code, gaps=[provider_gap(error)]), submission_retries, stream_call_seq, active_call_seq

    submission_retries = 0
    # Record this native turn's finish, not the legacy structured-output context variable.
    context.private_finish[call_id] = response.finish_reason or 'unknown'
    diag = context.private_diagnostics.setdefault(call_id, {})
    diag['finish'] = context.private_finish[call_id]
    usage_diag = safe_usage_diagnostics(usage)
    if usage_diag:
        diag['usage'] = usage_diag
    usage_cache = usage.usage_cache_summary() if isinstance(usage, ModelUsage) else None
    if isinstance(usage_cache, dict):
        diag['cache_telemetry'] = usage_cache
    context._persist_diagnostic(call_id)
    # The response and native call correlation are durable before billing settlement.
    state['pending'] = {
        'kind': 'model_returned',
        'call_id': call_id,
        'usage': tokens,
        'before': list(before),
        'phase': 'responding',
        'started_at': model_started_at,
        'message': response.message,
        'finish_reason': response.finish_reason,
        'stop_class': response.stop_class,
        'usage_detail': asdict(usage) if usage is not None else None,
        'calls': [{'id': c.id, 'name': c.name, 'arguments': c.arguments, 'raw_arguments': c.raw_arguments} for c in response.tool_calls],
    }
    save()
    return 'ok', None, submission_retries, stream_call_seq, active_call_seq


def _process_model_returned(
    *,
    state: dict[str, Any],
    source_by_id: Mapping[str, ResearchSource],
    as_of: datetime | None,
    context: Any,
    emit_reset: Callable[[int | None, str], None],
    save: Callable[[], None],
    outcome_fn: Callable[..., RuntimeOutcome],
    active_call_seq: int | None,
) -> tuple[RuntimeOutcome | None, str, list[Any], int, int | None]:
    """Settle a durable ``model_returned`` response and prepare its tool batch."""
    returned = state['pending']
    call_id = returned['call_id']
    try:
        context.finish_model(call_id, returned.get('usage'), tuple(returned['before']))
    except Exception:
        # The saved response is recoverable; never resubmit this request.
        _record_failed_attempt(
            state,
            call_id=call_id,
            reason='settlement_failed',
            finish_reason=returned.get('finish_reason'),
            usage=returned.get('usage') if returned.get('usage') is not None else returned.get('usage_detail'),
        )
        save()
        emit_reset(active_call_seq, 'failed')
        return outcome_fn('partial', 'settlement_failed', gaps=[gap('settlement_failed')]), call_id, [], 0, None

    receipt = context.model_call_receipts.get(call_id, {})
    accounting = receipt.get('settlement', 'unknown')
    call_cache = _record_cache_usage(state, call_id=call_id, usage_detail=returned.get('usage_detail'))
    cumulative_cache = dict(state['cache_usage']) if isinstance(state.get('cache_usage'), Mapping) else None
    diag = context.private_diagnostics.setdefault(call_id, {})
    if isinstance(call_cache, dict):
        diag['cache_telemetry'] = call_cache
    if isinstance(cumulative_cache, dict):
        diag['cumulative_cache_usage'] = cumulative_cache
    context._persist_diagnostic(call_id)
    receipt_entry: dict[str, Any] = {'call_id': call_id, 'provider': 'returned', 'settlement': accounting}
    if isinstance(call_cache, dict):
        receipt_entry['cache_telemetry'] = call_cache
        receipt_entry['input_tokens'] = call_cache.get('total_input_tokens')
        receipt_entry['cache_read_input_tokens'] = call_cache.get('cache_read_input_tokens')
        receipt_entry['cache_creation_input_tokens'] = call_cache.get('cache_creation_input_tokens')
        receipt_entry['cache_hit_ratio'] = call_cache.get('cache_hit_ratio')

    if 'error' in returned:
        error = returned['error']
        error_event: dict[str, Any] = {
            'kind': 'model_failure',
            'call_id': call_id,
            'code': error['code'],
            'external_state': 'returned',
            'usage': returned.get('usage'),
            'settlement': accounting,
        }
        if isinstance(call_cache, dict):
            error_event['cache_telemetry'] = call_cache
        state['events'].append(error_event)
        state.setdefault('model_receipts', []).append(receipt_entry)
        if (
            error['code'] in ('provider_output_truncated', 'provider_context_limit', 'provider_timeout')
            or error.get('interruption') == 'response_body_after_frames'
            or (error.get('retryable') is False and error['code'] != 'provider_cancelled')
        ):
            _record_failed_attempt(
                state,
                call_id=call_id,
                reason=error['code'],
                finish_reason=returned.get('finish_reason'),
                usage=returned.get('usage') if returned.get('usage') is not None else returned.get('usage_detail'),
                error=error,
                call_diag=context.private_diagnostics.get(call_id),
            )
        state['pending'] = None
        save()
        emit_reset(active_call_seq, 'cancelled' if error['code'] == 'provider_cancelled' else 'failed')
        return outcome_fn('partial', error['code'], gaps=[provider_gap(error)]), call_id, [], 0, None

    calls = [SimpleNamespace(**item) for item in returned['calls']]
    ids = [c.id for c in calls]
    if (
        not isinstance(returned['message'], dict)
        or returned.get('stop_class') == 'truncated'
        or (calls and returned.get('stop_class') != 'tool_calls')
        or any(
            not isinstance(c.id, str) or not c.id or not isinstance(c.name, str) or not c.name
            or (c.arguments is not None and not isinstance(c.arguments, dict))
            for c in calls
        )
        or len(ids) != len(set(ids))
    ):
        _record_failed_attempt(
            state,
            call_id=call_id,
            reason='incomplete_model_response',
            finish_reason=returned.get('finish_reason'),
            usage=returned.get('usage') if returned.get('usage') is not None else returned.get('usage_detail'),
        )
        state['pending'] = None
        state['stopped'] = 'incomplete_model_response'
        save()
        emit_reset(active_call_seq, 'failed')
        return outcome_fn('partial', 'incomplete_model_response', gaps=[gap('incomplete_model_response')]), call_id, [], 0, None

    state['history'].append({'assistant': returned['message'], 'results': []})
    model_note = _extract_model_note(returned['message'])
    model_event = {
        'kind': 'model',
        'call_id': call_id,
        'finish_reason': returned['finish_reason'],
        'external_state': 'returned',
        'usage': returned.get('usage'),
        'usage_detail': returned.get('usage_detail'),
        'settlement': accounting,
        'protocol_status': 'normalized',
        'evidence_status': 'not_assessed',
        'task_status': 'in_progress',
        'tool_call_ids': [c.id for c in calls],
    }
    if isinstance(call_cache, dict):
        model_event['cache_telemetry'] = call_cache
    if model_note is not None:
        model_event['note'] = model_note
    state['events'].append(model_event)
    state.setdefault('model_receipts', []).append(receipt_entry)
    if not calls:
        _record_failed_attempt(
            state,
            call_id=call_id,
            reason='no_submission',
            finish_reason=returned.get('finish_reason'),
            usage=returned.get('usage') if returned.get('usage') is not None else returned.get('usage_detail'),
        )
        state['pending'] = None
        save()
        emit_reset(active_call_seq, 'call_completed')
        return outcome_fn('partial', 'no_submission', gaps=[gap('no_submission')]), call_id, [], 0, None

    baseline = build_turn_baseline(state, source_by_id)
    state['pending'] = build_tool_batch_pending(
        state,
        call_id,
        returned['calls'],
        0,
        source_by_id=source_by_id,
        as_of=as_of,
        baseline=baseline,
    )
    save()
    return None, call_id, calls, 0, active_call_seq


def run_research_agent(
    *,
    question: str,
    sources: list[ResearchSource],
    model: Any,
    context: Any,
    discover: Callable[[str], Any] | None = None,
    acquire: Callable[[str, str], Any] | None = None,
    fin_data: FinDataToolbox | None = None,
    capabilities: dict[str, Any] | None = None,
    checkpoint: Callable[[dict], None] | None = None,
    on_submission: Callable[[], None] | None = None,
    on_stream_event: Callable[[dict[str, Any]], None] | None = None,
    initial_state: dict | None = None,
    as_of: datetime | None = None,
    prior: dict | None = None,
    focus: dict | None = None,
    clock: dict | None = None,
    now: Callable[[], datetime] | None = None,
    ocr_pages: Callable[[ResearchSource, list[int]], OcrOutcome] | None = None,
    news_providers: list[Any] | None = None,
    issuer_docs: IssuerDocuments | None = None,
) -> RuntimeOutcome:
    """Execute serially; a pending external call is never replayed on recovery.

    Checkpoint callbacks must durably save the provided complete state before returning.
    Source loading and authorization belong to the account-scoped caller; eligibility
    is checked again for each read and submission. ``focus`` is the resolved citation the
    user asked to examine; a restored checkpoint already carries it. ``ocr_pages(source, pages)`` recognises
    needs_ocr pages of a stored PDF and returns the stored new source version (the runtime stays storage
    agnostic); without it request_ocr reports ``ocr_unavailable``. Vision-model OCR calls are billed by the
    callback owner through the run's reserve/settle hooks and come back as ``OcrOutcome.events``.
    """
    del now
    source_by_id = {s.source_id: s for s in sources}
    news_provider_list = (
        list(news_providers)
        if news_providers is not None
        else ([DiscoverCallableProvider(discover)] if discover is not None else [])
    )
    state = _initialize_runtime_state(
        question=question,
        sources=sources,
        model=model,
        discover=discover,
        acquire=acquire,
        fin_data=fin_data,
        capabilities=capabilities,
        initial_state=initial_state,
        as_of=as_of,
        prior=prior,
        focus=focus,
        clock=clock,
        ocr_pages=ocr_pages,
        news_provider_list=news_provider_list,
        issuer_docs=issuer_docs,
    )

    def save() -> None:
        if checkpoint is not None:
            checkpoint(state)

    library_box: list[ResearchLibrary] = []

    def outcome(
        status: str,
        reason: str,
        *,
        report: str = '',
        claims: list[dict] | None = None,
        gaps: list[dict] | None = None,
        stopping_note: str | None = None,
        partial_notes: str | None = None,
    ) -> RuntimeOutcome:
        derived: list[dict] = []
        if status == 'partial':
            if state.get('search_invalid_response'):
                derived.append(gap('search_response_invalid'))
            for limited_code in ('search_quota_exhausted', 'search_rate_limited'):
                if any(event.get('name') == 'news_search' and event.get('reason') == limited_code for event in state['events']):
                    derived.append(gap(limited_code))
            if any(event.get('name') == 'market_data' and event.get('reason') == 'market_data_key_required' for event in state['events']):
                derived.append(gap('market_data_key_required'))
            if not state['read_ranges'] and any(
                event.get('name') == 'acquire_sources'
                and event.get('status') in ('unavailable', 'failed')
                and event.get('reason') not in ('acquisition_not_authorized_or_disabled', 'lead_not_discovered')
                for event in state['events']
            ):
                derived.append(gap('search_candidates_not_acquired'))
        resolved_partial_notes = partial_notes
        if status == 'partial' and not (isinstance(resolved_partial_notes, str) and resolved_partial_notes.strip()):
            resolved_partial_notes = _default_partial_notes(
                state=state,
                reason=reason,
                eligible_sources=[s for s in sources if _is_source_eligible(s, as_of)],
            )
        report_rec = insights.reconcile_run(state, source_by_id)
        derived.extend(insights.reconciliation_gap_rows(report_rec))
        unique = list({(item['code'], item['message']): item for item in [*derived, *(gaps or [])]}.values())
        chart_sources = [sid for chart in state.get('charts', []) for sid in chart.get('source_ids', [])]
        table_sources = [sid for table in state.get('tables', []) for sid in table.get('source_ids', [])]
        market_sources = [sid for sid in (state.get('analyzed_market_source_ids') or []) if isinstance(sid, str) and sid]
        acquired_ids_all = [sid for sid in (state.get('acquired_source_ids') or []) if isinstance(sid, str) and sid]
        used_source_ids = list(dict.fromkeys([
            *(x['source_id'] for x in state['read_ranges']),
            *chart_sources,
            *table_sources,
            *market_sources,
            *acquired_ids_all,
        ]))
        library_cov = None
        if library_box:
            try:
                library_cov = library_box[0].coverage()
            except Exception:
                library_cov = None
        coverage_raw = insights.run_coverage_map(state, source_by_id, library_coverage=library_cov, gaps=unique[:500])
        cache_usage_out = dict(state['cache_usage']) if isinstance(state.get('cache_usage'), Mapping) else None
        saved_claims = recover_draft_claims(state, source_by_id, lambda source: _is_source_eligible(source, as_of))
        outcome_claims = list(claims or [])
        existing_claim_ids = {claim.get('claim_id') for claim in outcome_claims}
        outcome_claims.extend(claim for claim in saved_claims if claim['claim_id'] not in existing_claim_ids)
        previous_claims = eligible_prior_claims(state, source_by_id, lambda source: _is_source_eligible(source, as_of))
        display_claims = [*outcome_claims, *previous_claims]
        requirements = assess_delivery(question, state, source_by_id, as_of=as_of, claims=display_claims)
        parts = assemble_parts(question, state, source_by_id, {s.source_id for s in sources if _is_source_eligible(s, as_of)}, requirements, display_claims)
        parts = merge_checkpoint_parts(question, state, {s.source_id for s in sources if _is_source_eligible(s, as_of)}, parts, source_by_id)
        if status == 'completed' and (incomplete_delivery(requirements) or any(p.get('core', True) and p.get('requirements') and p.get('status') != 'answered' for p in parts)):
            status = 'partial'
            report = ''
            unique.append(gap('model_reported', '核心问题的依据或可展示答案仍需核验。'))
            if not resolved_partial_notes:
                resolved_partial_notes = _default_partial_notes(state=state, reason=reason, eligible_sources=[s for s in sources if _is_source_eligible(s, as_of)])
        return RuntimeOutcome(
            status,
            reason,
            report,
            outcome_claims,
            state.get('facts', []),
            state['calculations'],
            unique[:500],
            used_source_ids,
            [],
            state['events'],
            stopping_note,
            resolved_partial_notes,
            charts=list(state.get('charts', [])),
            reconciliation=insights.reconciliation_view(report_rec),
            counter_evidence=insights.evaluate_method_and_reconciliation(state, source_by_id, claims=outcome_claims).get('counter_evidence'),
            coverage_map=insights.coverage_view(coverage_raw),
            method_packs=list(state.get('method_packs') or []),
            attributions=list(state.get('attributions') or []),
            cache_usage=cache_usage_out,
            tables=list(state.get('tables', [])),
            question_parts=parts,
        )

    if state.get('stopped'):
        return outcome('partial', state['stopped'], gaps=[gap('run_stopped')])
    if state.get('submission'):
        submission = state['submission']
        sub_status = submission.get('status', 'completed')
        return outcome(
            sub_status,
            'submitted',
            report=submission.get('report', '') if sub_status == 'completed' else '',
            claims=submission['claims'],
            gaps=submission['gaps'],
            stopping_note=submission['stopping_reason'],
            partial_notes=submission.get('partial_notes') if sub_status == 'partial' else None,
        )
    if state.get('pending') and state['pending'].get('kind') not in ('tool_batch', 'model_returned'):
        return outcome('partial', 'external_result_unknown', gaps=[gap('external_result_unknown')])

    if not callable(getattr(model, 'complete_turn', None)) and callable(getattr(model, 'complete', None)):
        model = ChatResearchAdapter(model)
    presend_retries, presend_base = presend_retry_settings()
    submission_retries = 0
    seen: set[str] = set(state.get('seen', []))
    stream_call_seq = sum(
        1 for ev in state.get('events', [])
        if isinstance(ev, Mapping) and ev.get('kind') in ('model', 'model_failure')
    )
    active_call_seq: int | None = None

    def _emit_reset(target_seq: int | None, reason: str) -> None:
        if on_stream_event is None or target_seq is None:
            return
        on_stream_event({'type': 'reset', 'call_seq': target_seq, 'reason': reason})

    deps = ToolDependencies(
        calculate=calculate,
        calculation_input_status=calculation_input_status,
        build_research_chart=build_research_chart,
        build_research_table=build_research_table,
        analyze_market_series=analyze_market_series,
        extract_statements=extract_statements,
        run_search_library=run_search_library,
        sync_library=sync_library,
        run_news_search=run_news_search,
        logger=_logger,
    )
    tool_ctx = ToolExecutionContext(
        question=question,
        state=state,
        sources=sources,
        source_by_id=source_by_id,
        context=context,
        as_of=as_of,
        seen=seen,
        discover=discover,
        acquire=acquire,
        fin_data=fin_data,
        ocr_pages=ocr_pages,
        news_providers=news_provider_list,
        issuer_docs=issuer_docs,
        library_box=library_box,
        save=save,
        outcome_builder=outcome,
        can_afford_presubmit_followup=lambda: _can_afford_presubmit_followup(context),
        emit_reset=_emit_reset,
        deps=deps,
    )

    while True:
        context.ensure_running()
        pending_kind = (state.get('pending') or {}).get('kind')
        calls: list[Any] = []
        cursor = 0
        call_id = ''
        if pending_kind == 'model_returned':
            pass
        elif pending_kind == 'tool_batch':
            pending = state['pending']
            call_id = pending['call_id']
            calls = [SimpleNamespace(**item) for item in pending['calls']]
            cursor = pending['cursor']
            if 0 <= cursor < len(calls) and not pending.get('started_at'):
                state['pending'] = build_tool_batch_pending(
                    state,
                    call_id,
                    pending['calls'],
                    cursor,
                    source_by_id=source_by_id,
                    as_of=as_of,
                )
        else:
            step_status, step_outcome, submission_retries, stream_call_seq, active_call_seq = _invoke_model_turn(
                state=state,
                source_by_id=source_by_id,
                model=model,
                context=context,
                library_box=library_box,
                fin_data=fin_data,
                on_submission=on_submission,
                on_stream_event=on_stream_event,
                emit_reset=_emit_reset,
                save=save,
                outcome_fn=outcome,
                presend_retries=presend_retries,
                presend_base=presend_base,
                submission_retries=submission_retries,
                stream_call_seq=stream_call_seq,
                active_call_seq=active_call_seq,
            )
            if step_status == 'retry':
                continue
            if step_status == 'outcome':
                assert step_outcome is not None
                return step_outcome

        if (state.get('pending') or {}).get('kind') == 'model_returned':
            returned_outcome, call_id, calls, cursor, active_call_seq = _process_model_returned(
                state=state,
                source_by_id=source_by_id,
                as_of=as_of,
                context=context,
                emit_reset=_emit_reset,
                save=save,
                outcome_fn=outcome,
                active_call_seq=active_call_seq,
            )
            if returned_outcome is not None:
                return returned_outcome

        tool_ctx.batch_call_id = call_id
        tool_ctx.batch_call_seq = active_call_seq
        tool_ctx.active_call_seq = active_call_seq
        early_outcome, active_call_seq = execute_tool_batch(tool_ctx, calls, cursor=cursor)
        if early_outcome is not None:
            return early_outcome
