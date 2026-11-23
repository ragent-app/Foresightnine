"""Phase P2 Track A tests: declarative tool registry, context manager, and tool executor."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace
from typing import get_args, get_origin

from pydantic import BaseModel
import pytest

from app.model_protocol import ModelToolCall, ModelUsage, canonical_json_dumps, canonicalize_json_value
from app.research_budget import ResearchExecutionBudget
from app.research_charts import ResearchChart
from app.research_context_manager import (
    DEFAULT_L1_COMPACT_TRIGGER_CHARS,
    L1_LOW_WATER_RATIO,
    MIN_COMPACTION_SAVINGS_CHARS,
    _coerce_model_usage,
    _compaction_salience_tier,
    _default_cache_usage,
    _l1_compaction_trigger_chars,
    _record_cache_usage,
    build_model_turn_input,
    compact_runtime_history,
)
from app.research_coordinator import ResearchExecutionContext
from app.research_item_validator import ItemPersistenceValidationError
import app.research_runtime as research_runtime
from app.research_sources import ResearchSource, SourcePermissions
from app.research_tool_executor import (
    QuotationLocateError,
    TOOL_HANDLERS,
    ToolDependencies,
    ToolExecutionContext,
    build_tool_batch_pending,
    build_turn_baseline,
    compute_action_signature,
    execute_tool_batch,
    locate_quotation,
    validate_persisted_tool_batch,
    validate_persisted_tool_item,
)
from app.research_tools_registry import (
    ALL_TOOL_DEFINITIONS,
    ACQUISITION_TOOLS,
    BULK_COMPACT_TOOLS,
    COVERAGE_UPDATING_TOOLS,
    FIN_TOOLS,
    PERSISTED_ITEM_TOOLS,
    PROTECTED_COMPACT_TOOLS,
    TOOL_INPUT_MODELS,
    TOOL_REGISTRY,
    TOOL_SPECS,
    TOOLS,
    ToolActivityContext,
    ToolSpec,
    get_tool_spec,
    summarize_tool_activity,
)
from app.research_topics import (
    ResearchCalculation,
    ResearchClaim,
    ResearchContent,
    ResearchFact,
    ResearchGap,
    ResearchTurn,
)


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _source(
    source_id: str = 's1',
    text: str = 'Revenue was 100 USD in FY2024. Revenue was 120 USD in FY2025.',
    *,
    title: str = 'FY2025 Annual Report',
    published_at: datetime | None = NOW - timedelta(days=5),
    model_input: bool = True,
) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id='owner-1',
        title=title,
        text=text,
        kind='user_supplied',
        published_at=published_at,
        obtained_at=NOW,
        content_sha256=f'sha-{source_id}',
        permissions=SourcePermissions(store=True, model_input=model_input, display=True, export=True),
    )


def _exec_context() -> ResearchExecutionContext:
    return ResearchExecutionContext(
        ResearchExecutionBudget(None, None, None, float('inf')),
        False,
        lambda: None,
        lambda: None,
        lambda *_: None,
        lambda *_: None,
        lambda: None,
    )


# --------------------------------------------------------------------------- 1. Registry & ResearchContent self-check

def test_registry_persistence_models_match_research_content_fields():
    """Every persisted-item ResearchContent model must be declared on its producing ToolSpec."""
    content_list_models: dict[str, type[BaseModel]] = {}
    for field_name, field_info in ResearchContent.model_fields.items():
        if get_origin(field_info.annotation) is not list:
            continue
        args = get_args(field_info.annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            content_list_models[field_name] = args[0]

    # Run/turn metadata models (gaps, turns) are produced by runtime/turn assembly, not individual tools.
    # Tables are constructed by the typed table builder before they reach the state.
    from app.research_tables import ResearchTable
    assert content_list_models.pop('tables') is ResearchTable
    assert set(content_list_models) == {'facts', 'calculations', 'claims', 'charts', 'gaps', 'turns'}
    assert content_list_models['gaps'] is ResearchGap
    assert content_list_models['turns'] is ResearchTurn

    tool_item_fields = {
        'facts': ResearchFact,
        'calculations': ResearchCalculation,
        'charts': ResearchChart,
        'claims': ResearchClaim,
    }
    for field_name, expected_cls in tool_item_fields.items():
        assert content_list_models[field_name] is expected_cls

    expected_tool_mapping = {
        'record_facts': ResearchFact,
        'calculate_financials': ResearchCalculation,
        'create_chart': ResearchChart,
        'submit_research': ResearchClaim,
    }
    assert PERSISTED_ITEM_TOOLS == expected_tool_mapping
    assert set(PERSISTED_ITEM_TOOLS.values()) == set(tool_item_fields.values())

    for tool_name, expected_model in expected_tool_mapping.items():
        spec = get_tool_spec(tool_name)
        assert spec is not None
        assert spec.persistence_model is expected_model
        assert spec.persistence_item_kind is not None

    for spec in TOOL_SPECS:
        if spec.name not in expected_tool_mapping:
            assert spec.persistence_model is None
            assert spec.persistence_item_kind is None


def test_registry_specs_handlers_and_definitions_are_consistent():
    assert len(TOOL_SPECS) == len(TOOL_REGISTRY) == len(ALL_TOOL_DEFINITIONS) == len(TOOL_INPUT_MODELS)
    assert set(TOOL_HANDLERS) == set(TOOL_REGISTRY)
    assert [s.name for s in TOOL_SPECS] == [t.name for t in ALL_TOOL_DEFINITIONS]
    assert TOOLS[-1].name == 'submit_research'

    assert BULK_COMPACT_TOOLS.isdisjoint(PROTECTED_COMPACT_TOOLS)
    assert FIN_TOOLS <= BULK_COMPACT_TOOLS
    assert ACQUISITION_TOOLS <= BULK_COMPACT_TOOLS
    assert set(PERSISTED_ITEM_TOOLS) <= PROTECTED_COMPACT_TOOLS
    assert 'outline_source' not in COVERAGE_UPDATING_TOOLS
    assert 'search_source' not in COVERAGE_UPDATING_TOOLS
    assert set(TOOL_REGISTRY) - COVERAGE_UPDATING_TOOLS == {'outline_source', 'search_source', 'activate_tool_group'}

    for spec, definition in zip(TOOL_SPECS, ALL_TOOL_DEFINITIONS, strict=True):
        assert issubclass(spec.input_model, BaseModel)
        assert spec.description.strip()
        assert callable(spec.activity_summarizer)
        assert definition.input_schema == canonicalize_json_value(spec.input_model.model_json_schema())
        assert canonical_json_dumps(definition.input_schema)

    with pytest.raises(ValueError, match='together'):
        ToolSpec(
            name='broken_half',
            description='broken',
            input_model=TOOL_REGISTRY['outline_source'].input_model,
            persistence_model=ResearchFact,
        )
    with pytest.raises(ValueError, match='does not match'):
        ToolSpec(
            name='broken_mismatch',
            description='broken',
            input_model=TOOL_REGISTRY['outline_source'].input_model,
            persistence_model=ResearchFact,
            persistence_item_kind='calculation',
        )


def test_runtime_reexports_preserve_symbol_identity():
    assert research_runtime.TOOLS is TOOLS
    assert research_runtime._BULK_COMPACT_TOOLS is BULK_COMPACT_TOOLS
    assert research_runtime._PROTECTED_COMPACT_TOOLS is PROTECTED_COMPACT_TOOLS
    assert research_runtime._COVERAGE_UPDATING_TOOLS is COVERAGE_UPDATING_TOOLS
    assert research_runtime._FIN_TOOLS is FIN_TOOLS
    assert research_runtime._ACQUISITION_TOOLS is ACQUISITION_TOOLS
    assert research_runtime.compact_runtime_history is compact_runtime_history
    assert research_runtime._compaction_salience_tier is _compaction_salience_tier
    assert research_runtime._default_cache_usage is _default_cache_usage
    assert research_runtime._coerce_model_usage is _coerce_model_usage
    assert research_runtime._record_cache_usage is _record_cache_usage
    assert research_runtime._l1_compaction_trigger_chars is _l1_compaction_trigger_chars
    assert research_runtime.summarize_tool_activity is summarize_tool_activity


# --------------------------------------------------------------------------- 2. activity_summarizer dispatch

def test_activity_summarizer_dispatch_across_tools_and_eligibility():
    eligible_src = _source('s-ok', 'x' * 500, title='10-K Report', published_at=NOW - timedelta(days=2))
    future_src = _source('s-fut', 'Future text', title='Future Report', published_at=NOW + timedelta(days=2))
    denied_src = _source('s-no', 'Denied text', title='Secret Report', model_input=False)
    by_id = {'s-ok': eligible_src, 's-fut': future_src, 's-no': denied_src}

    assert summarize_tool_activity('unknown_tool', {'rationale': 'x'}) == (None, None)
    assert summarize_tool_activity('read_sources', 'not-a-mapping') == (None, None)
    assert summarize_tool_activity('submit_research', 'not-a-mapping') == (None, '正在核验引文位置与研究结论')

    # Direct single-arg call to ToolSpec.activity_summarizer works without context.
    spec_news = TOOL_REGISTRY['news_search']
    assert spec_news.activity_summarizer({'query': 'NVDA earnings', 'rationale': ' check news '}) == (
        'check news',
        'NVDA earnings',
    )

    assert summarize_tool_activity('search_library', {'query': 'gross margin', 'rationale': 'find'}) == (
        'find',
        '资料库 · gross margin',
    )
    assert summarize_tool_activity(
        'acquire_sources',
        {
            'urls': [
                'https://sec.gov/a',
                'https://apple.com/b',
                'https://reuters.com/c',
                'https://sec.gov/d',
                'https://wsj.com/e',
            ],
            'rationale': 'fetch leads',
        },
    ) == ('fetch leads', 'sec.gov、apple.com、reuters.com 等 4 个')

    # Eligible vs cutoff/ineligible title masking.
    assert summarize_tool_activity('outline_source', {'source_id': 's-ok', 'rationale': 'scan'}, source_by_id=by_id, as_of=NOW) == (
        'scan',
        '《10-K Report》',
    )
    assert summarize_tool_activity('outline_source', {'source_id': 's-fut'}, source_by_id=by_id, as_of=NOW) == (
        None,
        's-fut',
    )
    assert summarize_tool_activity('search_source', {'source_id': 's-no', 'query': 'rev'}, source_by_id=by_id, as_of=NOW) == (
        None,
        's-no · rev',
    )
    assert summarize_tool_activity(
        'read_sources',
        {'reads': [{'source_id': 's-ok', 'start': 10, 'end': 120}], 'rationale': 'quote'},
        source_by_id=by_id,
        as_of=NOW,
    ) == ('quote', '《10-K Report》10–120 字')
    assert summarize_tool_activity(
        'read_pages',
        {'source_id': 's-ok', 'first_page': 2, 'last_page': 5, 'rationale': 'pages'},
        source_by_id=by_id,
        as_of=NOW,
    ) == ('pages', '《10-K Report》 p.2–5')
    assert summarize_tool_activity(
        'request_ocr',
        {'source_id': 's-ok', 'pages': list(range(1, 11)), 'rationale': 'ocr'},
        source_by_id=by_id,
        as_of=NOW,
    ) == ('ocr', '《10-K Report》 OCR p.1,2,3,4,5,6,7,8…')
    assert summarize_tool_activity(
        'extract_statements',
        {'source_id': 's-ok', 'rationale': 'parse'},
        source_by_id=by_id,
        as_of=NOW,
    ) == ('parse', '《10-K Report》 · 财务报表')

    assert summarize_tool_activity(
        'calculate_financials',
        {
            'calculations': [
                {'formula': 'cagr'},
                {'formula': 'ttm_sum'},
                {'formula': 'weighted_average'},
                {'formula': 'ratio'},
            ],
            'rationale': 'metrics',
        },
    ) == ('metrics', '复合年均增长率、TTM 滚动合计、加权平均 等（4 项）')
    assert summarize_tool_activity(
        'record_facts',
        {'facts': [{'label': 'Revenue'}, {'label': 'Margin'}], 'rationale': 'log'},
    ) == ('log', 'Revenue、Margin（2 项）')
    assert summarize_tool_activity(
        'create_chart',
        {'chart_type': 'bar', 'title': 'Revenue Trend', 'rationale': 'viz'},
    ) == ('viz', 'bar · Revenue Trend')
    assert summarize_tool_activity(
        'create_table',
        {'table_type': 'comparison', 'title': 'Peer Comps', 'rationale': 'tbl'},
    ) == ('tbl', 'comparison · Peer Comps')
    assert summarize_tool_activity(
        'analyze_market_series',
        {'source_id': 'src-aapl', 'benchmark_source_id': 'src-spy'},
    ) == (None, 'src-aapl vs src-spy')
    assert summarize_tool_activity(
        'activate_method_pack',
        {'pack_id': 'earnings_review', 'rationale': 'pack'},
    ) == ('pack', 'earnings_review')

    # Direct 2-arg ToolActivityContext call works cleanly.
    act_ctx = ToolActivityContext(source_by_id=by_id, as_of=NOW)
    assert TOOL_REGISTRY['outline_source'].activity_summarizer({'source_id': 's-ok'}, act_ctx) == (None, '《10-K Report》')


# --------------------------------------------------------------------------- 3. research_context_manager

def test_context_manager_cache_accounting_and_idempotency():
    base = _default_cache_usage()
    assert base['input_tokens'] == 0 and base['cache_hit_ratio'] is None

    assert _coerce_model_usage(None) is None
    assert _coerce_model_usage('bad') is None
    coerced = _coerce_model_usage({'input_tokens': 40, 'output_tokens': 10, 'cache_read_input_tokens': 60})
    assert isinstance(coerced, ModelUsage) and coerced.cache_read_input_tokens == 60

    state: dict = {}
    assert _record_cache_usage(state, call_id='c0', usage_detail=None) is None

    s1 = _record_cache_usage(
        state,
        call_id='c1',
        usage_detail=ModelUsage(100, 20, 120, cache_creation_input_tokens=80, cache_read_input_tokens=0),
    )
    assert s1 is not None and s1['total_input_tokens'] == 100

    # Duplicate call_id is idempotent and does not double-count.
    dup = _record_cache_usage(
        state,
        call_id='c1',
        usage_detail=ModelUsage(100, 20, 120, cache_creation_input_tokens=80, cache_read_input_tokens=0),
    )
    assert dup == s1
    assert state['cache_usage']['calls_with_usage'] == 1

    # Second distinct call via checkpoint-serialized dict format.
    _record_cache_usage(
        state,
        call_id='c2',
        usage_detail={'input_tokens': 15, 'output_tokens': 25, 'total_tokens': 40, 'cache_read_input_tokens': 180, 'cache_creation_input_tokens': 0},
    )
    assert state['cache_usage'] == {
        'input_tokens': 295,
        'output_tokens': 45,
        'cache_read_input_tokens': 180,
        'cache_creation_input_tokens': 80,
        'cache_hit_ratio': round(180 / 295, 6),
        'calls_with_usage': 2,
        'calls_with_cache_telemetry': 2,
    }


def test_context_manager_salience_compaction_trigger_and_turn_input():
    # Salience tier assignment.
    assert _compaction_salience_tier(
        'news_search', {'leads': []}, False, [], facts_count=0, fact_source_ids=set(), acquired_urls=set(),
    ) == 2
    assert _compaction_salience_tier(
        'news_search',
        {'leads': [{'url': 'https://example.com/a'}]},
        False,
        [],
        facts_count=0,
        fact_source_ids=set(),
        acquired_urls={'https://example.com/a'},
    ) == 1
    assert _compaction_salience_tier(
        'news_search',
        {'leads': [{'url': 'https://example.com/new'}]},
        False,
        [],
        facts_count=0,
        fact_source_ids=set(),
        acquired_urls={'https://example.com/a'},
    ) == 3
    assert _compaction_salience_tier(
        'read_sources',
        {'results': [{'source_id': 's1', 'status': 'read', 'text': 'X' * 200}]},
        False,
        ['s1'],
        facts_count=1,
        fact_source_ids={'s1'},
        acquired_urls=set(),
    ) == 1
    assert _compaction_salience_tier(
        'read_sources',
        {'results': [{'source_id': 's2', 'status': 'read', 'text': 'X' * 200}]},
        False,
        ['s2'],
        facts_count=1,
        fact_source_ids={'s1'},
        acquired_urls=set(),
    ) == 3

    # Trigger calculation tightens with model policy / capacity / remaining budget.
    tight_model = SimpleNamespace(
        policy=SimpleNamespace(max_input_tokens=30_000),
        research_capacity=lambda: SimpleNamespace(work_context_tokens=15_000, context_tokens=20_000, input_counter='chars_div_3'),
    )
    tight_ctx = SimpleNamespace(budget=SimpleNamespace(max_total_tokens=40_000), accounted_tokens=35_000)
    trigger = _l1_compaction_trigger_chars({'user': 'Q'}, tight_model, tight_ctx, DEFAULT_L1_COMPACT_TRIGGER_CHARS)
    assert 1_000 <= trigger < DEFAULT_L1_COMPACT_TRIGGER_CHARS

    # Compaction no-op below MIN_COMPACTION_SAVINGS_CHARS unless hard limit exceeded.
    small_state = {
        'history': [
            {
                'assistant': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r1', 'name': 'read_sources', 'input': {}}]},
                'results': [{'tool_call_id': 'r1', 'content': json.dumps({'results': [{'source_id': 's1', 'start': 0, 'end': 2500, 'status': 'read', 'text': 'A' * 2500}]}), 'is_error': False}],
            },
            {
                'assistant': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r2', 'name': 'read_sources', 'input': {}}]},
                'results': [{'tool_call_id': 'r2', 'content': json.dumps({'results': [{'source_id': 's1', 'start': 2500, 'end': 3500, 'status': 'read', 'text': 'B' * 1000}]}), 'is_error': False}],
            },
        ],
        'facts': [{'fact_id': 'f1', 'source_id': 's1'}],
        'events': [],
    }
    res_min = compact_runtime_history(small_state, keep_recent=1, trigger_chars=3_000, force=False)
    assert res_min['compacted'] is False and res_min['reason'] == 'below_min_savings'
    assert res_min['low_water_chars'] == int(3_000 * L1_LOW_WATER_RATIO)
    assert res_min['achievable_saving_chars'] < MIN_COMPACTION_SAVINGS_CHARS

    # Exceeding hard_limit_chars forces L1 compaction even below MIN_COMPACTION_SAVINGS_CHARS.
    res_hard = compact_runtime_history(small_state, keep_recent=1, trigger_chars=3_000, force=False, hard_limit_chars=3_200)
    assert res_hard['compacted'] is True
    assert res_hard['cleared_count'] == 1
    assert small_state['compaction_count'] == 1

    # build_model_turn_input preserves user string and emits transient tail_note when zero_yield_turns >= 3.
    src = _source()
    turn_in = build_model_turn_input(
        state={
            'user': json.dumps({'question': 'Q'}),
            'history': [],
            'events': [],
            'read_ranges': [],
            'facts': [],
            'calculations': [],
            'zero_yield_turns': 3,
        },
        source_by_id={'s1': src},
    )
    assert turn_in.user == json.dumps({'question': 'Q'})
    assert isinstance(turn_in.tail_note, str) and '[Convergence]' in turn_in.tail_note


# --------------------------------------------------------------------------- 4. research_tool_executor helpers

def test_tool_executor_persistence_validation_and_helpers():
    secret = 'SECRET_EXECUTOR_LEAK_GUARD'
    fact_row = {
        'fact_id': 'f1',
        'label': 'Revenue',
        'value': '100',
        'unit': 'USD',
        'currency': 'USD',
        'period': 'FY2024',
        'source_id': 's1',
        'quotation': 'Revenue was 100 USD.',
        'character_start': 0,
        'character_end': 20,
        'location': {'character_start': 0, 'character_end': 20},
        'verification': 'numeric_and_location_match_semantics_not_verified',
    }
    validated_fact = validate_persisted_tool_item('record_facts', fact_row)
    assert isinstance(validated_fact, ResearchFact)

    batch_facts = validate_persisted_tool_batch('record_facts', [fact_row])
    assert len(batch_facts) == 1 and isinstance(batch_facts[0], ResearchFact)

    with pytest.raises(ItemPersistenceValidationError) as exc_info:
        validate_persisted_tool_item('calculate_financials', {'calculation_id': 'c1', 'formula_id': secret}, item_index=2)
    assert exc_info.value.item_kind == 'calculation'
    assert exc_info.value.item_index == 2
    assert secret not in exc_info.value.sanitized_message
    assert secret not in json.dumps(exc_info.value.validation_errors)

    with pytest.raises(ValueError, match='does not declare a persistence_model'):
        validate_persisted_tool_item('outline_source', {'source_id': 's1'})

    # build_tool_batch_pending uses registry activity summarizer.
    src = _source('s1', 'Revenue was 100 USD in FY2024.')
    pending = build_tool_batch_pending(
        {'pending': {'baseline': {'facts': 0}}},
        'batch-1',
        [{'id': 't1', 'name': 'outline_source', 'arguments': {'source_id': 's1', 'rationale': 'scan'}}],
        0,
        source_by_id={'s1': src},
        as_of=NOW,
    )
    assert pending['kind'] == 'tool_batch'
    assert pending['rationale'] == 'scan'
    assert pending['target'] == '《FY2025 Annual Report》'
    assert pending['baseline'] == {'facts': 0}

    # locate_quotation finds exact span and rejects missing/outside/ambiguous spans.
    span = locate_quotation(src, 'Revenue was 100 USD', None, None, ranges=[{'source_id': 's1', 'start': 0, 'end': len(src.text)}])
    assert span == (0, 19)
    with pytest.raises(QuotationLocateError) as outside_exc:
        locate_quotation(src, 'FY2024', None, None, ranges=[{'source_id': 's1', 'start': 0, 'end': 10}])
    assert outside_exc.value.kind == 'not_in_read_ranges'

    # compute_action_signature ignores rationale while reacting to evidence changes.
    s_base = {'read_ranges': [], 'facts': [], 'charts': [], 'tables': [], 'leads': [], 'acquired_source_ids': []}
    sig_a = compute_action_signature('outline_source', {'source_id': 's1'}, s_base)
    sig_b = compute_action_signature('outline_source', {'source_id': 's1'}, {**s_base, 'read_ranges': [{'source_id': 's1', 'start': 0, 'end': 5}]})
    assert sig_a != sig_b


def test_execute_tool_batch_standalone_validates_persistence_and_records_events(caplog):
    caplog.set_level(logging.WARNING, logger='app.research_runtime')
    text = 'FY2024 revenue was 100 USD. FY2025 revenue was 120 USD.'
    src = _source('s1', text)
    state: dict = {
        'user': json.dumps({'question': 'Growth?'}),
        'history': [{'assistant': {'role': 'assistant', 'content': []}, 'results': []}],
        'events': [],
        'read_ranges': [],
        'facts': [],
        'calculations': [],
        'charts': [],
        'tables': [],
        'leads': [],
        'acquired_source_ids': [],
        'pending': None,
    }
    baseline = build_turn_baseline(state, {'s1': src})
    state['pending'] = {'kind': 'tool_batch', 'call_id': 'call-1', 'cursor': 0, 'calls': [], 'baseline': baseline}

    def poisoned_calc(req):
        return {'formula_id': 'BAD_FORMULA', 'extra_field': 'SECRET_SHOULD_NOT_LEAK'}

    checkpoints: list[dict] = []
    tool_ctx = ToolExecutionContext(
        question='Growth?',
        state=state,
        sources=[src],
        source_by_id={'s1': src},
        context=_exec_context(),
        as_of=NOW,
        seen=set(),
        save=lambda: checkpoints.append(json.loads(json.dumps(state))),
        outcome_builder=lambda status, reason, **kw: SimpleNamespace(status=status, reason=reason, **kw),
        deps=ToolDependencies(calculate=poisoned_calc),
    )

    # First batch: read_sources + record_facts succeed.
    calls_1 = [
        ModelToolCall('c1', 'read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}),
        ModelToolCall(
            'c2',
            'record_facts',
            {
                'facts': [
                    {
                        'label': 'Revenue',
                        'value': 100,
                        'unit': 'USD',
                        'currency': 'USD',
                        'period': 'FY2024',
                        'source_id': 's1',
                        'quotation': 'FY2024 revenue was 100 USD.',
                    },
                    {
                        'label': 'Revenue',
                        'value': 120,
                        'unit': 'USD',
                        'currency': 'USD',
                        'period': 'FY2025',
                        'source_id': 's1',
                        'quotation': 'FY2025 revenue was 120 USD.',
                    },
                ],
                'rationale': 'record',
            },
        ),
    ]
    early, _ = execute_tool_batch(tool_ctx, calls_1)
    assert early is None
    assert len(state['facts']) == 2
    assert state['zero_yield_turns'] == 0
    assert [ev['name'] for ev in state['events']] == ['read_sources', 'record_facts']

    # Second batch: poisoned calculation is rejected atomically at persistence boundary.
    state['history'].append({'assistant': {'role': 'assistant', 'content': []}, 'results': []})
    state['pending'] = {'kind': 'tool_batch', 'call_id': 'call-2', 'cursor': 0, 'calls': [], 'baseline': build_turn_baseline(state, {'s1': src})}
    f1, f2 = state['facts'][0]['fact_id'], state['facts'][1]['fact_id']
    calls_2 = [
        ModelToolCall(
            'c3',
            'calculate_financials',
            {'calculations': [{'formula': 'growth_percent', 'first_fact_id': f2, 'second_fact_id': f1}], 'rationale': 'calc'},
        ),
    ]
    early2, _ = execute_tool_batch(tool_ctx, calls_2)
    assert early2 is None
    assert state['calculations'] == []
    feedback = json.loads(state['history'][-1]['results'][0]['content'])
    assert feedback['status'] == 'invalid'
    assert feedback['error'] == 'persistence_validation_failed'
    assert feedback['item_kind'] == 'calculation'
    assert 'SECRET_SHOULD_NOT_LEAK' not in json.dumps(feedback)
    assert 'research_tool_item_validation_failed' in caplog.text
