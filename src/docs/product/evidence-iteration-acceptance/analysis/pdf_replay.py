"""PDF bytes to actual statement extraction, isolated, offline and reproducible."""
import base64,hashlib,json
from pathlib import Path
from datetime import datetime,timezone
from time import monotonic
from app.research_documents import extract_pdf_pages
from app.research_sources import ResearchSource,SourcePermissions
from app.research_statement_extraction import ExtractStatementsRequest,extract_statements
from app.research_tool_executor import _read_pages
from app.research_tools_registry import ReadPagesRequest
here=Path(__file__).resolve().parent
root=here.parents[3]
paths={'uk':here/'uk-dma-2025-position.pdf','synthetic':root/'docs/product/context-evidence-acceptance/evidence/cross-page-statement.pdf'}
results={}
for name,path in paths.items():
 raw=path.read_bytes();doc=extract_pdf_pages(path,deadline=monotonic()+30);h=hashlib.sha256(raw).hexdigest()
 source=ResearchSource(source_id='sample',owner_id='fixture',title=name,kind='user_supplied',obtained_at=datetime(2026,4,1,tzinfo=timezone.utc),retrieval_method='user_pdf',original_pdf_base64=base64.b64encode(raw).decode(),response_sha256=h,text=doc.text,content_sha256=hashlib.sha256(doc.text.encode()).hexdigest(),provenance={'document_pages':doc.document_pages},permissions=SourcePermissions(store=True,model_input=True,display=True,export=True))
 reads=[]
 def extract():return extract_statements(ExtractStatementsRequest(source_id='sample',statements=['balance'],rationale='Check headers and rows'),source,document_pages=doc.document_pages,read_ranges=reads,read_budget_chars=40000)[0]
 first=extract();table=first['statements'][0]; selected=[r for r in table['rows'] if r.get('standard_line') in ('total_assets','cash_and_equivalents') or r['label'].startswith('Cash and balances')]
 if name=='uk':
  assert [c.get('period_end') for c in table['columns']]==['2025-03-31','2024-03-31']
  assert table['unit']['currency']=='GBP' and table['unit']['scale']==1000000
  assert next(r for r in selected if r.get('standard_line')=='total_assets')['value_as_stated']==['273984','279345']
  assert next(r for r in selected if r['label'].startswith('Cash and balances'))['value_as_stated']==['3231','2619']
  assert all(not r.get('note_reference') for r in table['rows'])
 else:
  assert [c.get('period_end') for c in table['columns']]==['2025-12-31','2024-12-31']
  assert table.get('header_geometry',{}).get('status')=='geometric_alignment_unverified'
  cash=next(r for r in selected if r.get('standard_line')=='cash_and_equivalents'); assert 'referenced_note_not_bound' in cash['warnings']
  _read_pages(ReadPagesRequest(source_id='sample',first_page=3,last_page=3,rationale='Read note'),source_by_id={'sample':source},as_of=None,read_ranges=reads)
  second=extract();cash=next(r for r in second['statements'][0]['rows'] if r.get('standard_line')=='cash_and_equivalents')
  assert cash.get('note_reference',{}).get('status')=='located_unverified'
  results['note_after_read']=cash['note_reference']
  # A second same-number note, even on a different page, makes the
  # reference ambiguous and must not be bound.
  duplicate_text=source.text+'\n五、1 重号附注内容。'
  pages=[list(x) for x in doc.document_pages['pages']]; pages[-1][1]=len(duplicate_text)
  dup=source.model_copy(update={'text':duplicate_text,'content_sha256':hashlib.sha256(duplicate_text.encode()).hexdigest(),
                                'provenance':{'document_pages':{**doc.document_pages,'pages':pages}}})
  dup_reads=[{'source_id':'sample','start':0,'end':len(duplicate_text)}]
  dup_result,_=extract_statements(ExtractStatementsRequest(source_id='sample',statements=['balance'],rationale='Check duplicate note'),dup,document_pages=dup.provenance['document_pages'],read_ranges=dup_reads,read_budget_chars=40000)
  dup_cash=next(r for r in dup_result['statements'][0]['rows'] if r.get('standard_line')=='cash_and_equivalents')
  assert not dup_cash.get('note_reference') and 'referenced_note_not_bound' in dup_cash['warnings']
  results['duplicate_note_withheld']=True
  # A reversed continuation header must not reuse the first page's columns.
  second_page_start=doc.document_pages['pages'][1][0]; tail=source.text[second_page_start:]
  assert '期末余额' in tail and '上年年末余额' in tail
  tail=tail.replace('期末余额','@@TEMP@@',1).replace('上年年末余额','期末余额',1).replace('@@TEMP@@','上年年末余额',1)
  assert len(tail)==len(source.text[second_page_start:])
  reverse_text=source.text[:second_page_start]+tail
  reverse_source=source.model_copy(update={'text':reverse_text,'content_sha256':hashlib.sha256(reverse_text.encode()).hexdigest()})
  reverse,_=extract_statements(ExtractStatementsRequest(source_id='sample',statements=['balance'],rationale='Check continuation'),reverse_source,document_pages=doc.document_pages,read_ranges=[],read_budget_chars=40000)
  rev_table=reverse['statements'][0]
  assert 'continuation_column_header_conflict' in rev_table['warnings']
  assert all(all(v is None for v in r['value']) for r in rev_table['rows'] if r['page']==2)
  results['reversed_continuation_values_withheld']=True
  from dataclasses import replace
  from app.research_pdf_geometry import extract_geometry,split_header_date
  geo=extract_geometry(source); assert geo and split_header_date(source.text,geo,1,0,153)
  year=next(line for line in geo.lines if source.text[line.start:line.end]=='2025')
  duplicate_geo=replace(geo,lines=geo.lines+(year,))
  assert split_header_date(source.text,duplicate_geo,1,0,153) is None
  results['ambiguous_geometry_withheld']=True
 results[name]={'pdf_sha256':h,'text_sha256':source.content_sha256,'columns':table['columns'],'unit':table['unit'],'warnings':table.get('warnings',[]),'geometry':table.get('header_geometry'),'rows':selected,'read_spans':first['read_spans']}
(here/'pdf-replay-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
