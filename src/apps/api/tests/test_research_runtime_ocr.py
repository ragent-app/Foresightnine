"""On-demand OCR in the research runtime: request_ocr, automatic OCR in read_pages, source versions,
OCR evidence rules and billing of vision-model OCR calls."""
import base64
import itertools
import json
import os
import shutil
from datetime import datetime, timezone
from time import monotonic

import pytest

import app.research_ocr as research_ocr
from app.openai_compatible import BinaryHttpResponse, _TransportTimeout
from app.research_blob_store import DocumentBlobStore
from app.research_documents import extract_pdf_pages, page_for_offset
from app.research_ocr import (OcrCache, OcrPageResult, OcrUsageHooks, TesseractEngine, VisionModelEngine,
                              build_ocr_pages_callback, ocr_pdf_pages, run_source_ocr)
from app.research_runtime import TOOLS, SYSTEM, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions

from tests.test_research_ocr import FakeEngine, build_mixed_pdf, needs_tesseract, scanned_page_jpeg, vision_engine
from tests.test_research_runtime import Model, context, turn
from tests.test_research_runtime_s0b import STOP, multi, tool_messages

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TEXT_PAGE = 'Revenue was 100 USD.'


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv('FORESIGHTNINE_DOCUMENT_STORE_DIR', raising=False)
    for name in list(os.environ):
        if name.startswith('FORESIGHTNINE_OCR_'):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(research_ocr, '_PROCESS_CACHE', OcrCache())


def scan_page(tmp_path, text='Quarterly revenue 4215 million', name='scan'):
    return scanned_page_jpeg(tmp_path, text, name)


def pdf_source(tmp_path, pages, *, source_id='pdf', retain='base64', store=None):
    """A ResearchSource built from a real PDF with the page map produced by ingestion (needs_ocr for image pages)."""
    raw = build_mixed_pdf(pages)
    path = tmp_path / f'{source_id}.pdf'
    path.write_bytes(raw)
    extracted = extract_pdf_pages(path, deadline=monotonic() + 60)
    document_pages = dict(extracted.document_pages)
    kwargs = {}
    if retain == 'base64':
        kwargs['original_pdf_base64'] = base64.b64encode(raw).decode('ascii')
    elif retain == 'blob':
        document_pages['original_blob_sha256'] = store.put_file(path)
        document_pages['original_retained'] = True
    return ResearchSource(
        source_id=source_id, owner_id='owner', title='Annual report', text=extracted.text, kind='user_supplied',
        retrieval_method='web_pdf', obtained_at=NOW, content_sha256='h' + source_id,
        provenance={'document_pages': document_pages},
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True), **kwargs)


class VersionStore:
    """Stands in for ResearchSources.create: keeps the derived versions the callback persists."""

    def __init__(self):
        self.versions: list[ResearchSource] = []

    def __call__(self, old, new_id, text, provenance):
        new = old.model_copy(update={'source_id': new_id, 'text': text, 'provenance': provenance,
                                     'derived_from_source_id': old.source_id, 'content_sha256': 'h' + new_id})
        self.versions.append(new)
        return new


class Hooks:
    """Reserve/settle recorder that, like the run context, refuses a call id it has already seen."""

    def __init__(self, deny=None):
        self.log: list[tuple] = []
        self.used: set[str] = set()
        self.deny = deny

    def usage(self):
        def reserve(call_id, max_tokens):
            if self.deny is not None:
                raise self.deny
            if call_id in self.used:
                raise ValueError('Model call identifier was already used in this run.')
            self.used.add(call_id)
            self.log.append(('reserve', call_id, max_tokens))

        def settle(call_id, tokens):
            self.log.append(('settle', call_id, tokens))
        return OcrUsageHooks(reserve, settle)


def callback(store, *, engines, usage=None, operation_id='op'):
    return build_ocr_pages_callback(operation_id=operation_id, persist_version=store, usage=usage,
                                    engines=engines, cache=OcrCache())


def run(model, sources, **kwargs):
    return run_research_agent(question='Q', sources=sources, model=model, context=context(), **kwargs)


