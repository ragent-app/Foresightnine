"""Serve isolated fixtures and current built frontend on one OS-selected loopback port."""
import socket
from pathlib import Path
import uvicorn
from fastapi.responses import FileResponse
from serve_fixture import app
root=Path('/tmp/foresight-repair-local').resolve()
@app.middleware('http')
async def static_csp(request, call_next):
    response=await call_next(request)
    if not request.url.path.startswith(('/v1/', '/v2/')):
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
    return response
@app.get('/{path:path}')
def static(path: str):
    target=(root/path).resolve()
    if root not in target.parents or not target.is_file(): target=root/'index.html'
    return FileResponse(target)
sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',0));sock.listen(128)
print(sock.getsockname()[1],flush=True)
config=uvicorn.Config(app,log_level='warning');uvicorn.Server(config).run(sockets=[sock])
