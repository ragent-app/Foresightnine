"""Local-only UI acceptance: Chrome/CDP plus deterministic HTTP fixture.

Run from the repository root with src/apps/api/.venv/bin/python. Requires an
already running Vite on 127.0.0.1:15077; creates no backend state. Screenshots
are written to /tmp/foresightnine-browser-acceptance.
"""
import asyncio, base64, json, os, pathlib, subprocess, tempfile, time, urllib.request
from urllib.parse import urlparse
import websockets

BASE = 'http://127.0.0.1:15077'
OUT = pathlib.Path('/tmp/foresightnine-browser-acceptance')
OUT.mkdir(exist_ok=True)
TEXT = 'Year ended 2026: total revenue was 100 USD. The source did not disclose EPS.'
QUOTE = 'Year ended 2026: total revenue was 100 USD.'
NOW = '2026-11-23T12:00:00Z'
CLAIM = {'claim_id':'C1','role':'support','text':'2026 revenue was 100 USD.', 'turn_id':'turn_1',
         'citations':[{'source_id':'s1','quotation':QUOTE,'location':{'character_start':0,'character_end':len(QUOTE)}}]}
SOURCE = {'status':'available','source_id':'s1','title':'示例年报','kind':'user_supplied','is_simulated':False,
          'published_at':NOW,'event_at':None,'revised_at':None,'obtained_at':NOW,'content_sha256':'a'*64,
          'retrieval_method':'user_supplied','pdf_text_order':None,'derived_from_source_id':None,
          'permissions':{'store':True,'model_input':True,'display':True,'export':True},'verification':'user_declared_not_verified'}
def topic(partial=False):
    q='收入和 EPS 是多少？' if partial else '2026 年收入是多少？'
    parts=[{'id':'r1','question':'2026 年收入','core':True,'status':'answered','answer':'收入为 100 USD。','reason':'','references':[{'claim_id':'C1','origin':'current'}],'requirements':[]}]
    if partial: parts.append({'id':'r2','question':'EPS','core':True,'status':'unresolved','answer':'','reason':'未找到 EPS，仍需补充资料。','references':[],'requirements':[]})
    turn={'turn_id':'turn_1','index':1,'question':q,'question_origin':'user','status':'partial' if partial else 'completed','reason':'submitted',
          'started_at':NOW,'finished_at':NOW,'parent_version_id':'v1','operation_ids':['op_1'],
          'official_fetch_status':'not_requested','search_status':'not_requested','usage':{'model_calls':2,'search_calls':0,'total_tokens':12},
          'report':'' if partial else '2026 年收入为 100 USD。[[claim:C1]]','claim_ids':['C1'],'fact_ids':[],'calculation_ids':[],
          'gaps':[],'events':[],'question_parts':parts}
    return {'topic_id':'partial' if partial else 'complete','owner_id':'acc_1','created_at':NOW,'updated_at':NOW,
            'request':{'question':q,'source_ids':['s1']},'versions':[{'version_id':'v1','created_at':NOW,'created_by':'user','review':'draft','content':{}},
            {'version_id':'v2','created_at':NOW,'created_by':'agent','review':'draft','source_ids':['s1'],
             'content':{'turns':[turn],'claims':[CLAIM],'sources':[]}}]}

def response(path):
    if path == '/v2/research/topics': return []
    if path in ('/v2/research/topics/complete','/v2/research/topics/partial'): return topic(path.endswith('/partial'))
    if path.endswith('/runs'): return []
    if path.endswith('/versions/v2/sources/s1/text'): return {'source_id':'s1','text':TEXT,'content_sha256':'a'*64}
    if path.endswith('/versions/v2/sources'): return [SOURCE]
    return [] if path.endswith('/sources') else {}