_IDS = itertools.count(1)


def request(pages, rationale='ocr', source_id='pdf'):
    return ('request_ocr', {'source_id': source_id, 'pages': pages, 'rationale': rationale}, f'ocr{next(_IDS)}')


# ---- tool registration ---------------------------------------------------------------------------

def test_request_ocr_tool_registered_before_submit_and_system_stays_static():
    names = [t.name for t in TOOLS]
    assert names.index('request_ocr') == len(names) - 2 and names[-1] == 'submit_research'
    schema = next(t for t in TOOLS if t.name == 'request_ocr').input_schema
    assert set(schema['required']) == {'source_id', 'pages', 'rationale'}
    assert schema['properties']['pages']['maxItems'] == 20 and schema['properties']['pages']['minItems'] == 1
    assert 'request_ocr' in SYSTEM and '2026' not in SYSTEM


def test_request_ocr_validation_rejects_zero_page_and_more_than_20():
    m = Model([turn(*request([0, 1], 'a')), turn(*request(list(range(1, 22)), 'b')), STOP])
    run(m, [])
    assert all(json.loads(msg['content'])['status'] == 'invalid' for msg in m.inputs[-1] if msg.get('role') == 'tool')


def test_request_ocr_without_callback_reports_ocr_unavailable(tmp_path):
    src = pdf_source(tmp_path, [scan_page(tmp_path), TEXT_PAGE])
    m = Model([turn(*request([1])), STOP])
    run(m, [src])
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'ocr_unavailable' and fb['message']


# ---- request_ocr creates a derived version ----------------------------------------------------------

@needs_tesseract
def test_request_ocr_creates_derived_version_with_ocr_pages_and_shifted_offsets(tmp_path):
    src = pdf_source(tmp_path, [scan_page(tmp_path), TEXT_PAGE])
    assert [e[2] for e in src.provenance['document_pages']['pages']] == ['needs_ocr', 'text']
    old_text_start = src.provenance['document_pages']['pages'][1][0]
    store = VersionStore()
    m = Model([turn(*request([1])), turn('outline_source', {'source_id': 'pdf'}, 'o'), STOP])
    saved = []
    out = run(m, [src], ocr_pages=callback(store, engines=[TesseractEngine()]),
              checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    fb = json.loads(m.inputs[1][-1]['content'])
    new = store.versions[0]
    assert fb['status'] == 'completed' and fb['source_id'] == new.source_id and fb['previous_source_id'] == 'pdf'
    assert fb['pages'][0]['page'] == 1 and fb['pages'][0]['status'] == 'ocr' and fb['pages'][0]['engine'] == 'tesseract'
    assert new.derived_from_source_id == 'pdf' and new.provenance['supersedes_source_id'] == 'pdf'
    assert new.url == src.url and new.permissions == src.permissions and new.retrieval_method == src.retrieval_method
    document_pages = new.provenance['document_pages']
    assert [e[2] for e in document_pages['pages']] == ['ocr', 'text']
    assert document_pages['ocr']['1']['engine'] == 'tesseract' and document_pages['ocr_usage']['pages'] == 1
    start, end, _ = document_pages['pages'][0]
    assert 'revenue' in new.text[start:end].lower() and end > start
    # later pages shifted by the inserted text but still address the same page text
    new_text_start, new_text_end, _ = document_pages['pages'][1]
    assert new_text_start > old_text_start and new.text[new_text_start:new_text_end].strip() == TEXT_PAGE
    assert page_for_offset(document_pages, new_text_start) == 2
    # the old version is untouched and later outline calls resolve to the new version
    assert src.provenance['document_pages']['pages'][0][2] == 'needs_ocr' and 'Quarterly' not in src.text
    outline = json.loads(m.inputs[2][-1]['content'])
    assert outline['source_id'] == new.source_id and outline['source_version']['requested_source_id'] == 'pdf'
    assert outline['page_status_summary']['counts'] == {'ocr': 1, 'text': 1}
    assert outline['page_status_summary']['ocr']['pages'] == [1]
    assert saved[-1]['source_versions'] == {'pdf': new.source_id} and new.source_id in saved[-1]['acquired_source_ids']
    assert new.source_id in out.source_ids


@needs_tesseract
def test_read_pages_ocr_automatically_and_reads_from_new_version(tmp_path):
    src = pdf_source(tmp_path, [TEXT_PAGE, scan_page(tmp_path)])
    store = VersionStore()
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 2, 'rationale': 'r'}, 'a'), STOP])
    out = run(m, [src], ocr_pages=callback(store, engines=[TesseractEngine()]))
    fb = json.loads(m.inputs[1][-1]['content'])
    new = store.versions[0]
    assert fb['source_id'] == new.source_id and fb['source_version']['requested_source_id'] == 'pdf'
    assert fb['ocr']['status'] == 'completed' and fb['ocr']['source_id'] == new.source_id
    assert [r['status'] for r in fb['page_statuses']] == ['text', 'ocr']
    assert fb['page_statuses'][1]['ocr_mean_confidence'] is not None
    assert TEXT_PAGE in fb['results'][0]['text'] and 'revenue' in fb['results'][0]['text'].lower()
    assert out.source_ids == [new.source_id]
    tool_event = next(e for e in out.events if e.get('name') == 'read_pages')
    assert tool_event['ocr']['source_id'] == new.source_id and tool_event['ocr']['pages'] == [2]


