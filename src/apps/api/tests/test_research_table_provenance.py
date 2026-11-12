import base64
from datetime import datetime, timezone
from hashlib import sha256

from app.research_sources import ResearchSource, SourcePermissions
from app.research_table_provenance import trace_html_table
from app.research_uploads import HtmlUploadRequest, extract_uploaded_html


def source_for(html):
    raw = html.encode()
    encoded = base64.b64encode(raw).decode()
    permissions = SourcePermissions(store=True, display=True)
    text, raw_hash = extract_uploaded_html(HtmlUploadRequest(title='Filing', html_base64=encoded, permissions=permissions))
    return ResearchSource(source_id='s', owner_id='o', title='Filing', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        retrieval_method='user_html', response_sha256=raw_hash, original_html_base64=encoded, permissions=permissions)


def trace(source):
    start = source.text.index('100')
    return trace_html_table(source, start, start + 3)


def test_original_html_structure_traces_selected_row_and_actual_spans():
    source = source_for('<p>Unit: millions</p><table><tr><th colspan="2">Years Ended</th></tr>'
                        '<tr><th>2024</th><th>2023</th></tr><tr><td>100</td><td>90</td></tr></table>')
    result = trace(source)
    assert result is not None and result['source_sha256'] == source.response_sha256
    assert [cell['text'] for cell in result['rows'][1]['cells']] == ['2024', '2023']
    assert result['rows'][0]['cells'][0] == {'start': 0, 'end': 2, 'text': 'Years Ended', 'kind': 'th'}
    assert result['rows'][2]['cells'][0]['text'] == '100'
    assert source.text[result['row_start']:result['row_end']] == result['rows'][-1]['text']


def test_original_trace_fails_closed_for_forgery_or_ambiguous_markup():
    html = '<table><tr><td>2024</td><td>2023</td></tr><tr><td>100</td><td>90</td></tr></table>'
    source = source_for(html)
    assert trace(source.model_copy(update={'response_sha256': '0' * 64})) is None
    assert trace(source.model_copy(update={'content_sha256': '0' * 64})) is None
    assert trace(source.model_copy(update={'text': source.text.replace('2024', '2022'),
                                           'content_sha256': sha256(source.text.replace('2024', '2022').encode()).hexdigest()})) is None
    assert trace(source_for(html + html)) is None
    assert trace(source_for(html.replace('<td>100', '<td rowspan="2">100'))) is None
    assert trace(source_for(html.replace('2024', '[colspan=2] 2024'))) is None
    nested = '<table><tr><td><table><tr><td>x</td></tr></table></td><td>90</td></tr><tr><td>100</td><td>90</td></tr></table>'
    assert trace(source_for(nested)) is None
    distant_span = '<table><tr><td rowspan="20">old</td><td>1</td></tr>' + '<tr><td>a</td><td>b</td></tr>' * 16 + '<tr><td>100</td><td>90</td></tr></table>'
    assert trace(source_for(distant_span)) is None


def test_trace_deadline_retains_worker_slot_until_work_finishes(monkeypatch):
    from threading import BoundedSemaphore, Event
    import app.research_table_provenance as module
    slots, entered, release = BoundedSemaphore(1), Event(), Event()
    monkeypatch.setattr(module, '_TABLE_TRACE_WORKERS', slots)
    def slow(*args):
        entered.set()
        release.wait(2)
        return None
    monkeypatch.setattr(module, 'trace_html_table', slow)
    source = source_for('<table><tr><td>100</td><td>90</td></tr></table>')
    assert module.bounded_table_trace(source, 0, 3, timeout=0.01) == {'status': 'timeout'}
    assert entered.is_set()
    assert module.bounded_table_trace(source, 0, 3, timeout=0.01) == {'status': 'busy'}
    release.set()
    assert slots.acquire(timeout=2)
    slots.release()
