"""One ordinary hostname TLS comparison and one unauthenticated HEAD via production transport."""
import json, socket, time
from http.client import HTTPSConnection
from urllib.parse import urlsplit
from app import openai_compatible as t
host = urlsplit(t._GLM_CODING_BASE_URL).hostname
out = {'host':host, 'model_calls':0, 'authentication_sent':False, 'request':'HEAD /'}
def run(fn):
    start=time.monotonic()
    try:
        v=fn(); return {'elapsed_ms':round((time.monotonic()-start)*1000),'result':v}
    except Exception as e:
        return {'elapsed_ms':round((time.monotonic()-start)*1000),'exception':type(e).__name__,'errno':getattr(e,'errno',None),'cause':type(e.__cause__).__name__ if e.__cause__ else None}
c=HTTPSConnection(host,443,timeout=5)
def ordinary():
    c.connect(); return {'tls_version':c.sock.version(), 'peer_ip':c.sock.getpeername()[0]}
out['ordinary_hostname_tls']=run(ordinary); c.close()
trace=[]
original=t._PinnedHTTPSConnection.connect
def traced(self):
    start=time.monotonic()
    try:
        original(self); trace.append({'address':self._validated_address,'elapsed_ms':round((time.monotonic()-start)*1000),'result':'connected'})
    except Exception as e:
        trace.append({'address':self._validated_address,'elapsed_ms':round((time.monotonic()-start)*1000),'exception':type(e).__name__,'errno':getattr(e,'errno',None)}); raise
t._PinnedHTTPSConnection.connect=traced
def head():
    r=t._default_binary_transport('HEAD', f'https://{host}/', {}, b'', 20)
    return {'http_status':r.status_code}
out['production_transport_head']=run(head); out['pinned_trace']=trace
print(json.dumps(out,indent=2))