def test_read_pages_does_not_retry_pages_whose_ocr_failed(tmp_path):
    src = pdf_source(tmp_path, [TEXT_PAGE, None])
    calls = []

    def failing(source, pages):
        calls.append(pages)
        return research_ocr.OcrOutcome('failed', None, 'ocr_no_text', 'OCR produced no usable text for the requested pages.',
                                       [{'page': 2, 'status': 'needs_ocr', 'error': 'empty_text'}])
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 2, 'rationale': 'r'}, 'a'),
               turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 2, 'rationale': 'again'}, 'b'),
               turn('read_pages', {'source_id': 'pdf', 'first_page': 2, 'last_page': 2, 'rationale': 'second'}, 'c'), STOP])
    run(m, [src], ocr_pages=failing)
    assert calls == [[2]]
    first = json.loads(m.inputs[1][-1]['content'])
    assert first['ocr']['status'] == 'failed' and [r['status'] for r in first['page_statuses']] == ['text', 'needs_ocr']
    assert 'pages' in first['results'][0] or first['results'][0]['status'] == 'read'


def test_ocr_unavailable_without_original(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE], retain='none')
    store = VersionStore()
    m = Model([turn(*request([1])), STOP])
    run(m, [src], ocr_pages=callback(store, engines=[FakeEngine(text='x')]))
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'ocr_unavailable' and fb['reason'] == 'original_not_retained'
    assert 'not retained' in fb['message'] and not store.versions


@needs_tesseract
def test_original_from_blob_store_when_no_inline_pdf(tmp_path, monkeypatch):
    blobs = DocumentBlobStore(tmp_path / 'blobs')
    monkeypatch.setenv('FORESIGHTNINE_DOCUMENT_STORE_DIR', str(tmp_path / 'blobs'))
    src = pdf_source(tmp_path, [scan_page(tmp_path), TEXT_PAGE], retain='blob', store=blobs)
    assert src.original_pdf_base64 is None
    store = VersionStore()
    m = Model([turn(*request([1])), STOP])
    run(m, [src], ocr_pages=callback(store, engines=[TesseractEngine()]))
    assert json.loads(m.inputs[1][-1]['content'])['status'] == 'completed'
    assert store.versions[0].provenance['document_pages']['original_blob_sha256']


def test_request_for_readable_pages_changes_nothing(tmp_path):
    src = pdf_source(tmp_path, [TEXT_PAGE])
    store = VersionStore()
    m = Model([turn(*request([1])), STOP])
    run(m, [src], ocr_pages=callback(store, engines=[FakeEngine(text='x')]))
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'unchanged' and fb['pages'][0]['note'] == 'already readable' and not store.versions


