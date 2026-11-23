"""Capture actual offline search and trace; deliberately never reads answer annotations."""
import json,sys,hashlib
from pathlib import Path
from datetime import datetime,timezone
from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest,run_search_library,sync_library
from app.research_sources import ResearchSource,SourcePermissions
from app.research_delivery import assess
here=Path(__file__).resolve().parent
cases=json.loads((here/'inputs.json').read_text()); rows=[]
for case in cases:
 clock=datetime.fromisoformat('2026-03-31T23:59:59+00:00' if case['id'].startswith('S') else '2026-11-22T00:00:00+00:00')
 sources={}
 for sid,title,text in case['sources']:
  meta=case.get('source_meta',{}).get(sid,{})
  sources[sid]=ResearchSource(source_id=sid,owner_id='offline-acceptance',title=title,kind='user_supplied',text=text,obtained_at=datetime(2026,3,1,tzinfo=timezone.utc),published_at=datetime.fromisoformat(meta.get('published_at',clock.isoformat())),content_sha256=hashlib.sha256(text.encode()).hexdigest(),permissions=SourcePermissions(store=True,model_input=meta.get('model_input',True),display=True,export=True))
 lib=ResearchLibrary([],vector_scorer=None);sync_library(lib,sources,{},clock,vector_scorer=None)
 requirements=([{'id':f'item-{i}','question':x,'status':'missing','core':True} for i,x in enumerate(case['requirements'])] if 'requirements' in case else assess(case['question'],{},sources,as_of=clock))
 
 if '--repeat-query' in sys.argv:
  original_search=lib.search
  def repeat(query, **kwargs): return original_search(case['question'], **kwargs)
  lib.search=repeat
 result,_=run_search_library(SearchLibraryRequest(query=case['question'],max_hits=case.get('passages',2)),library=lib,source_by_id=sources,versions={},as_of=clock,requirements=requirements,selection_mode='anchored',selection_max_bytes=512)
 rows.append({'id':case['id'],'requirements':requirements,'result':result})
Path(sys.argv[1]).write_text(json.dumps(rows,ensure_ascii=False,indent=2,default=str)+'\n')
