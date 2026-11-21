"""Financial data tools in the research runtime: resolve_entity, market_data (BYOK), company_financials, filings.

No network: fake transports serve the fin_data fixtures."""
import json
from datetime import datetime, timezone

from app.fin_data.adapters.sec_edgar import SecEdgarAdapter
from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
from app.model_protocol import ModelResponse
from app.research_runtime import SYSTEM, TOOLS, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions

from tests.fin_data.helpers import NOW as TD_NOW, ConceptTransport
from tests.fin_runtime_helpers import (ACCOUNT, FakeTransport, Reply, fixture_bytes, json_reply, make_toolbox,
                                       memory_persist)
from tests.test_research_runtime import Model, context, turn

STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
FIN = {'cik': '320193', 'lines': ['revenue'], 'periods': 'last_4_quarters', 'rationale': 'quarterly revenue'}
PX = {'entity_id': 'SSE:ANT', 'start_date': '2026-11-16', 'end_date': '2026-11-19', 'rationale': 'prices'}


class Script:
    """Model whose next response depends on the tool results seen so far."""
    def __init__(self, fn):
        self.fn, self.inputs = fn, []

    def complete(self, messages, tools, **kwargs):
        self.inputs.append(messages)
        return self.fn([json.loads(m['content']) for m in messages if m.get('role') == 'tool'])


def core_caps(box):
    return {k: v for k, v in box.capabilities().items() if not k.startswith(('cn_', 'hk_'))}


def last_result(model, index=1):
    return json.loads(model.inputs[index][-1]['content'])


def run(model, **kw):
    return run_research_agent(question='Q', sources=kw.pop('sources', []), model=model, context=context(), **kw)


# ---------------------------------------------------------------- tool schemas
def test_tools_are_static_and_replace_the_old_ones():
    names = [t.name for t in TOOLS]
    assert 'historical_prices' not in names and 'sec_company_concept' not in names
    assert names[names.index('acquire_sources') + 1:names.index('record_facts')] == [
        'resolve_entity', 'market_data', 'company_financials', 'filings', 'issuer_documents']
    dumped = json.dumps([t.__dict__ for t in TOOLS], default=str) + SYSTEM
    assert '2026' not in dumped and '2025' not in dumped  # static, date free
    fin = next(t for t in TOOLS if t.name == 'company_financials').input_schema
    assert set(fin['required']) == {'rationale'} and 'derive_q4' in fin['properties']


# ---------------------------------------------------------------- market_data, BYOK
def test_market_data_byok_persists_owner_scoped_source_with_coverage_and_no_key_material():
    t = FakeTransport(json_reply('td_time_series_ipo.json'))
    persist = memory_persist()
    m = Model([turn('market_data', PX, 'p1'), STOP])
    out = run(m, fin_data=make_toolbox(twelve=t, persist=persist))
    url, headers = t.calls[0]
    assert headers['Authorization'] == 'apikey byok-secret-123' and 'byok-secret-123' not in url
    fb = last_result(m)
    assert fb['status'] == 'ok' and fb['source_id'] == 'src_1' and fb['source_id'] in out.source_ids
    assert fb['permissions'] == {'store': True, 'display': True, 'model_input': True, 'export': False, 'owner_scoped': True}
    assert fb['coverage']['rows'] == 4 and fb['coverage']['currency'] == 'CNY'
    assert fb['provenance']['provider'] == 'twelve_data' and len(fb['provenance']['response_sha256']) == 64
    source = persist.store[0]
    assert source.kind == 'market_data' and source.retrieval_method == 'market_data' and source.permissions.export is False
    assert source.provenance['credential_owner'] == 'user_byok' and source.provenance['owner_account_id'] == ACCOUNT
    assert source.provenance['coverage']['rows'] == 4 and source.provenance['row_count'] == 4
    assert source.text.splitlines()[0] == 'ANT@SSE | 2026-11-16 | 1day | O 68.88 H 95.00 L 68.88 C 93.10 V 185000000 CNY'
    assert 'byok-secret-123' not in source.model_dump_json() and 'byok-secret-123' not in json.dumps(fb)