@needs_tesseract
def test_duplicate_request_ocr_reuses_new_version_without_running_again(tmp_path):
    src = pdf_source(tmp_path, [scan_page(tmp_path), TEXT_PAGE])
    store = VersionStore()
    engine_calls = []
    inner = TesseractEngine()

    class Counting:
        name, version = inner.name, inner.version
        available = staticmethod(inner.available)

        def ocr(self, png, *, langs, timeout):
            engine_calls.append(1)
            return inner.ocr(png, langs=langs, timeout=timeout)
    m = Model([turn(*request([1], 'first')), turn(*request([1], 'second wording')),
               turn(*request([1], 'third'), ) , STOP])
    run(m, [src], ocr_pages=callback(store, engines=[Counting()]))
    first, second = (json.loads(m.inputs[i][-1]['content']) for i in (1, 2))
    assert first['status'] == 'completed' and len(engine_calls) == 1 and len(store.versions) == 1
    assert second['status'] == 'duplicate_action' and second['reused'] is True
    assert second['source_id'] == first['source_id'] == store.versions[0].source_id


@needs_tesseract
def test_second_request_on_old_id_continues_from_latest_version(tmp_path):
    src = pdf_source(tmp_path, [scan_page(tmp_path, 'First scanned page', 'a'), scan_page(tmp_path, 'Second scanned page', 'b')])
    store = VersionStore()
    m = Model([turn(*request([1], 'one')), turn(*request([2], 'two')), STOP])
    run(m, [src], ocr_pages=callback(store, engines=[TesseractEngine()]))
    second = json.loads(m.inputs[2][-1]['content'])
    assert len(store.versions) == 2 and second['previous_source_id'] == store.versions[0].source_id
    assert second['source_version']['requested_source_id'] == 'pdf'
    assert [e[2] for e in store.versions[1].provenance['document_pages']['pages']] == ['ocr', 'ocr']
    assert store.versions[1].derived_from_source_id == store.versions[0].source_id


# ---- evidence rules ------------------------------------------------------------------------------

def _fact_run(tmp_path, confidence):
    src = pdf_source(tmp_path, [None, TEXT_PAGE])
    store = VersionStore()
    fact = {'label': 'net revenue', 'value': 250, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
            'quotation': 'Net revenue was 250 USD.'}

    class Scripted(Model):
        def complete(self, messages, tools, **kwargs):
            self.inputs.append(json.loads(json.dumps(messages)))
            results = [json.loads(x['content']) for x in messages if x.get('role') == 'tool']
            if not results:  # read_pages OCRs page 1 and reads from the new version
                return turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 2, 'rationale': 'r'}, 'a')
            if len(results) == 1:
                return turn('record_facts', {'facts': [{**fact, 'source_id': results[0]['source_id']}], 'rationale': 'record'}, 'f')
            return STOP
    model = Scripted([])
    engine = FakeEngine(text='Net revenue was 250 USD.', confidence=confidence)
    out = run(model, [src], ocr_pages=callback(store, engines=[engine]))
    return model, out, store


def test_fact_on_ocr_page_is_marked_and_warned_when_confidence_is_low(tmp_path):
    model, out, store = _fact_run(tmp_path, 40.0)
    fb = json.loads(model.inputs[2][-1]['content'])
    fact = out.facts[0]
    assert fact['extraction_method'] == 'ocr' and fact['ocr_engine'] == 'tesseract' and fact['ocr_mean_confidence'] == 40.0
    assert fact['source_id'] == store.versions[0].source_id
    assert fb['warnings'][0]['page'] == 1 and 'original page image' in fb['warnings'][0]['message']


def test_fact_on_confident_ocr_page_is_marked_without_warning(tmp_path):
    model, out, _ = _fact_run(tmp_path, 96.0)
    fb = json.loads(model.inputs[2][-1]['content'])
    assert out.facts[0]['extraction_method'] == 'ocr' and 'warnings' not in fb