class CDP:
    def __init__(self, ws): self.ws=ws; self.seq=0; self.pending={}; self.errors=[]; self.requests=[]
    async def start(self): self.task=asyncio.create_task(self.listen())
    async def listen(self):
        async for raw in self.ws:
            msg=json.loads(raw)
            if 'id' in msg:
                fut=self.pending.pop(msg['id'],None)
                if fut and not fut.done(): fut.set_result(msg)
            elif msg.get('method')=='Fetch.requestPaused':
                p=msg['params']; url=p['request']['url']; path=urlparse(url).path; self.requests.append(path)
                body=base64.b64encode(json.dumps(response(path),ensure_ascii=False).encode()).decode()
                asyncio.create_task(self.call('Fetch.fulfillRequest',{'requestId':p['requestId'],'responseCode':200,'responseHeaders':[{'name':'Content-Type','value':'application/json'}],'body':body}))
            elif msg.get('method') in ('Runtime.exceptionThrown','Log.entryAdded'):
                self.errors.append(msg)
    async def call(self, method, params=None):
        self.seq+=1; n=self.seq; fut=asyncio.get_running_loop().create_future(); self.pending[n]=fut
        await self.ws.send(json.dumps({'id':n,'method':method,'params':params or {}}))
        result=await asyncio.wait_for(fut,15)
        if 'error' in result: raise RuntimeError(str(result['error']))
        return result.get('result',{})
    async def js(self, expr):
        r=await self.call('Runtime.evaluate',{'expression':expr,'returnByValue':True,'awaitPromise':True})
        if 'exceptionDetails' in r: raise RuntimeError(str(r['exceptionDetails']))
        return r['result'].get('value')
    async def until(self, expr):
        for _ in range(100):
            value=await self.js(expr)
            if value: return value
            await asyncio.sleep(.1)
        raise AssertionError('timed out: '+expr+'\n'+str(await self.js('document.body.innerText'))[:5000])
    async def click(self, expr):
        box=await self.until(f'(()=>{{let e={expr}; if(!e)return null; e.scrollIntoView({{block:"center"}}); let r=e.getBoundingClientRect(); return {{x:r.x+r.width/2,y:r.y+r.height/2}}}})()')
        await self.call('Input.dispatchMouseEvent',{'type':'mousePressed','x':box['x'],'y':box['y'],'button':'left','clickCount':1})
        await self.call('Input.dispatchMouseEvent',{'type':'mouseReleased','x':box['x'],'y':box['y'],'button':'left','clickCount':1})
    async def screenshot(self, name):
        p=await self.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
        (OUT/name).write_bytes(base64.b64decode(p['data']))

def poll_json(url):
    for _ in range(100):
        try: return json.load(urllib.request.urlopen(url,timeout=1))
        except Exception: time.sleep(.1)
    raise RuntimeError('Chrome debug endpoint did not start')

async def main():
    with tempfile.TemporaryDirectory(prefix='fn-browser-', ignore_cleanup_errors=True) as profile:
        proc=subprocess.Popen(['/usr/bin/google-chrome','--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
                               '--remote-debugging-port=19223','--remote-allow-origins=*',f'--user-data-dir={profile}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            tabs=await asyncio.to_thread(poll_json,'http://127.0.0.1:19223/json')
            async with websockets.connect(tabs[0]['webSocketDebuggerUrl'],max_size=10_000_000) as ws:
                c=CDP(ws); await c.start()
                for method in ('Page.enable','Runtime.enable','Log.enable'): await c.call(method)
                await c.call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':900,'deviceScaleFactor':1,'mobile':False})
                await c.call('Fetch.enable',{'patterns':[{'urlPattern':'*/v2/*'},{'urlPattern':'*/v1/*'}]})
                session={'account':{'account_id':'acc_1','email':'fixture@example.test','created_at':NOW},'session_id':'sess_1','access_token':'t'*40,
                         'expires_at':'2099-01-01T00:00:00Z','auth_mode':'local_mock'}
                init='sessionStorage.setItem("foresightnine.web.session.v2", '+json.dumps(json.dumps(session))+');'
                await c.call('Page.addScriptToEvaluateOnNewDocument',{'source':init})
                await c.call('Page.navigate',{'url':BASE+'/research/complete'})
                await c.until('document.body?.innerText.includes("2026 年收入是多少？") && !!document.querySelector("#claim-C1")')
                print('page:',await c.js('location.href'),await c.js('document.title'))
                await c.until('document.querySelector(".report")?.innerText.includes("查看依据")')
                await c.screenshot('01-completed.png')
                await c.click("document.querySelector('.report a[href=\"#claim-C1\"]')")
                await c.until('document.querySelector("#claim-C1")?.getBoundingClientRect().top >= 0')
                await c.screenshot('02-claim.png')
                await c.click("document.querySelector('#claim-C1 button[aria-label=\"引用 1\"]')")
                await c.until('[...document.querySelectorAll("button")].some(x=>x.innerText.trim()==="查看原文")')
                await c.click('[...document.querySelectorAll("button")].find(x=>x.innerText.trim()==="查看原文")')
                await c.until('document.querySelector(".reader-text mark.on")?.textContent.includes("total revenue was 100 USD")')
                await c.screenshot('03-source.png')
                await c.call('Page.reload')
                await c.until('document.querySelector(".report")?.innerText.includes("查看依据") && !!document.querySelector("#claim-C1")')
                await c.call('Page.navigate',{'url':BASE+'/research/partial'})
                await c.until('document.body?.innerText.includes("部分完成") && document.body?.innerText.includes("未找到 EPS")')
                await c.screenshot('04-partial.png')
                print('partial visible:',await c.js('document.body?.innerText.includes("尚未完成")'))
                print('api paths:',json.dumps(c.requests,ensure_ascii=False)); print('errors:',json.dumps(c.errors,ensure_ascii=False)[:3000])
                assert not c.errors, 'Browser console or runtime error'
                print('PASS: complete -> claim -> source -> reload -> partial')
        finally:
            proc.terminate(); proc.wait(timeout=10)
if __name__=='__main__': asyncio.run(main())
