"""Trace actual PDF bytes through the extractor and the native page reader."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from app.research_documents import extract_pdf_pages
from app.research_sources import ResearchSource, SourcePermissions
from app.research_statement_extraction import ExtractStatementsRequest, extract_statements
from app.research_tool_executor import _read_pages
from app.research_tools_registry import ReadPagesRequest

path = Path(__file__).with_name('cross-page-statement.pdf')
doc = extract_pdf_pages(path, deadline=monotonic() + 30)
source = ResearchSource(source_id='sample', owner_id='sample', title='开发样本', kind='user_supplied',
                        obtained_at=datetime(2026, 4, 1, tzinfo=timezone.utc), retrieval_method='user_pdf',
                        text=doc.text, content_sha256=hashlib.sha256(doc.text.encode()).hexdigest(),
                        provenance={'document_pages': doc.document_pages},
                        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
reads = []
result, _ = extract_statements(ExtractStatementsRequest(source_id='sample', statements=['balance'], rationale='核对跨页表头'),
                               source, document_pages=doc.document_pages, read_ranges=reads, read_budget_chars=40000)
table = result['statements'][0]
row = next(r for r in table['rows'] if r.get('note_ref') == '五、1')
assert table['pages'] == [1,2] and 'referenced_note_not_bound' in row['warnings']
assert result['status'] == 'partial' and 'period_unresolved' in table['warnings']
assert all(c.get('period_end') is None for c in table['columns'])
original_reads = list(reads)
note, _ = _read_pages(ReadPagesRequest(source_id='sample',first_page=3,last_page=3,rationale='定位附注正文'),
                      source_by_id={'sample':source}, as_of=None, read_ranges=reads)
assert '本期现金及银行存款' in json.dumps(note, ensure_ascii=False)
print(json.dumps({'pdf_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'text_sha256':source.content_sha256,
                  'pages':table['pages'], 'columns':table['columns'], 'table_status':result.get('status'),
                  'unit':table.get('unit'), 'warnings':table.get('warnings'), 'referenced_note_warning':row['warnings'],
                  'before_note':original_reads,'after_note':reads},ensure_ascii=False,indent=2))
