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
    for row in [r for r in json.loads((HERE/'frozen-baseline.json').read_text())['rows'] if r['run']=='D2-A-2']:
     id=row['run']; session={'account':{'account_id':id,'email':'fixture@example.test','created_at':m.NOW},'session_id':'sess_1','access_token':id+'_token'+'x'*40,'expires_at':'2099-01-01T00:00:00Z','auth_mode':'local_mock'}
     script=await c.call('Page.addScriptToEvaluateOnNewDocument',{'source':'sessionStorage.setItem("foresightnine.web.session.v2",'+json.dumps(json.dumps(session))+')'})
     await c.call('Page.navigate',{'url':BASE+'/research/'+id})
     
     try: await c.until('document.body && (document.body.innerText.includes("部分完成") || document.body.innerText.includes("研究不存在") || document.body.innerText.includes("加载失败"))')
     except Exception:
      print(json.dumps(c.errors,ensure_ascii=False),file=__import__('sys').stderr); raise
     await c.click('[...document.querySelectorAll("button")].find(x=>x.innerText.includes("查看原文"))'); await c.until('document.querySelector(".reader-text mark.on")?.textContent'); out.append({'reader':await c.js('document.body.innerText'),'run':id,'url':await c.js('location.href'),'body':await c.js('document.body.innerText')}); (HERE/'browser-citation.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)); await c.call('Page.removeScriptToEvaluateOnNewDocument',{'identifier':script['identifier']})
    print(json.dumps(out,ensure_ascii=False,indent=2)); print('errors',len(c.errors),file=__import__('sys').stderr)
  finally: p.kill();p.wait(timeout=10)
asyncio.run(main())
