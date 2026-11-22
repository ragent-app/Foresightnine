from hashlib import sha256

import pytest

from app.research_pdf_geometry import GeometryLine, complete_sentence_line, map_geometry


def xml_line(words):
    content = ''.join(f'<word xMin="{x}" yMin="1" xMax="{x + width}" yMax="9">{text}</word>'
                      for text, x, width in words)
    return f'<line xMin="0" yMin="1" xMax="200" yMax="9">{content}</line>'


def document(*lines):
    return ('<doc><page width="600" height="800"><flow><block>' + ''.join(lines)
            + '</block></flow></page></doc>').encode()


def test_exact_mapping_preserves_text_offsets_and_hashes():
    text = 'Heading\nRevenue  rose.\n'
    geometry = map_geometry(document(xml_line([('Revenue', 0, 40), ('rose.', 45, 24)])), text, 'pdf-hash')
    assert geometry.pdf_sha256 == 'pdf-hash'
    assert geometry.text_sha256 == sha256(text.encode()).hexdigest()
    line, = geometry.lines
    assert text[line.start:line.end] == 'Revenue  rose.'
    assert line.page == 1


def test_ambiguous_and_wide_column_lines_are_not_mapped():
    line = xml_line([('Revenue', 0, 40), ('rose.', 45, 24)])
    columns = xml_line([('2024', 0, 20), ('100', 100, 20)])
    geometry = map_geometry(document(line, columns), 'Revenue rose.\nRevenue rose.\n2024  100\n', 'hash')
    assert not geometry.lines
    assert geometry.unmatched_lines == 2


def test_entity_declarations_rejected():
    with pytest.raises(ValueError):
        map_geometry(b'<!DOCTYPE doc [<!ENTITY x "x">]><doc/>', 'x', 'hash')


def test_sentence_tail_after_wrapped_negation_is_not_a_complete_unit():
    text = 'The committee did not\nraise the rate.\n'
    start = text.index('raise')
    assert not complete_sentence_line(text, GeometryLine(1, start, start + len('raise the rate.'), (0, 0, 1, 1)))
    whole = 'The committee did not raise the rate.'
    assert complete_sentence_line(whole, GeometryLine(1, 0, len(whole), (0, 0, 1, 1)))


def test_geometry_candidate_reaches_analysis_with_original_offsets(monkeypatch):
    from datetime import datetime, timezone
    import app.research_pdf_geometry as geometry_module
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = 'Heading    unknown\n\nThe committee   did not raise rates.\n'
    start = text.index('The committee')
    line = GeometryLine(1, start, len(text.rstrip('\n')), (0, 0, 100, 10))
    sidecar = geometry_module.PdfGeometry('test_geometry', 'pdf', sha256(text.encode()).hexdigest(), (line,), 0)
    monkeypatch.setattr(geometry_module, 'extract_geometry', lambda source, should_stop: sidecar)
    source = ResearchSource(source_id='test', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        retrieval_method='user_pdf', permissions=SourcePermissions(store=True, model_input=True, display=True))
    prompts = []
    result = analyze('Did the committee raise rates?', [source], None,
                     lambda prompt: (prompts.append(prompt) or {}, 1), max_prompt_chars=6000)
    assert prompts, result.limitations
    selected = [item for item in result.segments if item.get('pdf_geometry')]
    assert selected
    assert text[selected[0]['character_start']:selected[0]['character_end']] == text[line.start:line.end]
    assert selected[0]['pdf_geometry']['alignment'] == 'exact_unique'


def test_wrapped_sentence_preserves_negation_and_original_range():
    from app.research_pdf_geometry import PdfGeometry, geometry_evidence_groups
    text = 'The board did not\nraise the rate.\n'
    first = GeometryLine(1, 0, text.index('\n'), (20, 10, 120, 20))
    start = text.index('raise')
    second = GeometryLine(1, start, start + len('raise the rate.'), (20, 22, 105, 32))
    geometry = PdfGeometry('test', 'pdf', sha256(text.encode()).hexdigest(), (first, second), 0)
    groups = geometry_evidence_groups(text, geometry)
    assert len(groups) == 1 and groups[0].kind == 'sentence'
    a, b = groups[0].parts[0]
    assert text[a:b] == 'The board did not\nraise the rate.'
    assert not geometry_evidence_groups(text + ' changed', geometry)


def test_wrapped_sentence_rejects_cross_column_and_cross_page():
    from app.research_pdf_geometry import PdfGeometry, geometry_evidence_groups
    text = 'The board did not\nraise the rate.\n'
    a = GeometryLine(1, 0, text.index('\n'), (20, 10, 120, 20))
    start = text.index('raise')
    for b in (GeometryLine(1, start, start + len('raise the rate.'), (220, 22, 300, 32)),
              GeometryLine(2, start, start + len('raise the rate.'), (20, 22, 100, 32))):
        assert not geometry_evidence_groups(text, PdfGeometry('test', 'pdf', sha256(text.encode()).hexdigest(), (a, b), 0))


