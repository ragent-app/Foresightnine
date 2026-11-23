"""Tests for degraded diagnostics and observability in research_run_insights."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

import app.research_run_insights as insights
from app.model_protocol import ModelResponse
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from tests.test_research_runtime import Model, context


def _source(source_id: str = 's1') -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id='owner',
        title='Disclosure',
        text='Revenue was 100 USD.',
        kind='user_supplied',
        obtained_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        content_sha256='hash-' + source_id,
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )


def test_reconcile_run_empty_still_returns_none() -> None:
    assert insights.reconcile_run({'facts': [], 'prior_facts': []}, {'s1': _source()}) is None
    assert insights.reconciliation_view(None) is None
    assert insights.coverage_view(None) is None


def test_reconcile_run_surfaces_content_free_degraded_diagnostic(monkeypatch, caplog) -> None:
    secret = 'SECRET_SOURCE_SNIPPET_DO_NOT_LEAK'

    def boom(*_args, **_kwargs):
        raise ValueError(f'bad value: {secret}')

    monkeypatch.setattr(insights, 'reconcile', boom)
    state = {'facts': [{'fact_id': 'f1', 'source_id': 's1', 'label': 'Revenue', 'value': '100', 'unit': 'USD'}]}
    with caplog.at_level(logging.WARNING, logger='app.research_run_insights'):
        caplog.clear()
        report = insights.reconcile_run(state, {'s1': _source()})

    assert report is not None
    assert report['status'] == 'degraded'
    diag = report['diagnostic']
    assert diag['operation'] == 'reconcile_run'
    assert diag['exception_class'] == 'ValueError'
    assert diag['location']['file'] == 'research_run_insights.py'
    assert secret not in json.dumps(report)
    assert len(caplog.records) == 1
    assert 'research_insight_degraded' in caplog.records[0].message
    assert secret not in caplog.records[0].message

    # Downstream consumers handle degraded reconciliation safely without crashing or re-throwing.
    assert insights.reconciliation_for_model(report) is None
    assert insights.reconciliation_gap_rows(report) == []
    view = insights.reconciliation_view(report)
    assert view == {'version': 1, 'status': 'degraded', 'diagnostic': diag}


def test_reconcile_run_handles_malformed_state_and_broken_source_objects(caplog) -> None:
    class BrokenSource:
        source_id = 's1'

        @property
        def kind(self):
            raise RuntimeError('SECRET_SOURCE_BROKEN')

    with caplog.at_level(logging.WARNING, logger='app.research_run_insights'):
        caplog.clear()
        bad_state = insights.reconcile_run(None, {})  # type: ignore[arg-type]
        bad_source = insights.reconcile_run(
            {'facts': [{'fact_id': 'f1', 'source_id': 's1'}]},
            {'s1': BrokenSource()},
        )

    assert bad_state['status'] == 'degraded'
    assert bad_state['diagnostic']['operation'] == 'reconcile_run'
    assert bad_state['diagnostic']['exception_class'] == 'AttributeError'
    assert bad_source['status'] == 'degraded'
    assert bad_source['diagnostic']['exception_class'] == 'RuntimeError'
    assert 'SECRET_SOURCE_BROKEN' not in json.dumps(bad_source)
    assert all('SECRET_SOURCE_BROKEN' not in rec.message for rec in caplog.records)


def test_coverage_map_and_coverage_view_surface_degraded_diagnostics(monkeypatch, caplog) -> None:
    secret = 'SECRET_COVERAGE_TOKEN'

    def boom_map(*_args, **_kwargs):
        raise RuntimeError(f'coverage broke: {secret}')

    monkeypatch.setattr(insights, 'build_coverage_map', boom_map)
    with caplog.at_level(logging.WARNING, logger='app.research_run_insights'):
        caplog.clear()
        cov = insights.run_coverage_map({'events': []}, {'s1': _source()})

    assert cov['status'] == 'degraded'
    assert cov['diagnostic']['operation'] == 'run_coverage_map'
    assert cov['diagnostic']['exception_class'] == 'RuntimeError'
    assert secret not in json.dumps(cov)
    assert len(caplog.records) == 1
    assert secret not in caplog.records[0].message

    # coverage_view preserves the upstream run_coverage_map diagnostic.
    view = insights.coverage_view(cov)
    assert view == {'version': 1, 'status': 'degraded', 'diagnostic': cov['diagnostic']}

    # coverage_view also surfaces its own diagnostic if coverage_ui_view fails.
    monkeypatch.setattr(insights, 'coverage_ui_view', lambda _c: (_ for _ in ()).throw(KeyError(f'SECRET_KEY:{secret}')))
    with caplog.at_level(logging.WARNING, logger='app.research_run_insights'):
        caplog.clear()
        view_err = insights.coverage_view({'categories': []})
    assert view_err['version'] == 1
    assert view_err['status'] == 'degraded'
    assert view_err['diagnostic']['operation'] == 'coverage_view'
    assert view_err['diagnostic']['exception_class'] == 'KeyError'
    assert secret not in json.dumps(view_err)
    assert secret not in caplog.records[0].message


def test_secondary_insight_helpers_degrade_safely(monkeypatch, caplog) -> None:
    secret = 'SECRET_SECONDARY_DETAIL'
    monkeypatch.setattr(insights, 'reconciliation_model_summary', lambda _r: (_ for _ in ()).throw(ValueError(secret)))
    monkeypatch.setattr(insights, 'reconciliation_ui_view', lambda _r: (_ for _ in ()).throw(ValueError(secret)))
    monkeypatch.setattr(insights, 'reconciliation_gaps', lambda _r, limit=5: (_ for _ in ()).throw(ValueError(secret)))
    monkeypatch.setattr(insights, 'select_packs', lambda _q: (_ for _ in ()).throw(ValueError(secret)))

    report = {'rows': [{'status': 'differs'}]}
    with caplog.at_level(logging.WARNING, logger='app.research_run_insights'):
        caplog.clear()
        assert insights.reconciliation_for_model(report) is None
        assert insights.reconciliation_gap_rows(report) == []
        rec_view = insights.reconciliation_view(report)
        assert insights.method_pack_texts('earnings revenue', {}) == ([], [])

    assert rec_view['status'] == 'degraded'
    assert rec_view['diagnostic']['operation'] == 'reconciliation_view'
    assert len(caplog.records) == 4
    assert all(secret not in rec.message for rec in caplog.records)
    assert secret not in json.dumps(rec_view)


def test_broken_logger_does_not_crash_degraded_diagnostic(monkeypatch, caplog, capfd) -> None:
    def broken_warning(*_args, **_kwargs):
        raise OSError('SECRET_LOG_DISK_FULL')

    monkeypatch.setattr(insights._logger, 'warning', broken_warning)
    monkeypatch.setattr(insights, 'build_coverage_map', lambda *_a, **_k: (_ for _ in ()).throw(ValueError('SECRET_ERR')))
    with caplog.at_level(logging.ERROR, logger='app.research_diagnostics'):
        caplog.clear()
        cov = insights.run_coverage_map({'events': []}, {})
    assert cov['status'] == 'degraded'
    assert cov['diagnostic']['exception_class'] == 'ValueError'
    assert any('diagnostic_write_failed' in rec.message for rec in caplog.records)
    assert all('SECRET_' not in rec.message for rec in caplog.records)

    diag_logger = logging.getLogger('app.research_diagnostics')
    monkeypatch.setattr(diag_logger, 'error', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('SECRET_HANDLER_DOWN')))
    cov2 = insights.run_coverage_map({'events': []}, {})
    captured = capfd.readouterr()
    assert cov2['status'] == 'degraded'
    assert 'diagnostic_write_failed logging_unavailable' in captured.err
    assert 'SECRET_' not in captured.err


def test_research_runtime_never_crashes_when_insights_fail(monkeypatch) -> None:
    monkeypatch.setattr(insights, 'build_coverage_map', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(insights, 'reconcile', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('boom')))
    stop = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
    out = run_research_agent(question='Q', sources=[_source()], model=Model([stop]), context=context())
    assert out.status == 'partial'
    assert out.coverage_map is not None
    assert out.coverage_map['status'] == 'degraded'
    assert out.coverage_map['diagnostic']['operation'] == 'run_coverage_map'


def test_counter_searches_separate_external_document_and_linked_findings():
    from app.research_run_insights import evaluate_method_and_reconciliation
    state = {'counter_searches': [
        *[{'call_id': f'ext-{i}', 'scope': 'external', 'target': 'IPO demand', 'hypothesis': 'Demand may be weaker', 'query': 'weak demand', 'status': 'completed'} for i in range(2)],
        *[{'call_id': f'doc-{i}', 'scope': 'document', 'target': 'IPO demand', 'hypothesis': 'Demand may be weaker', 'query': 'risk', 'status': 'completed', 'source_id': 'filing'} for i in range(6)],
        {'call_id': 'ordinary', 'scope': 'external'},
    ]}
    result = evaluate_method_and_reconciliation(state, {}, claims=[])
    counter = result['counter_evidence']
    assert counter['external_attempts'] == 2
    assert counter['document_attempts'] == 6
    assert counter['completed_actions'] == 8
    assert counter['linked_findings'] == []
    assert len(counter['attempts']) == 8


def test_only_explicit_search_intent_is_recorded_and_resume_is_idempotent():
    from types import SimpleNamespace
    from app.research_tool_executor import _record_counter_search
    from app.research_tools_registry import SearchSourceRequest
    ctx = SimpleNamespace(state={})
    ordinary = SimpleNamespace(req=SearchSourceRequest(source_id='s1', query='risk'), call=SimpleNamespace(id='ordinary'))
    _record_counter_search(ctx, ordinary, scope='document', result={'status': 'completed'}, source_id='s1')
    assert ctx.state == {}
    request = SearchSourceRequest(source_id='s1', query='risk', counter_search={'target': 'IPO valuation', 'hypothesis': 'Valuation may be unsustainable'})
    invocation = SimpleNamespace(req=request, call=SimpleNamespace(id='counter-1'))
    for _ in range(2):
        _record_counter_search(ctx, invocation, scope='document', result={'status': 'completed', 'total_matches': 0}, source_id='s1')
    assert len(ctx.state['counter_searches']) == 1
    assert ctx.state['counter_searches'][0]['matches'] == 0
    assert ctx.state['counter_searches'][0]['target'] == 'IPO valuation'


def test_linked_counterfinding_requires_a_distinct_source_and_explicit_target():
    target = _source('target')
    contrary = _source('contrary')
    state = {'prior_claims_meta': [{'claim_id': 'c1', 'source_ids': ['target']}], 'counter_searches': []}
    def claim(sid):
        return {'claim_id': 'c2', 'role': 'counterargument', 'challenges_claim_ids': ['c1'], 'citations': [{'source_id': sid, 'quotation': 'Evidence'}]}
    same = insights.evaluate_method_and_reconciliation(state, {'target': target, 'contrary': contrary}, claims=[claim('target')])
    assert same['counter_evidence']['linked_findings'] == []
    distinct = insights.evaluate_method_and_reconciliation(state, {'target': target, 'contrary': contrary}, claims=[claim('contrary')])
    assert distinct['counter_evidence']['linked_findings'][0]['independence'] == 'not_verified'
    assert distinct['counter_evidence']['linked_findings'][0]['quotation'] == 'Evidence'
