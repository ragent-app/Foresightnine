"""One non-model diagnostic round: resolver and pinned TCP/TLS only; no HTTP to provider."""
import json, os, socket, time
from urllib.parse import urlsplit
from app import openai_compatible as transport
host = urlsplit(transport._GLM_CODING_BASE_URL).hostname
port = 443
out = {'host': host, 'port': port, 'provider_http_sent': False, 'model_calls': 0,
       'proxy_variables_present': {k: bool(os.environ.get(k)) for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy','all_proxy','no_proxy')}}
def run(label, fn):
    start = time.monotonic()
    try:
        value = fn()
        return {'stage': label, 'elapsed_ms': round((time.monotonic()-start)*1000), 'result': value}
    except Exception as e:
        return {'stage': label, 'elapsed_ms': round((time.monotonic()-start)*1000), 'exception': type(e).__name__, 'errno': getattr(e, 'errno', None), 'cause': type(e.__cause__).__name__ if e.__cause__ else None, 'cause_errno': getattr(e.__cause__, 'errno', None)}
out['standard_dns'] = run('standard_dns', lambda: sorted({r[4][0] for r in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)}))
out['validated_dns'] = run('actual_validated_resolver', lambda: list(transport._validated_addresses(host,port)))
out['connections'] = []
for address in out['validated_dns'].get('result', []):
    record = {'address': address, 'http_sent': False}
    original = socket.create_connection
    def traced(*args, **kwargs):
        start = time.monotonic()
        try:
            sock = original(*args, **kwargs)
            record['tcp'] = {'stage': 'tcp', 'elapsed_ms': round((time.monotonic()-start)*1000)}
            return sock
        except Exception as e:
            record['tcp'] = {'stage': 'tcp', 'elapsed_ms': round((time.monotonic()-start)*1000), 'exception': type(e).__name__, 'errno': getattr(e, 'errno', None)}
            raise
    socket.create_connection = traced
    connection = transport._PinnedHTTPSConnection(host, port, address, 5.0)
    try:
        stage = run('pinned_connect_tcp_and_tls', connection.connect)
        record['pinned_connect'] = stage
        if 'result' in stage:
            record['tls_version'] = connection.sock.version()
    finally:
        connection.close(); socket.create_connection = original
    out['connections'].append(record)
print(json.dumps(out, indent=2))