def test_geometry_table_requires_matching_header_and_rows_without_joining_quotations():
    from app.research_pdf_geometry import GeometryTableRow, PdfGeometry, geometry_evidence_groups
    text = 'Metric       2022       2023\nRevenue       10         12\n'
    end = text.index('\n'); start = end + 1
    header = GeometryTableRow(1, 0, end, (('Metric', 0, 30), ('2022', 100, 120), ('2023', 200, 220)), (0, 10, 220, 20))
    row = GeometryTableRow(1, start, len(text.rstrip('\n')), (('Revenue', 0, 35), ('10', 100, 110), ('12', 200, 210)), (0, 22, 210, 32))
    geometry = PdfGeometry('test', 'pdf', sha256(text.encode()).hexdigest(), (), 0, (header, row))
    group, = geometry_evidence_groups(text, geometry)
    assert group.kind == 'table' and tuple(text[a:b] for a, b in group.parts) == ('Metric       2022       2023', 'Revenue       10         12')
    bad = GeometryTableRow(1, row.start, row.end, (('Revenue', 0, 35), ('10', 140, 150), ('12', 200, 210)), row.bounds)
    assert not geometry_evidence_groups(text, PdfGeometry('test', 'pdf', geometry.text_sha256, (), 0, (header, bad)))


def test_poppler_table_rows_map_exact_offsets_before_grouping():
    from app.research_pdf_geometry import geometry_evidence_groups
    def row(y, values):
        words = ''.join(f'<word xMin="{x}" yMin="{y}" xMax="{x+20}" yMax="{y+8}">{token}</word>' for token, x in values)
        return f'<line xMin="0" yMin="{y}" xMax="220" yMax="{y+8}">{words}</line>'
    raw = ('<doc><page>' + row(10, [('Metric', 0), ('2022', 100), ('2023', 200)])
           + row(20, [('Revenue', 0), ('10', 100), ('12', 200)]) + '</page></doc>').encode()
    text = 'Metric       2022       2023\nRevenue       10         12\n'
    result = map_geometry(raw, text, 'pdf')
    group, = geometry_evidence_groups(text, result)
    assert tuple(text[a:b] for a,b in group.parts) == ('Metric       2022       2023', 'Revenue       10         12')


def test_geometry_groups_keep_standalone_complete_sentence():
    from app.research_pdf_geometry import PdfGeometry, geometry_evidence_groups
    text = 'The committee did not increase the rate.'
    line = GeometryLine(1, 0, len(text), (0, 0, 120, 10))
    geometry = PdfGeometry('test', 'pdf', sha256(text.encode()).hexdigest(), (line,), 0)
    group, = geometry_evidence_groups(text, geometry)
    assert group.kind == 'sentence' and text[group.parts[0][0]:group.parts[0][1]] == text


def test_pdf_diagnostic_reports_mapping_and_unknown_without_claiming_completeness():
    from app.research_pdf_geometry import PdfGeometry, pdf_structure_diagnostic
    unavailable = pdf_structure_diagnostic('s', None, [])
    assert unavailable['geometry_status'] == 'unavailable'
    assert unavailable['group_count'] is None and unavailable['unmatched_line_count'] is None
    geometry = PdfGeometry('test', 'pdf', 'text', (GeometryLine(1, 0, 2, (0, 0, 1, 1)),), 2)
    result = pdf_structure_diagnostic('s', geometry, [])
    assert result['geometry_status'] == 'mapped' and result['unmatched_line_count'] == 2
    assert result['group_count'] == 0 and result['reading_order_status'] == 'not_independently_verified'
    limited = PdfGeometry('test', 'pdf', 'text', geometry.lines * 1201, 0)
    assert pdf_structure_diagnostic('s', limited, [])['geometry_status'] == 'limit_reached'
    assert pdf_structure_diagnostic('s', limited, [])['group_count'] is None