def test_fact_on_text_page_has_no_ocr_marking(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE])

    class Scripted(Model):
        def complete(self, messages, tools, **kwargs):
            self.inputs.append(json.loads(json.dumps(messages)))
            n = sum(1 for x in messages if x.get('role') == 'tool')
            if n == 0:
                return turn('read_pages', {'source_id': 'pdf', 'first_page': 2, 'last_page': 2, 'rationale': 'r'}, 'a')
            if n == 1:
                return turn('record_facts', {'facts': [{'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD',
                            'period': '2025', 'source_id': 'pdf', 'quotation': TEXT_PAGE}], 'rationale': 'r'}, 'f')
            return STOP
    out = run(Scripted([]), [src])
    assert 'extraction_method' not in out.facts[0]


def test_ocr_fact_fields_survive_topic_fact_validation():
    from app.research_topics import ResearchFact
    fact = ResearchFact.model_validate({
        'fact_id': 'f', 'label': 'x', 'value': '1', 'unit': 'USD', 'period': '2025', 'source_id': 's', 'quotation': 'q',
        'character_start': 0, 'character_end': 1, 'location': {'character_start': 0, 'character_end': 1},
        'verification': 'v', 'extraction_method': 'ocr', 'ocr_engine': 'tesseract', 'ocr_mean_confidence': 80.0})
    assert fact.extraction_method == 'ocr'


# ---- vision engine billing ---------------------------------------------------------------------------

def vision_transport(log, *, tokens=(900, 30), text='Revenue | 4,215', fail=None):
    def transport(method, url, headers, body, timeout, **kwargs):
        log.append(1)
        if fail == 'timeout':
            raise _TransportTimeout()
        reply = {'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': tokens[0], 'output_tokens': tokens[1]},
                 'stop_reason': 'end_turn'}
        return BinaryHttpResponse(200, json.dumps(reply).encode(), {})
    return transport


def dead_local():
    return FakeEngine(error='engine_failed')


def test_vision_engine_disabled_without_hooks(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE])
    sent = []
    vision = vision_engine(vision_transport(sent))
    store = VersionStore()
    m = Model([turn(*request([1])), STOP])
    run(m, [src], ocr_pages=callback(store, engines=[dead_local(), vision], usage=None))
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'failed' and fb['pages'][0]['status'] == 'needs_ocr' and not sent and not store.versions


def test_vision_engine_reserves_and_settles_once_per_page_and_records_events(tmp_path):
    src = pdf_source(tmp_path, [scan_page(tmp_path, 'First page', 'a'), scan_page(tmp_path, 'Second page', 'b'), TEXT_PAGE])
    sent = []
    hooks = Hooks()
    store = VersionStore()
    m = Model([turn(*request([1, 2])), STOP])
    out = run(m, [src], ocr_pages=callback(store, engines=[dead_local(), vision_engine(vision_transport(sent))],
                                           usage=hooks.usage()))
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'completed' and len(sent) == 2
    ids = ['op:ocr:pdf:p1:vision_model', 'op:ocr:pdf:p2:vision_model']
    assert sorted(e[1] for e in hooks.log if e[0] == 'reserve') == ids
    assert sorted((e[1], e[2]) for e in hooks.log if e[0] == 'settle') == [(i, 930) for i in ids]
    assert all(e[2] == research_ocr.vision_input_token_estimate() + 4096 for e in hooks.log if e[0] == 'reserve')
    events = [e for e in out.events if e.get('kind') == 'ocr_vision']
    assert sorted(e['call_id'] for e in events) == ids
    assert all(e['status'] == 'settled' and e['input_tokens'] == 900 and e['output_tokens'] == 30
               and e['external_state'] == 'returned' and e['model'] == 'vision-test-model' for e in events)
    assert store.versions[0].provenance['document_pages']['ocr_usage']['llm_input_tokens'] == 1800