def test_market_data_without_account_key_is_unavailable_and_platform_key_is_ignored(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_TWELVEDATA_API_KEY', 'platform-key')
    t = FakeTransport(json_reply('td_time_series_ipo.json'))
    box = make_toolbox(market_key=None, twelve=t)
    m = Model([turn('market_data', PX, 'p1'), STOP])
    out = run(m, fin_data=box)
    fb = last_result(m)
    assert fb['status'] == 'unavailable' and fb['reason'] == 'market_data_key_required'
    assert 'Settings' in fb['message'] and 'not coming from market data' in fb['message'] and not t.calls
    assert core_caps(box) == {'market_data_available': False, 'market_data_key_required': True,
                                  'sec_financials_available': True, 'filings_available': True}
    assert any(g['code'] == 'market_data_key_required' for g in out.gaps)


def test_market_data_policy_can_deny_byok_and_intraday_lines_keep_timestamps():
    from app.fin_data.policy import FinDataPolicy
    t = FakeTransport(json_reply('td_time_series_ipo.json'))
    m = Model([turn('market_data', PX, 'p1'), STOP])
    run(m, fin_data=make_toolbox(twelve=t, policy=FinDataPolicy(rules=[])))
    assert last_result(m)['status'] == 'denied' and last_result(m)['reason'] == 'not_in_policy' and not t.calls
    persist = memory_persist()
    t2 = FakeTransport(json_reply('td_time_series_intraday_ipo.json'))
    m2 = Model([turn('market_data', {**PX, 'interval': '30min', 'start_date': '2026-11-18', 'end_date': '2026-11-19'}, 'p1'), STOP])
    run(m2, fin_data=make_toolbox(twelve=t2, persist=persist))
    assert 'interval=30min' in t2.calls[0][0]
    assert persist.store[0].text.splitlines()[0].startswith('ANT@SSE | 2026-11-18T09:30:00+08:00 | 30min | O ')


def test_market_data_calls_are_counted_and_use_per_account_buckets():
    calls = []
    tight = InProcessRateLimiter({'twelve_data': ProviderLimit(0.001, 1, 800)}, now=lambda: TD_NOW)
    box = make_toolbox(twelve=FakeTransport(json_reply('td_time_series_ipo.json')), limiter=tight,
                       reserve_call=lambda: calls.append(1))
    m = Model([turn('market_data', PX, 'p1'),
               turn('market_data', {**PX, 'start_date': '2026-11-17', 'rationale': 'again'}, 'p2'), STOP])
    run(m, fin_data=box)
    assert last_result(m, 1)['status'] == 'ok'
    second = last_result(m, 2)
    assert second['status'] == 'rate_limited' and second['retryable'] is True and len(calls) == 2


def test_unknown_market_data_outcome_is_recorded_not_replayed():
    t = FakeTransport(OSError('boom'))
    m = Model([turn('market_data', PX, 'p1'), turn('market_data', {**PX, 'rationale': 'retry'}, 'p2'), STOP])
    run(m, fin_data=make_toolbox(twelve=t))
    assert last_result(m, 1)['status'] == 'unknown' and last_result(m, 1)['external_state'] == 'unknown'
    assert last_result(m, 2)['status'] == 'duplicate_action' and len(t.calls) == 1


# ---------------------------------------------------------------- company_financials
def test_company_financials_lines_are_quotable_by_record_facts():
    t = ConceptTransport()
    persist = memory_persist()

    def step(results):
        if not results:
            return turn('company_financials', FIN, 'f1')
        if len(results) == 1:
            assert results[0]['status'] in ('ok', 'partial')
            return turn('read_sources', {'reads': [{'source_id': results[0]['source_id']}], 'rationale': 'read'}, 'r1')
        if len(results) == 2:
            line = results[1]['results'][0]['text'].splitlines()[0]
            value = line.rsplit('value ', 1)[1].split(' ')[0]
            assert 'accn ' in line and 'form ' in line and 'filed ' in line and line.split(' | ')[2].startswith('us-gaap:')
            return turn('record_facts', {'rationale': 'record', 'facts': [{
                'label': 'revenue', 'value': value, 'unit': 'USD', 'period': line.split(' | ')[3],
                'source_id': 'src_1', 'quotation': line}]}, 'rf')
        return STOP

    m = Script(step)
    out = run(m, fin_data=make_toolbox(sec=t, persist=persist))
    facts = out.facts
    assert len(facts) == 1 and facts[0]['currency'] == 'USD' and facts[0]['source_id'] == 'src_1'
    source = persist.store[0]
    assert source.kind == 'official' and source.retrieval_method == 'sec_companyfacts' and source.permissions.export is True
    eps = source.provenance['endpoints']
    assert eps and all('/companyconcept/' in u for u in eps) and str(source.url) == eps[0] and source.provenance['endpoint'] == eps[0]
    assert 'companyfacts' not in str(source.url) and source.provenance['provider_calls'] >= len(eps)
    assert source.provenance['provider'] == 'sec_edgar' and len(source.provenance['provider_response_sha256']) == 64


def test_company_financials_by_ticker_requires_unique_exact_match_and_counts_both_requests():
    calls = []
    t = ConceptTransport(others={'company_tickers': json_reply('sec_tickers.json')})
    m = Model([turn('company_financials', {'ticker': 'EXM', 'lines': ['revenue'], 'periods': 'last_4_quarters',
                                           'rationale': 'by ticker'}, 'f1'), STOP])
    run(m, fin_data=make_toolbox(sec=t, reserve_call=lambda: calls.append(1)))
    fb = last_result(m)
    assert fb['status'] in ('ok', 'partial') and fb['entity_id'] == 'cik:0000320193' and len(calls) == 1 + len(t.concept_urls)  # tickers + one per concept file
    t2 = FakeTransport(json_reply('sec_tickers.json'))
    m2 = Model([turn('company_financials', {'ticker': 'NOPE', 'rationale': 'x'}, 'f1'), STOP])
    run(m2, fin_data=make_toolbox(sec=t2))
    assert last_result(m2)['reason'] == 'entity_not_found'


def test_duplicate_company_financials_and_market_data_are_reused_not_refetched():
    sec, td = ConceptTransport(), FakeTransport(json_reply('td_time_series_ipo.json'))
    m = Model([turn('company_financials', FIN, 'a'), turn('company_financials', {**FIN, 'rationale': 'again'}, 'b'),
               turn('market_data', PX, 'c'), turn('market_data', {**PX, 'rationale': 'again'}, 'd'), STOP])
    run(m, fin_data=make_toolbox(sec=sec, twelve=td))
    for index in (2, 4):
        dup = last_result(m, index)
        assert dup['status'] == 'duplicate_action' and dup['reused'] is True and dup['source_id']
    urls = [u for u, _ in sec.calls]
    assert urls and len(urls) == len(set(urls)) and len(td.calls) == 1  # nothing fetched twice


def test_sec_requests_use_global_pacing_and_rate_limit_without_calling_the_provider():
    t = FakeTransport(json_reply('sec_companyfacts.json'))
    m = Model([turn('company_financials', FIN, 'a'), STOP])
    run(m, fin_data=make_toolbox(sec=t, reserve_sec=lambda: False))
    fb = last_result(m)
    assert fb['status'] == 'rate_limited' and fb['retryable'] is True and not t.calls
    assert fb['suggested_actions'][0]['retry_after_seconds'] == 1.0


def test_large_companyfacts_stream_to_a_temp_file_and_parse():
    doc = json.loads(fixture_bytes('sec_companyfacts.json'))
    doc['facts']['us-gaap']['PaddingTag'] = {'units': {'USD': [
        {'end': '2020-12-31', 'val': i, 'accn': f'0000000000-20-{i:06d}', 'fy': 2020, 'fp': 'FY', 'form': '10-K',
         'filed': '2021-02-01'} for i in range(60000)]}}
    data = json.dumps(doc).encode()
    assert len(data) > 5_000_000
    streamed = []

    def stream(method, url, headers, body, timeout, sink):
        streamed.append(url)
        sink.write(data)
        return Reply(200, b'')

    plain = FakeTransport(Reply(500))
    persist = memory_persist()
    m = Model([turn('company_financials', {k: v for k, v in FIN.items() if k != 'lines'}, 'a'), STOP])
    run(m, fin_data=make_toolbox(sec=plain, sec_stream=stream, persist=persist))
    fb = last_result(m)
    assert fb['status'] in ('ok', 'partial') and streamed and 'companyfacts' in streamed[0] and not plain.calls
    from hashlib import sha256
    assert fb['provenance']['response_sha256'] == sha256(data).hexdigest()
    assert persist.store[0].text  # parsed from the temp file


def test_adapter_default_wiring_streams_only_without_injected_transport():
    assert SecEdgarAdapter('ForesightNine Test ops@example.com')._stream is not None
    assert SecEdgarAdapter('ForesightNine Test ops@example.com', transport=FakeTransport(Reply()))._stream is None


# ---------------------------------------------------------------- filings and acquire_sources
def _acquired(url):
    source = ResearchSource(source_id='doc1', owner_id='o', title='10-K doc', text='Revenue was 100 USD.', url=url, kind='official',
                            obtained_at=datetime(2026, 11, 20, tzinfo=timezone.utc), content_sha256='h',
                            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    return source, {'status': 'completed', 'outcomes': []}


def test_filings_list_urls_are_accepted_by_acquire_sources_and_other_urls_are_not():
    fetched = []

    def acquire(url, title):
        fetched.append((url, title))
        return _acquired(url)

    def step(results):
        if not results:
            return turn('filings', {'cik': '320193', 'forms': ['10-K'], 'rationale': 'list'}, 'f1')
        if len(results) == 1:
            row = results[0]['filings'][0]
            assert row['form'] == '10-K' and row['primary_document_url'].startswith('https://www.sec.gov/Archives/edgar/data/320193/')
            assert 'acquire_sources' in results[0]['lead_note']
            return turn('acquire_sources', {'urls': [row['primary_document_url'],
                                                     'https://www.sec.gov/Archives/edgar/data/1/guessed.htm'],
                                            'rationale': 'fetch'}, 'a1')
        return STOP

    m = Script(step)
    out = run(m, fin_data=make_toolbox(sec=FakeTransport(json_reply('sec_submissions.json'))), acquire=acquire)
    res = last_result(m, 2)['results']
    assert res[0]['status'] == 'acquired' and res[1]['reason'] == 'lead_not_discovered'
    assert len(fetched) == 1 and fetched[0][0].startswith('https://www.sec.gov/Archives/') and '10-K filed' in fetched[0][1]
    assert 'doc1' in out.source_ids


def test_filings_search_is_denied_by_default_policy_without_a_request():
    t = FakeTransport(Reply())
    m = Model([turn('filings', {'action': 'search', 'query': 'going concern', 'rationale': 'x'}, 'f1'), STOP])
    run(m, fin_data=make_toolbox(sec=t))
    fb = last_result(m)
    assert fb['status'] == 'denied' and 'not enabled' in fb['message'] and not t.calls


# ---------------------------------------------------------------- resolve_entity
def test_resolve_entity_returns_all_candidates_caches_them_and_feeds_market_data():
    persist = memory_persist()
    sec, td = FakeTransport(json_reply('sec_tickers.json')), FakeTransport(json_reply('td_symbol_search.json'))
    state = []

    def step(results):
        if not results:
            return turn('resolve_entity', {'query': 'EXM', 'rationale': 'identify'}, 'e1')
        if len(results) == 1:
            ids = [c['entity_id'] for c in results[0]['candidates']]
            assert 'NASDAQ:EXM' in ids and len(ids) >= 3 and not any(c['unique'] for c in results[0]['candidates'])
            assert results[0]['reason'] == 'multiple_candidates'
            return STOP
        return STOP

    m = Script(step)
    run(m, fin_data=make_toolbox(sec=sec, twelve=td, persist=persist), checkpoint=lambda s: state.append(json.loads(json.dumps(s))))
    cache = state[-1]['entity_candidates']
    assert cache['NASDAQ:EXM']['identifiers']['cik'] == '0000320193' and cache['NASDAQ:EXM']['symbol'] == 'EXM'


def test_resolve_entity_without_market_key_uses_sec_only_and_says_so():
    sec = FakeTransport(json_reply('sec_tickers.json'))
    m = Model([turn('resolve_entity', {'query': 'example corp', 'market': 'US', 'rationale': 'identify'}, 'e1'), STOP])
    run(m, fin_data=make_toolbox(market_key=None, sec=sec))
    fb = last_result(m)
    assert [c['entity_id'] for c in fb['candidates']] == ['cik:0000320193'] and 'Twelve Data' in fb['notes'][0]


# ---------------------------------------------------------------- gating and capabilities
def test_disabled_runs_make_no_requests_and_report_the_reason():
    sec, td = FakeTransport(Reply()), FakeTransport(Reply())
    box = make_toolbox(sec=sec, twelve=td, disabled_reason='historical_cutoff')
    m = Model([turn('company_financials', FIN, 'a'), turn('market_data', PX, 'b'),
               turn('filings', {'cik': '320193', 'rationale': 'r'}, 'c'), turn('resolve_entity', {'query': 'X', 'rationale': 'r'}, 'd'), STOP])
    run(m, fin_data=box)
    for index in (1, 2, 3, 4):
        assert last_result(m, index) == {'status': 'restricted', 'reason': 'historical_cutoff'}
    assert not sec.calls and not td.calls
    assert core_caps(box) == {'market_data_available': False, 'market_data_key_required': False,
                                  'sec_financials_available': False, 'filings_available': False}


def test_capabilities_flags_follow_key_and_sec_contact():
    assert core_caps(make_toolbox()) == {'market_data_available': True, 'market_data_key_required': False,
                                             'sec_financials_available': True, 'filings_available': True}
    assert core_caps(make_toolbox(user_agent=None, market_key=None)) == {
        'market_data_available': False, 'market_data_key_required': True,
        'sec_financials_available': False, 'filings_available': False}


# ---------------------------------------------------------------- platform key fallback (policy gated)
def _permissive_policy():
    from app.fin_data.policy import FinDataPolicy, PolicyRule
    rules = [PolicyRule(provider='twelve_data', category=c, credential_owner=o, store=True, display=True,
                        model_input=True, owner_scoped=(o == 'user_byok'))
             for c in ('identity', 'bars_daily', 'bars_intraday') for o in ('platform', 'user_byok')]
    return FinDataPolicy(rules=rules)


class _Settings:
    def __init__(self, key):
        self.key = key

    def runtime(self, _account):
        return [], ({'twelve_data': self.key} if self.key else {})


def _resolve(monkeypatch, own_key, policy, platform_key='platform-key'):
    import app.main as api
    monkeypatch.setenv('FORESIGHTNINE_TWELVEDATA_API_KEY', platform_key)
    monkeypatch.setattr(api, 'market_data_settings', _Settings(own_key))
    return api._resolve_market_key('acct', policy=policy)


def test_resolver_default_policy_ignores_platform_key(monkeypatch):
    from app.fin_data.policy import default_policy
    assert _resolve(monkeypatch, None, default_policy()) == (None, None)
    assert _resolve(monkeypatch, 'own-key', default_policy()) == ('own-key', 'user_byok')


def test_resolver_permissive_policy_falls_back_to_platform_and_prefers_account_key(monkeypatch):
    assert _resolve(monkeypatch, None, _permissive_policy()) == ('platform-key', 'platform')
    assert _resolve(monkeypatch, 'own-key', _permissive_policy()) == ('own-key', 'user_byok')
    assert _resolve(monkeypatch, None, _permissive_policy(), platform_key='') == (None, None)


def test_platform_key_market_data_uses_platform_owner_and_shared_bucket():
    from app.fin_data.policy import FinDataPolicy
    t = FakeTransport(json_reply('td_time_series_ipo.json'))
    persist = memory_persist()
    m = Model([turn('market_data', PX, 'p1'), STOP])
    box = make_toolbox(market_key='platform-key', credential_owner='platform', twelve=t, persist=persist,
                       policy=_permissive_policy())
    run(m, fin_data=box)
    assert t.calls[0][1]['Authorization'] == 'apikey platform-key'
    fb = last_result(m)
    assert fb['status'] == 'ok' and fb['permissions']['owner_scoped'] is False
    prov = persist.store[0].provenance
    assert prov['credential_owner'] == 'platform' and prov['owner_scoped'] is False and prov['owner_account_id'] is None
    assert 'platform-key' not in persist.store[0].model_dump_json()
    assert box.capabilities()['market_data_available'] is True and box.capabilities()['market_data_key_required'] is False
    # the default policy still denies a platform-owned call even if a caller passes one
    t2 = FakeTransport(json_reply('td_time_series_ipo.json'))
    m2 = Model([turn('market_data', PX, 'p1'), STOP])
    run(m2, fin_data=make_toolbox(market_key='platform-key', credential_owner='platform', twelve=t2))
    assert last_result(m2)['status'] == 'denied' and last_result(m2)['reason'] == 'license_platform_key_not_permitted' and not t2.calls


def test_capabilities_follow_resolved_market_key(monkeypatch):
    import app.main as api
    from app.fin_data.policy import default_policy
    for own, policy, expected in ((None, default_policy(), (False, True)), (None, _permissive_policy(), (True, False)),
                                  ('own-key', default_policy(), (True, False))):
        key, owner = _resolve(monkeypatch, own, policy)
        caps = make_toolbox(market_key=key, credential_owner=owner or 'user_byok').capabilities()
        assert (caps['market_data_available'], caps['market_data_key_required']) == expected
