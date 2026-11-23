"""Render all frozen replay fixtures through Vite and loopback FastAPI, no interception."""
import asyncio, json, pathlib, subprocess, tempfile, urllib.request, importlib.util, os, socket
import websockets
HERE=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('cdp',HERE.parent/'browser_delivery_cdp.py'); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
BASE='http://127.0.0.1:'+os.environ['REPAIR_PORT']
sock=socket.socket();sock.bind(('127.0.0.1',0)); CHROME_PORT=sock.getsockname()[1];sock.close()
async def main():
 with tempfile.TemporaryDirectory(prefix='fn-repair-', ignore_cleanup_errors=True) as profile:
  p=subprocess.Popen(['/usr/bin/google-chrome','--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--disable-background-networking','--renderer-process-limit=1',f'--remote-debugging-port={CHROME_PORT}','--remote-allow-origins=*',f'--user-data-dir={profile}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:
   tabs=await asyncio.to_thread(m.poll_json,f'http://127.0.0.1:{CHROME_PORT}/json')
   async with websockets.connect(tabs[0]['webSocketDebuggerUrl'],max_size=10000000) as ws:
    c=m.CDP(ws); await c.start()
    for method in ('Page.enable','Runtime.enable','Log.enable'): await c.call(method)
    await c.call('Page.navigate',{'url':BASE+'/'})
    out=[]
    raw_dir=os.environ.get('REPAIR_RAW_RESULTS_DIR')
    rows=([{'run':p.stem} for p in sorted(pathlib.Path(raw_dir).glob('*.json'))] if raw_dir else json.loads((HERE/'frozen-baseline.json').read_text())['rows'])
    output=pathlib.Path(os.environ.get('REPAIR_BROWSER_OUTPUT', str(HERE/'browser-results.json')))
    for row in rows:
     id=row['run']; session={'account':{'account_id':id,'email':'fixture@example.test','created_at':m.NOW},'session_id':'sess_1','access_token':id+'_token'+'x'*40,'expires_at':'2099-01-01T00:00:00Z','auth_mode':'local_mock'}
     script=await c.call('Page.addScriptToEvaluateOnNewDocument',{'source':'sessionStorage.setItem("foresightnine.web.session.v2",'+json.dumps(json.dumps(session))+')'})
     await c.call('Page.navigate',{'url':BASE+'/research/'+id})
     
     try:
      await c.until('location.pathname === '+json.dumps('/research/'+id)+' && !![...document.querySelectorAll("button")].find(x=>/已回答 [0-9]+\\/[0-9]+ 部分/.test(x.innerText))')
      toggle='[...document.querySelectorAll("button")].find(x=>/已回答 [0-9]+\\/[0-9]+ 部分/.test(x.innerText))'
      was_open=await c.js('('+toggle+')?.getAttribute("aria-expanded") === "true"')
      if not was_open: await c.click(toggle)
      await c.until('('+toggle+')?.getAttribute("aria-expanded") === "true"')
     except Exception:
      print(json.dumps(c.errors,ensure_ascii=False),file=__import__('sys').stderr); raise
     out.append({'run':id,'url':await c.js('location.href'),'parts_initially_open':was_open,'parts_open':True,'body':await c.js('document.body.innerText')}); output.write_text(json.dumps(out,ensure_ascii=False,indent=2)); await c.call('Page.removeScriptToEvaluateOnNewDocument',{'identifier':script['identifier']})
    print(json.dumps(out,ensure_ascii=False,indent=2)); print('errors',len(c.errors),file=__import__('sys').stderr)
  finally: p.kill();p.wait(timeout=10)
asyncio.run(main())
