// Isolated browser acceptance: every API response below is synthetic.
// Run against the local Vite QA harness, never against production.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/home/lxx/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const OUT = __dirname;
const now = '2026-11-13T12:00:00Z';
const empty = () => ({summary:'',sources:[],source_candidates:[],facts:[],calculations:[],claims:[],limitations:[],run:{}});
const source = {source_id:'source_qa',title:'QA source',status:'available',permissions:{display:true,model_input:true,export:false},published_at:now,obtained_at:now};
const claim = {claim_id:'claim_qa',text:'Recorded mock finding',role:'support',kind:'model_interpretation',support_status:'not_independently_verified',citations:[{source_id:'source_qa',quotation:'Evidence text',location:{character_start:0,character_end:13}}]};
const run = {source_processing:[{source_id:'source_qa',eligibility:'eligible',initial_selection:'not_selected',selection_status:'not_selected',analysis_status:'not_started',selection_gap:'not_selected_reason_unknown'}],requirement_coverage:{schema_version:'1',requirements:[{requirement_id:'user_request',origin:'user',relation_status:'not_assessed',gaps:[]}]},operation_id:'operation_qa',status:'partial',checkpoint:'analysis_completed',workflow_receipts:[{stage:'analysis',status:'completed'},{stage:'coordination',status:'stopped',reason:'remaining_gaps'}],evidence_links:{schema_version:'1',truncated:false,claims:[{claim_index:0,citation_location_status:'all_recorded',semantic_support_status:'not_independently_verified',model_review_opinion:'uncertain',citations:[{citation_index:0,same_passage_fact_ids:[]}]}],calculations:[]},research_gaps:{schema_version:'1',items:[{code:'semantic_support_not_independently_verified'}]}};
const baseTopic = (id,q,ids=[]) => ({topic_id:id,request:{question:q,source_ids:ids,scope:'',as_of:null},versions:[{version_id:'version_1',created_at:now,review:'draft',note:'',created_by:'agent',source_ids:ids,content:empty()}]});
let account='a', topics={a:[baseTopic('legacy','Legacy research')],b:[]}, selected=null, op=null, reason='unknown_external', events=[];
const operation = status => ({operation_id:'operation_qa',status,cancel_token:'cancel_qa'});
function completedVersion(topic) {const c=empty();Object.assign(c,{claims:[claim],sources:[{source_id:'source_qa',title:'QA source'}],limitations:['Mock limitation'],run});topic.versions.push({version_id:'version_2',parent_version_id:'version_1',created_at:now,review:'draft',note:'',created_by:'agent',source_ids:['source_qa'],content:c});}
(async () => {
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || '/usr/bin/google-chrome',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(5000);const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error') errors.push(m.text())});
 await page.route('**/v*/**',async route=>{const req=route.request(),u=new URL(req.url()),p=u.pathname,method=req.method();if(!/^\/v[12]\//.test(p)) return route.continue();const body=req.postDataJSON?.() || {};events.push(`${method} ${p}`);let data={},status=200;
  const topicId=p.match(/\/topics\/([^/]+)/)?.[1]; const t=topics[account].find(x=>x.topic_id===topicId);
  if(p==='/v2/research/topics' && method==='GET') data=topics[account];
  else if(p==='/v2/research/sources' && method==='POST') data={source_id:'source_qa'};
  else if(p==='/v2/research/topics' && method==='POST'){selected=baseTopic('topic_'+Date.now(),body.question,body.source_ids);topics[account].push(selected);data=selected;}
  else if(p.endsWith('/context')) data={title:'QA source',before:'Before ',selected:'Evidence text',after:' after',page_start:null,page_end:null};
  else if(p.endsWith('/sources') && method==='GET') data=t?.versions.at(-1).source_ids.includes('source_qa')?[source]:[];
  else if(p.endsWith('/runs') && method==='GET') data=op?[{operation:op}]:[];
  else if(p.endsWith('/runs') && method==='POST'){op=operation('running');data={operation:op};}
  else if(p.endsWith('/progress')) data={version_id:'version_2',checkpoint:'analysis_completed',result_status:'partial',created_at:now};
  else if(p.endsWith('/inputs')) data={captured_at:now,parent_version_id:'version_1',as_of:null,budget:{max_elapsed_seconds:60,max_total_tokens:10000},sources:[]};
  else if(p.endsWith('/recovery-status')) data={can_continue:reason===null,can_deliver:reason!=='changed',reason,checkpoint:run.checkpoint,version_id:reason==='changed'?null:'version_2'};
  else if(p.endsWith('/continue') || p.endsWith('/recover')) {op=operation('running');data={operation:op};}
  else if(p.endsWith('/cancel')){op=operation('cancelled');if(selected.versions.length===1) completedVersion(selected);data={operation:op};}
  else if(p.endsWith('/export/metadata')) data={schema_version:'1',format:'metadata_only',topic_id:topicId,version_id:'version_2',disclosure:'No research text or source content is included. Full export requires source permissions.'};
  else if(p.endsWith('/export')){status=403;data={detail:'not allowed'};}
  else if(p.endsWith('/versions') && method==='POST'){const v={version_id:'version_3',parent_version_id:body.parent_version_id,created_at:now,review:body.review,note:body.note,created_by:'user',source_ids:['source_qa'],content:body.content};t.versions.push(v);data=t;}
  else if(p.match(/\/runs\/operation_qa$/)) data={operation:op};
  else if(p.match(/\/topics\/[^/]+$/)) data=t;
  else {status=404;data={detail:'mock route missing'};}
  await route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 });
 const checks=[];const check=async(name,fn)=>{await fn();checks.push({name,status:'passed'});console.log('PASS',name)};
 try{
  await page.goto('http://127.0.0.1:5173/qa/stage-b-acceptance/');
  await check('page identity and visible workspace',async()=>{assert.equal(await page.title(),'Stage B local mock acceptance');await page.getByRole('heading',{name:'Your research'}).waitFor();assert.equal(await page.locator('vite-error-overlay').count(),0)});
  await check('legacy version without new receipts',async()=>{await page.getByRole('button',{name:'Legacy research'}).click();await page.getByRole('heading',{name:'Legacy research'}).waitFor()});
  await check('question-only research entry',async()=>{await page.getByLabel('Research question').fill('Mock research question');await page.getByRole('button',{name:'Create research'}).click();await page.getByRole('heading',{name:'Mock research question'}).waitFor()});
  await check('run and cancellation',async()=>{await page.getByRole('button',{name:'Run research'}).click();await page.getByRole('button',{name:/cancel/i}).first().waitFor();await page.getByRole('button',{name:'View saved stage'}).waitFor();await page.getByRole('button',{name:/cancel/i}).first().click();await page.getByText('An external call has an unknown outcome',{exact:false}).waitFor()});
  await check('recovery unknown call fails closed',async()=>{assert.equal(await page.getByRole('button',{name:'Continue unfinished steps'}).count(),0);assert.equal(await page.getByRole('button',{name:'Deliver saved results'}).count(),1)});
  await check('saved workflow, evidence and unknown support',async()=>{await page.getByText('Execution details').click();await page.getByText('Unresolved gaps remain').waitFor();await page.getByText('Recorded evidence links').click();await page.getByText('Semantic support has not been independently verified.',{exact:false}).first().waitFor();await page.getByRole('button',{name:'View source context'}).first().click();await page.getByText('The quotation matches this text',{exact:false}).waitFor()});
  await check('source selection and recorded requirements',async()=>{await page.getByText('Selected source processing').click();await page.getByText('not selected for the initial prompt',{exact:false}).waitFor();await page.getByText('reason for not selecting is unknown',{exact:false}).waitFor();await page.getByText('Recorded question links').click();await page.getByText('coverage not assessed',{exact:false}).waitFor();await page.getByText('not an independent check of support',{exact:false}).waitFor()});
  assert.deepEqual(errors.filter(message => !message.includes('403 (Forbidden)')),[]);fs.writeFileSync(path.join(OUT,'browser-result.json'),JSON.stringify({mock:true,checks,errors,events},null,2));
 }catch(e){fs.writeFileSync(path.join(OUT,'browser-result.json'),JSON.stringify({mock:true,checks,errors,events,failure:String(e)},null,2));await page.screenshot({path:path.join(OUT,'failure.png'),fullPage:true});throw e;}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
