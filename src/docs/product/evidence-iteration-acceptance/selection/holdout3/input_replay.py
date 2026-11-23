"""Fixed offline coordinator: read selected ranges, construct and serialize next native turn."""
import hashlib,json,sys
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace
from app.research_sources import ResearchSource,SourcePermissions
from app.research_tool_executor import _read_sources
from app.research_tools_registry import ReadRequest
from app.research_context_manager import build_model_turn_input
from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import canonical_json_dumps
here=Path(__file__).resolve().parent
cases=json.loads((here/'inputs.json').read_text()); traces={x['id']:x for x in json.loads(Path(sys.argv[1]).read_text())}; rows=[]
settings=SimpleNamespace(base_url='https://example.com',model='offline-fixture',api_key='unused',allow_private_network=False)
provider=AnthropicMessagesProvider(settings,SimpleNamespace(timeout_seconds=10,max_input_tokens=None,max_output_tokens=256),anthropic_version='2023-06-01',auth_scheme='bearer',max_tokens=256)
for case in cases:
 clock=datetime.fromisoformat('2026-03-31T23:59:59+00:00' if case['id'].startswith('S') else '2026-11-22T00:00:00+00:00'); sources={}
 for sid,title,text in case['sources']:
  m=case.get('source_meta',{}).get(sid,{})
  sources[sid]=ResearchSource(source_id=sid,owner_id='offline-acceptance',title=title,kind='user_supplied',text=text,obtained_at=datetime(2026,3,1,tzinfo=timezone.utc),published_at=datetime.fromisoformat(m.get('published_at',clock.isoformat())),content_sha256=hashlib.sha256(text.encode()).hexdigest(),permissions=SourcePermissions(store=True,model_input=m.get('model_input',True),display=True,export=True))
 search=traces[case['id']]['result']; reads=[]; size=0
 for hit in search['hits']:
  loc=hit['read_sources']; src=sources[loc['source_id']]; n=len(src.text[loc['start']:loc['end']].encode())+len((hit.get('table_header') or '').encode())
  if size+n<=512: reads.append(loc); size+=n
 ranges=[]
 result,_=_read_sources(ReadRequest(reads=reads,rationale='Read selected evidence within the fixed 512-byte comparison capacity.'),source_by_id=sources,as_of=clock,read_ranges=ranges) if reads else ({'results':[]},None)
 inventory=[{'source_id':s.source_id,'content_sha256':s.content_sha256,'eligibility':'eligible'} for s in sources.values() if s.permissions.model_input and s.published_at<=clock]
 def exchange(name,call_id,args,data):
  return {'assistant':{'role':'assistant','content':[{'type':'tool_use','id':call_id,'name':name,'input':args}]},'results':[{'tool_call_id':call_id,'content':json.dumps(data,ensure_ascii=False),'is_error':False}]}
 state={'user':json.dumps({'question':case['question'],'research_as_of':clock.isoformat(),'sources':inventory},ensure_ascii=False),'history':[exchange('search_library','search',{'query':case['question'],'max_hits':case.get('passages',2)},search),exchange('read_sources','read',{'reads':reads,'rationale':'Read selected evidence.'},result)],'read_ranges':ranges}
 turn=build_model_turn_input(state=state,source_by_id=sources); wire=provider._wire(turn,256); body=canonical_json_dumps(wire).encode(); wiretext=body.decode()
 # Match the actual serialized tool_result for the read call, not any search snippet.
 matches=[]
 for msg in wire.get('messages',[]):
  for block in msg.get('content',[]) if isinstance(msg.get('content'),list) else []:
   if block.get('type')=='tool_result' and block.get('tool_use_id')=='read':
    content=block.get('content',''); data=json.loads(content) if isinstance(content,str) else {}; matches=data.get('results',[])
 observed=[{'source_id':r['source_id'],'start':r['start'],'end':r['end'],'content_sha256':r['content_sha256']} for r in matches if r.get('status')=='read' and r.get('text')==sources[r['source_id']].text[r['start']:r['end']]]
 rows.append({'id':case['id'],'requested_reads':reads,'read_ranges':ranges,'observed_in_serialized_read_result':observed,'wire_sha256':hashlib.sha256(body).hexdigest(),'wire_bytes':len(body),'actual_provider_response':'not_called','reopened_delivery':'not_observed'})
Path(sys.argv[2]).write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