def test_extract_geometry_supports_web_pdf_blob_store_and_page_bounds(tmp_path, monkeypatch):
    import shutil
    from datetime import datetime, timezone
    from time import monotonic
    from app.research_blob_store import DocumentBlobStore
    from app.research_documents import extract_pdf_pages
    from app.research_pdf_geometry import extract_geometry
    from app.research_sources import ResearchSource, SourcePermissions
    from tests.test_research_large_documents import build_pdf

    if not (shutil.which('pdftotext') and shutil.which('prlimit') and shutil.which('pdfinfo')):
        pytest.skip('poppler-utils and prlimit are required')

    raw = build_pdf([
        'Page one revenue rose steadily.',
        'Page two gross margin reached 64 percent.',
        'Page three operating cash flow grew.',
    ])
    store = DocumentBlobStore(tmp_path / 'blobs')
    digest = store.put_bytes(raw)
    pdf_path = tmp_path / 'source.pdf'
    pdf_path.write_bytes(raw)
    extracted = extract_pdf_pages(pdf_path, deadline=monotonic() + 30)
    doc_pages = {**extracted.document_pages, 'original_retained': True, 'original_blob_sha256': digest}
    source = ResearchSource(
        source_id='src-web-pdf',
        owner_id='owner',
        title='Annual Report',
        text=extracted.text,
        kind='user_supplied',
        obtained_at=datetime.now(timezone.utc),
        content_sha256=sha256(extracted.text.encode('utf-8')).hexdigest(),
        retrieval_method='web_pdf',
        response_sha256=digest,
        original_pdf_base64=None,
        permissions=SourcePermissions(store=True, model_input=True, display=True),
        provenance={'document_pages': doc_pages},
    )

    bounded = extract_geometry(source, first_page=2, last_page=3, blob_store=store)
    assert bounded is not None
    assert bounded.pdf_sha256 == digest
    assert {line.page for line in bounded.lines} == {2, 3}
    mapped_texts = [source.text[line.start:line.end] for line in bounded.lines]
    assert any('gross margin reached 64 percent' in item for item in mapped_texts)
    assert any('operating cash flow grew' in item for item in mapped_texts)
    assert not any('Page one' in item for item in mapped_texts)

    # Configured env blob store fallback also works when blob_store argument is omitted.
    monkeypatch.setenv('FORESIGHTNINE_DOCUMENT_STORE_DIR', str(tmp_path / 'blobs'))
    via_env = extract_geometry(source, first_page=2, last_page=2)
    assert via_env is not None and [line.page for line in via_env.lines] == [2]

    # Guardrails and fail-closed checks.
    assert extract_geometry(source, first_page=3, last_page=2, blob_store=store) is None
    assert extract_geometry(source.model_copy(update={'response_sha256': '0' * 64}), blob_store=store) is None
    assert extract_geometry(source.model_copy(update={'content_sha256': '0' * 64}), blob_store=store) is None
    assert extract_geometry(source.model_copy(update={'permissions': SourcePermissions(store=True, model_input=False)}), blob_store=store) is None
    assert extract_geometry(source.model_copy(update={'retrieval_method': 'web_html'}), blob_store=store) is None


def test_extract_geometry_invokes_prlimit_and_page_flags(tmp_path, monkeypatch):
    import base64
    from datetime import datetime, timezone
    import app.research_pdf_geometry as geometry_module
    from app.research_sources import ResearchSource, SourcePermissions
    from tests.test_research_large_documents import build_pdf

    raw = build_pdf(['Alpha line.', 'Beta line.'])
    digest = sha256(raw).hexdigest()
    text = 'Alpha line.\fBeta line.\f'
    source = ResearchSource(
        source_id='src-inline-web-pdf',
        owner_id='owner',
        title='Brief',
        text=text,
        kind='user_supplied',
        obtained_at=datetime.now(timezone.utc),
        content_sha256=sha256(text.encode('utf-8')).hexdigest(),
        retrieval_method='web_pdf',
        response_sha256=digest,
        original_pdf_base64=base64.b64encode(raw).decode('ascii'),
        permissions=SourcePermissions(store=True, model_input=True, display=True),
    )

    seen_cmd = []

    class FakePopen:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            seen_cmd.extend(cmd)
            out_path = cmd[-1]
            with open(out_path, 'wb') as fh:
                fh.write(b'<doc><page><line xMin="0" yMin="1" xMax="100" yMax="9"><word xMin="0" yMin="1" xMax="30" yMax="9">Beta</word><word xMin="35" yMin="1" xMax="60" yMax="9">line.</word></line></page></doc>')

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(geometry_module.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(geometry_module.subprocess, 'Popen', FakePopen)

    result = geometry_module.extract_geometry(source, first_page=2, last_page=2)
    assert result is not None
    assert '--as=536870912' in seen_cmd and '--cpu=3' in seen_cmd
    assert '-f' in seen_cmd and seen_cmd[seen_cmd.index('-f') + 1] == '2'
    assert '-l' in seen_cmd and seen_cmd[seen_cmd.index('-l') + 1] == '2'
    assert len(result.lines) == 1
    assert result.lines[0].page == 2
    assert text[result.lines[0].start:result.lines[0].end] == 'Beta line.'