def test_vision_cache_hit_is_free_and_not_reserved(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE])
    sent = []
    hooks = Hooks()
    shared_cache = OcrCache()
    for _ in range(2):
        store = VersionStore()
        cb = build_ocr_pages_callback(operation_id='op', persist_version=store, usage=hooks.usage(), cache=shared_cache,
                                      engines=[dead_local(), vision_engine(vision_transport(sent))])
        assert cb(src, [1]).status == 'completed'
    assert len(sent) == 1 and len([e for e in hooks.log if e[0] == 'reserve']) == 1


def test_unknown_vision_outcome_is_settled_unknown_and_never_reissued(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE])
    sent = []
    hooks = Hooks()
    store = VersionStore()
    cb = callback(store, engines=[dead_local(), vision_engine(vision_transport(sent, fail='timeout'))], usage=hooks.usage())
    first = cb(src, [1])
    assert first.status == 'failed' and not store.versions and len(sent) == 1
    assert [e[:2] for e in hooks.log] == [('reserve', 'op:ocr:pdf:p1:vision_model'), ('settle', 'op:ocr:pdf:p1:vision_model')]
    assert hooks.log[1][2] is None  # usage unknown: nothing invented
    event = first.events[0]
    assert event['status'] == 'unknown' and event['external_state'] == 'unknown' and event['error'] == 'timeout'
    assert first.pages[0]['status'] == 'needs_ocr'
    # an explicit second attempt is refused by the run's call-id rule and nothing is sent again
    second = cb(src, [1])
    assert second.status == 'failed' and len(sent) == 1
    assert second.events[0]['status'] == 'denied' and second.events[0]['external_state'] == 'not_started'
    assert [e for e in hooks.log if e[0] == 'reserve'] == [hooks.log[0]]


def test_denied_reservation_leaves_page_needs_ocr_and_sends_nothing(tmp_path):
    from app.phase3 import ProviderFailure
    src = pdf_source(tmp_path, [None, TEXT_PAGE])
    sent = []
    hooks = Hooks(deny=ProviderFailure('provider_budget_exhausted', 'budget', retryable=False))
    cb = callback(VersionStore(), engines=[dead_local(), vision_engine(vision_transport(sent))], usage=hooks.usage())
    outcome = cb(src, [1])
    assert not sent and outcome.status == 'failed'
    assert outcome.events[0]['status'] == 'denied' and outcome.events[0]['reason'] == 'provider_budget_exhausted'
    assert not hooks.log


def test_two_responses_of_only_repeated_ocr_requests_stop_the_run(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE], retain='none')
    m = Model([turn(*request([1], 'a')), turn(*request([1], 'b')), turn(*request([1], 'c'))])
    out = run(m, [src], ocr_pages=callback(VersionStore(), engines=[FakeEngine()]))
    assert out.reason == 'no_progress'
    second = json.loads(m.inputs[2][-1]['content'])
    assert second['status'] == 'duplicate_action' and 'will stop' in second['run_notice']


def test_run_source_ocr_page_cap_and_deadline(tmp_path):
    src = pdf_source(tmp_path, [None, TEXT_PAGE])
    result = run_source_ocr(src, list(range(1, 60)), engines=[FakeEngine(text='Page text here')], cache=OcrCache())
    assert result.status == 'completed' and [r['page'] for r in result.pages][:2] == [1, 2]
    assert any(r['status'] == 'text' for r in result.pages)
    expired = run_source_ocr(src, [1], engines=[FakeEngine(text='x')], cache=OcrCache(), seconds=0.0001)
    assert expired.status in {'failed', 'completed'}  # a deadline can only reduce what is recognised, never raise


def test_ondemand_seconds_env_default(monkeypatch):
    assert research_ocr.on_demand_seconds() == 120.0
    monkeypatch.setenv('FORESIGHTNINE_OCR_ONDEMAND_SECONDS', '15')
    assert research_ocr.on_demand_seconds() == 15.0


# ---- API level: acquisition -> read_pages auto OCR through the run's billing -------------------------------

