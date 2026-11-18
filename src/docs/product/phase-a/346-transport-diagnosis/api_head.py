"""Single unauthenticated HEAD to API route through production transport; no model call."""
import json, socket, time
from http.client import HTTPResponse
from app import openai_compatible as t
url=t._GLM_CODING_BASE_URL.rstrip('/')+'/chat/completions'
trace=[]
def instrument(cls, name, label, extra=lambda self: {}):
    original=getattr(cls,name)
    def wrapped(self,*args,**kwargs):
        start=time.monotonic()
        try:
            result=original(self,*args,**kwargs)
            trace.append({'stage':label,'elapsed_ms':round((time.monotonic()-start)*1000),**extra(self)})
            return result
        except Exception as e:
            trace.append({'stage':label,'elapsed_ms':round((time.monotonic()-start)*1000),'exception':type(e).__name__,'errno':getattr(e,'errno',None),**extra(self)})
            raise
    setattr(cls,name,wrapped)
instrument(t._PinnedHTTPSConnection,'connect','tcp_and_tls',lambda s:{'address':s._validated_address,'timeout_seconds':s.timeout})
instrument(t._PinnedHTTPSConnection,'request','send_head')
instrument(t._PinnedHTTPSConnection,'getresponse','response_headers')
instrument(HTTPResponse,'read','read_response_body')
original=socket.create_connection
def tcp(*args,**kwargs):
    start=time.monotonic()
    try:
        sock=original(*args,**kwargs)
        trace.append({'stage':'tcp','elapsed_ms':round((time.monotonic()-start)*1000),'address':args[0][0]})
        return sock
    except Exception as e:
        trace.append({'stage':'tcp','elapsed_ms':round((time.monotonic()-start)*1000),'address':args[0][0],'exception':type(e).__name__,'errno':getattr(e,'errno',None)})
        raise
socket.create_connection=tcp
start=time.monotonic()
out={'request':'HEAD '+url,'authentication_sent':False,'model_calls':0}
try:
    result=t._default_binary_transport('HEAD',url,{},b'',20)
    out['http_status']=result.status_code
except Exception as e:
    out['exception']=type(e).__name__; out['errno']=getattr(e,'errno',None)
out['elapsed_ms']=round((time.monotonic()-start)*1000); out['trace']=trace
print(json.dumps(out,indent=2))
