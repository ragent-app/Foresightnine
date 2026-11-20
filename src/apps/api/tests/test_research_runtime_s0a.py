"""S0-A runtime behaviours: research clock, price lookback/coverage, read-only failure continuation, guidance."""
import json
from datetime import date, datetime, timezone

import pytest

from app.model_protocol import ModelResponse
from app.phase3 import ProviderFailure
from app.research_clock import build_research_clock
from app.research_runtime import SYSTEM, TOOL_FAILURE_GUIDANCE, HistoricalPriceRequest, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions

from tests.test_research_runtime import Model, context, turn

STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def make_source(source_id='p1', text='Revenue was 100 USD.', provenance=None, url=None, kind='user_supplied'):
    return ResearchSource(source_id=source_id, owner_id='owner', title='T ' + source_id, text=text, url=url, kind=kind,
                          obtained_at=NOW, content_sha256='h' + source_id, provenance=provenance or {},
                          permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))


def price_args(**extra):
    return {'instrument_id': 'AAPL', 'rationale': 'prices', **extra}


def feedback(model, index=1):
    return json.loads(model.inputs[index][-1]['content'])


def test_clock_only_in_first_user_message_and_resume_keeps_it():
    clock = build_research_clock(NOW)
    saved = []
    m = Model([STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), clock=clock,
                       checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    first_user = json.loads(saved[-1]['user'])
    assert first_user['research_clock'] == clock
    assert clock['utc_date'] not in SYSTEM and '2026' not in SYSTEM  # rule refers to the field by name only
    assert 'research_clock' in SYSTEM
    # A resumed run keeps the saved first message even if a different clock is supplied.
    m2 = Model([STOP])
    other = build_research_clock(datetime(2030, 1, 1, tzinfo=timezone.utc))
    resumed = []
    run_research_agent(question='Q', sources=[], model=m2, context=context(), clock=other, initial_state=saved[-1],
                       checkpoint=lambda s: resumed.append(json.loads(json.dumps(s))))
    assert json.loads(resumed[-1]['user'])['research_clock'] == clock


def test_no_clock_means_no_field():
    saved = []
    run_research_agent(question='Q', sources=[], model=Model([STOP]), context=context(),
                       checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert 'research_clock' not in json.loads(saved[-1]['user'])


def test_price_request_requires_lookback_or_both_dates():
    with pytest.raises(ValueError):
        HistoricalPriceRequest(instrument_id='A', rationale='r')
    with pytest.raises(ValueError):
        HistoricalPriceRequest(instrument_id='A', rationale='r', start_date=date(2026, 1, 1))
    with pytest.raises(ValueError):
        HistoricalPriceRequest(instrument_id='A', rationale='r', lookback='1y', start_date=date(2026, 1, 1), end_date=date(2026, 2, 1))
    assert HistoricalPriceRequest(instrument_id='A', rationale='r', lookback='1y').lookback == '1y'


def test_lookback_is_resolved_with_instrument_exchange_and_coverage_is_returned():
    seen = []
    coverage = {'requested_end_date': '2026-11-19', 'effective_end_date': '2026-11-19', 'clamped': False,
                'actual_first_date': '2026-11-13', 'actual_last_date': '2026-11-19', 'row_count': 5,
                'latest_completed_session': '2026-11-19', 'session_status': 'pre_open'}

    def loader(req):
        seen.append(req)
        return make_source(provenance={**coverage, 'unrelated': 1})

    m = Model([turn('historical_prices', price_args(lookback='5d'), 'h1'), STOP])
    out = run_research_agent(question='Q', sources=[], model=m, context=context(), historical_prices=loader,
                             historical_instruments=('AAPL',), historical_sessions={'AAPL': ('NASDAQ', 'America/New_York')},
                             now=lambda: NOW)
    assert seen[0].end_date == date(2026, 11, 19) and seen[0].start_date < seen[0].end_date
    fb = feedback(m)
    assert fb['status'] == 'acquired' and fb['source_id'] == 'p1'
    assert fb['coverage'] == coverage and 'content_sha256' in fb and fb['total_characters'] == 20
    assert 'p1' in out.source_ids  # acquired but unread


def test_future_end_date_allowed_without_cutoff_and_range_error_has_message_and_hints_and_run_continues():
    def loader(req):
        err = ProviderFailure('historical_price_range_after_latest_session', 'End date is after the latest session.', retryable=False)
        err.hints = {'latest_allowed_end_date': '2026-11-19'}
        raise err

    m = Model([turn('historical_prices', price_args(start_date='2026-11-01', end_date='2027-01-01'), 'h1'), STOP])
    out = run_research_agent(question='Q', sources=[], model=m, context=context(), historical_prices=loader,
                             historical_instruments=('AAPL',))
    fb = feedback(m)
    assert fb['status'] == 'failed' and fb['reason'] == 'historical_price_range_after_latest_session'
    assert fb['hints'] == {'latest_allowed_end_date': '2026-11-19'} and fb['message']
    assert out.reason != 'historical_price_range_after_latest_session'


def test_research_cutoff_still_rejects_later_end_date():
    m = Model([turn('historical_prices', price_args(start_date='2024-01-01', end_date='2024-12-31'), 'h1'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), historical_prices=lambda r: None,
                       historical_instruments=('AAPL',), as_of=datetime(2024, 6, 1, tzinfo=timezone.utc))
    assert feedback(m)['status'] == 'invalid'


def test_unknown_price_outcome_is_failed_not_aborting():
    def loader(req):
        err = ProviderFailure('historical_price_transport_error', 'timeout', retryable=True)
        err.external_result_unknown = True
        raise err

    m = Model([turn('historical_prices', price_args(start_date='2026-11-01', end_date='2026-11-10'), 'h1'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), historical_prices=loader, historical_instruments=('AAPL',))
    fb = feedback(m)
    assert fb['status'] == 'failed' and fb['external_state'] == 'unknown' and 'not retried' in fb['message']


def test_acquire_with_unknown_and_success_continues_and_keeps_source():
    discover_hits = [{'title': 'A', 'url': 'https://a.example.com/x'}, {'title': 'B', 'url': 'https://b.example.com/y'}]
    good = make_source('good', url='https://b.example.com/y', kind='official')

    def acquire(url, title):
        if 'a.example' in url:
            return None, {'status': 'failed', 'outcomes': [{'reason': 'source_timeout', 'external_state': 'unknown'}]}
        return good, {'outcomes': [{'reason': None, 'external_state': 'returned'}]}

    m = Model([turn('discover_sources', {'query': 'q', 'rationale': 'r'}, 'd1'),
               turn('acquire_sources', {'urls': ['https://a.example.com/x', 'https://b.example.com/y'], 'rationale': 'r'}, 'a1'),
               STOP])
    out = run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda q: discover_hits, acquire=acquire)
    fb = feedback(m, 2)
    rows = {r['url']: r for r in fb['results']}
    assert rows['https://a.example.com/x']['status'] == 'failed'
    assert rows['https://a.example.com/x']['reason'] == 'source_timeout'
    assert 'not retried' in rows['https://a.example.com/x']['message']
    assert rows['https://b.example.com/y']['status'] == 'acquired'
    assert 'good' in out.source_ids
    assert out.reason != 'acquisition_result_unknown'
    assert len(m.inputs) == 3


def test_failure_rows_get_table_guidance():
    m = Model([turn('acquire_sources', {'urls': ['https://x.example.com/'], 'rationale': 'r'}, 'a1'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), acquire=lambda u, t: (None, {}))
    fb = feedback(m)
    assert fb['results'][0]['message'] == TOOL_FAILURE_GUIDANCE['lead_not_discovered']
    assert fb['message'] == TOOL_FAILURE_GUIDANCE['lead_not_discovered']


def test_guidance_table_covers_required_codes():
    for code in ('historical_price_range_after_latest_session', 'historical_price_source_not_authorized_or_unavailable',
                 'historical_price_rate_limited', 'historical_price_budget_unavailable', 'historical_price_transport_error',
                 'historical_price_provider_rejected', 'unit_or_period_not_found_in_returned_records', 'tag_not_reported',
                 'source_too_large', 'ocr_required', 'source_timeout', 'source_unavailable', 'http_forbidden_or_challenge',
                 'not_found', 'source_rate_limited', 'lead_not_discovered', 'search_no_results', 'search_sources_denied'):
        assert TOOL_FAILURE_GUIDANCE[code]


def test_search_no_results_gets_message_and_sec_extras_pass_through():
    from app.research_structured_connector import StructuredQueryResult
    m = Model([turn('discover_sources', {'query': 'q', 'rationale': 'r'}, 'd1'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda q: [])
    assert feedback(m)['reason'] == 'search_no_results'
    # reason + status 'completed' is not a failure status, so no message is forced there.

    res = StructuredQueryResult('unknown', 'unit_or_period_not_found_in_returned_records', external_state='returned')
    res.available_periods = ['2024-01-29/2025-01-26']
    res.available_units = ['USD']
    res.coverage = {'first': '2020'}
    sec_args = {'entity_query': 'NVDA', 'tag': 'Revenues', 'unit': 'USD', 'period_start': '2024-01-29',
                'period_end': '2025-01-26', 'rationale': 'r'}
    m2 = Model([turn('sec_company_concept', sec_args, 's1'), STOP])
    run_research_agent(question='Q', sources=[], model=m2, context=context(), sec_concept=lambda r: res)
    fb = feedback(m2)
    assert fb['available_periods'] == ['2024-01-29/2025-01-26'] and fb['available_units'] == ['USD']
    assert fb['coverage'] == {'first': '2020'} and 'available_periods' in fb['message']


def test_redirect_leads_report_unresolved_titles_without_resolving():
    hits = [{'title': f'Title {i} ' + 'x' * 200, 'url': f'https://www.google.com/goto?url=TOKEN{i}&gsessionid=x'} for i in range(7)]
    hits.append({'title': 'Ok', 'url': 'https://www.sec.gov/ok'})
    m = Model([turn('discover_sources', {'query': 'q', 'rationale': 'r'}, 'd1'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda q: hits)
    fb = feedback(m)
    assert len(fb['unresolved_titles']) == 5 and all(len(t) <= 160 for t in fb['unresolved_titles'])
    assert fb['message'] and fb['denied_reasons']['redirect_link'] == 7
    assert [lead['url'] for lead in fb['leads']] == ['https://www.sec.gov/ok']


def test_acquired_but_unread_sources_enter_outcome_source_ids():
    src = make_source('unread', url='https://b.example.com/y', kind='official')
    m = Model([turn('discover_sources', {'query': 'q', 'rationale': 'r'}, 'd1'),
               turn('acquire_sources', {'urls': ['https://b.example.com/y'], 'rationale': 'r'}, 'a1'), STOP])
    out = run_research_agent(question='Q', sources=[], model=m, context=context(),
                             discover=lambda q: [{'title': 'B', 'url': 'https://b.example.com/y'}],
                             acquire=lambda u, t: (src, {}))
    assert 'unread' in out.source_ids