@pytest.mark.parametrize('at_ingestion', [False, True])
def test_api_run_ocr_vision_page_is_billed_in_ledger(monkeypatch, tmp_path, at_ingestion):
    import time
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi.testclient import TestClient

    import app.main as api
    import app.research_fetch as research_fetch
    from app.glm_mcp_search import SearchCandidate as ProviderSearchCandidate

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.delenv('FORESIGHTNINE_FETCH_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', raising=False)
    if not at_ingestion:  # leave the scanned page for on-demand OCR
        monkeypatch.setenv('FORESIGHTNINE_OCR_INGEST_MAX_PAGES', '0')
    monkeypatch.setenv('FORESIGHTNINE_OCR_FALLBACK_CONFIDENCE', '101')  # always consult the vision engine
    sent = []
    engine = vision_engine(vision_transport(sent, text='Quarterly revenue 4215 million', tokens=(900, 30)))
    monkeypatch.setattr(VisionModelEngine, 'from_environment', classmethod(lambda cls, **kw: engine))

    pdf_url = 'https://ir.example.com/report.pdf'
    pdf_bytes = build_mixed_pdf([TEXT_PAGE, scan_page(tmp_path)])

    class Search:
        supports_project_search = True

        def search(self, query, **kwargs):
            return [ProviderSearchCandidate('Report', pdf_url, 'Unverified lead', None, None, True)]

    def fake_transport(method, url, headers, body, timeout, **kwargs):
        assert url == pdf_url
        return BinaryHttpResponse(200, pdf_bytes, {'content-type': 'application/pdf'})

    seen = {}

    class Provider:
        name = 'ocr-api-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            if not results:
                return turn('news_search', {'query': 'Report revenue', 'rationale': 'find'}, 'd1')
            if len(results) == 1:
                return turn('acquire_sources', {'urls': [pdf_url], 'rationale': 'fetch'}, 'a1')
            if len(results) == 2:
                seen['pdf'] = results[1]['results'][0]['source_id']
                return turn('read_pages', {'source_id': seen['pdf'], 'first_page': 1, 'last_page': 2, 'rationale': 'read'}, 'r1')
            seen['read'] = results[2]
            return turn('submit_research', {
                'report': 'Revenue was 100 USD.', 'limitations': [], 'stopping_reason': 'answered',
                'claims': [{'text': 'Revenue 100 USD.', 'source_id': results[2]['source_id'], 'quotation': TEXT_PAGE,
                            'role': 'support'}]}, 's1')

    provider = Provider()
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'challenge_provider', provider)
    monkeypatch.setattr(api, '_reserve_search_call', lambda *_: None)
    monkeypatch.setattr(research_fetch, '_validated_addresses', lambda host, port, *, allow_private=False: ('93.184.216.34',))
    monkeypatch.setattr(research_fetch, '_default_binary_transport', fake_transport)

    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                                                        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What was revenue?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    run_resp = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
                           json={'parent_version_id': topic['versions'][0]['version_id']})
    assert run_resp.status_code == 202, run_resp.text
    operation_id = run_resp.json()['operation']['operation_id']
    for _ in range(600):
        op = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.02)
    assert op['status'] == 'completed', op
    assert len(sent) == 1, 'one vision call for the one scanned page'
    read = seen['read']
    new_id = read['source_id']
    if at_ingestion:  # recognised while the document was acquired: the source already has the OCR page, no new version
        assert new_id == seen['pdf'] and 'ocr' not in read
    else:
        assert new_id != seen['pdf'] and read['ocr']['status'] == 'completed' and read['source_version']['requested_source_id'] == seen['pdf']
    stored = client.get(f'/v2/research/sources/{new_id}', headers=headers).json()
    assert 'Quarterly revenue 4215 million' in stored['text'] and TEXT_PAGE in stored['text']
    version = client.get(path, headers=headers).json()['versions'][-1]
    assert version['content']['claims'][0]['citations'][0]['source_id'] == new_id
    settlement = client.get(path + '/runs/' + operation_id + '/settlement', headers=headers).json()
    # four research model calls plus one vision OCR call, all settled through the account ledger
    assert provider.calls == 4
    assert settlement['model_ledger']['settled_calls'] == provider.calls + 1, settlement
