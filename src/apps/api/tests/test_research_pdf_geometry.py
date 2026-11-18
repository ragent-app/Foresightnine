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
