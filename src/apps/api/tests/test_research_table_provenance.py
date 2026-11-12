import base64
from datetime import datetime, timezone
from hashlib import sha256

from app.research_sources import ResearchSource, SourcePermissions
from app.research_table_provenance import annual_table_value_alignment, trace_html_table
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
    entered.clear()
    release.clear()
    assert module.bounded_table_trace(source, 0, 3, should_stop=lambda: entered.is_set()) == {'status': 'cancelled'}
    assert module.bounded_table_trace(source, 0, 3, timeout=0.01) == {'status': 'busy'}
    release.set()
    assert slots.acquire(timeout=2)
    slots.release()


def test_annual_alignment_checks_original_column_and_exact_date():
    from decimal import Decimal
    html = ('<table><tr><th>Metric</th><th colspan="2">Years Ended</th></tr>'
            '<tr><th></th><th>July 27, 2024</th><th>July 29, 2023</th></tr>'
            '<tr><td>Revenue</td><td>100</td><td>90</td></tr></table>')
    source = source_for(html)
    data = {'status': 'available', **trace(source)}
    matched = annual_table_value_alignment(data, Decimal('100'), 'Fiscal year ended July 27, 2024')
    assert matched['status'] == 'matched' and matched['column_date'] == '2024-07-27'
    assert annual_table_value_alignment(data, Decimal('100'), 'Fiscal year ended July 29, 2023')['status'] == 'conflict'
    assert annual_table_value_alignment(data, Decimal('100'), 'Fiscal year ended July 28, 2024')['status'] == 'conflict'
    assert annual_table_value_alignment(data, Decimal('100'), 'FY2024') is None
    assert annual_table_value_alignment(data, Decimal('90'), 'fiscal year ended July 29, 2023')['status'] == 'matched'
    duplicate = source_for(html.replace('<td>90</td>', '<td>100</td>'))
    assert annual_table_value_alignment({'status': 'available', **trace(duplicate)}, Decimal('100'), 'Fiscal year ended July 27, 2024') is None


def test_analysis_keeps_structural_calculation_provisional_and_fails_closed():
    from app.research_analysis import analyze
    html = ('<p>In millions</p><table><tr><th colspan="2">Years Ended</th>'
            '<th colspan="2">July 27, 2024</th><th colspan="2">July 29, 2023</th></tr>'
            '<tr><td colspan="2">Revenue</td><td colspan="2">100</td><td colspan="2">90</td></tr></table>')
    source = source_for(html)
    def fact(key, value, period):
        return {'fact_id': key, 'label': 'Revenue', 'value': value, 'unit': 'USD million', 'currency': 'USD',
                'period': period, 'citation': {'source_id': 's', 'quotation': 'Revenue | 100 | 90'}}
    facts = [fact('a', '100', 'Fiscal year ended July 27, 2024'), fact('b', '90', 'Fiscal year ended July 29, 2023')]
    def run(material, facts=facts):
        return analyze('Compare revenue.', [material], None, lambda _: ({'facts': facts,
            'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'a', 'second_fact_id': 'b'}]}, 1))
    # Source permission is separate from the user request and must allow the
    # model to see the material; original bytes stay in the source repository.
    source.permissions.model_input = True
    result = run(source)
    assert len(result.facts) == 2
    assert all(f['table_structure_alignment']['status'] == 'matched' for f in result.facts)
    assert all(f['table_alignment_unverified'] for f in result.facts)
    assert result.calculations[0]['value'] == '11.11'
    assert result.calculations[0]['input_status'] == 'unverifiable'
    assert any('provisional' in note for note in result.limitations)
    assert not run(source.model_copy(update={'original_html_base64': None})).calculations
    wrong = [facts[0], {**facts[1], 'period': 'Fiscal year ended July 28, 2023'}]
    mismatch = run(source, wrong)
    assert not mismatch.calculations
    assert mismatch.facts[1]['table_structure_alignment']['status'] == 'conflict'
    restricted = source.model_copy(deep=True)
    restricted.permissions.display = False
    assert not run(restricted).calculations
