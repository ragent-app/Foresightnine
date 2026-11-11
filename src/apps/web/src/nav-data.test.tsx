import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { computeFm } from '@foresightnine/fm-engine';
import { branches, fmInput, searchEvidenceFixture, MARKET_SNAPSHOT_BUNDLE } from './fixtures';
import { allocationRows, initialState, reducer } from './state';
import { buildNavExport, parseReturn, toDrawdown, type NavAssumptions } from './nav-data';
import { decodeNavAssumptions, navAssumptionsKey } from './nav-assumptions';
import { NavPanel } from './components/NavPanel';
import { PortfolioPanel } from './components/PortfolioPanel';
import { navExport, researchExport } from './exports';
const result=computeFm(fmInput());
const base=allocationRows(result,100000,'CNY','raw');
const rows=[{...base[0]!,amount:40000},{...base[1]!,amount:50000}];
const assumptions:NavAssumptions=Object.fromEntries(branches.map((b,i)=>[b.worldline.worldline_id,{[rows[0]!.instrumentId]:String((i-1)*10),[rows[1]!.instrumentId]:String((i-1)*20)}]));
const build=(r=rows,a=assumptions)=>buildNavExport(branches,r,100000,a);
const noop=()=>{};
function completed(){
 let s=reducer({...initialState,viewpoint:'QA scenario assumptions',capital:'100000'},{type:'start_challenge'});
 s=reducer(s,{type:'challenge_complete'});s=reducer(s,{type:'submit_reply',body:'QA'});s=reducer(s,{type:'add_evidence',evidence:searchEvidenceFixture});s=reducer(s,{type:'confirm',confirmation:'confirmed'});s=reducer(s,{type:'fm_start'});s=reducer(s,{type:'fm_complete',result});return s;
}
describe('user-owned portfolio paths',()=>{
 it('never substitutes fixed history or paths for missing inputs',()=>{const v=build(rows,{});expect(v.status).toBe('not_available');expect(v.historical).toEqual([]);expect(v.weak).toEqual([]);expect(v.scenarios.main.missingAssets).toHaveLength(2);});
 it('uses actual rounded amounts and residual cash, not target weights',()=>{const v=build();expect(v.status).toBe('ready');expect(v.residualCash).toBe(10000);expect(v.weak.at(-1)).toBeCloseTo(.86);expect(v.main.at(-1)).toBe(1);expect(v.strong.at(-1)).toBeCloseTo(1.14);expect(v.scenarios.strong.terminalReturn).toBeCloseTo(.14);});
 it('changes paths when allocations change with the same assumptions',()=>{const v=build([{...rows[0]!,amount:60000},{...rows[1]!,amount:30000}]);expect(v.strong.at(-1)).toBeCloseTo(1.12);expect(v.strong).not.toEqual(build().strong);});
 it('does not multiply by uncalibrated probability',()=>{expect(buildNavExport(branches.map(b=>({...b,assessment:{...b.assessment,conditional_probability:.99}})),rows,100000,assumptions).strong).toEqual(build().strong);});
 it('does not treat qualitative impact or blank as numerical zero',()=>{const a={...assumptions,[branches[0]!.worldline.worldline_id]:{[rows[0]!.instrumentId]:'0',[rows[1]!.instrumentId]:' '}};expect(build(rows,a).weak).toEqual([]);expect(build(rows,a).main).toHaveLength(25);});
 it('uses explicit zero and handles all-cash allocation',()=>{const v=build(rows.map(r=>({...r,amount:0})),{});expect(v.status).toBe('ready');expect(v.residualCash).toBe(100000);expect(v.weak.every(x=>x===1)).toBe(true);});
 it('does not carry numerical assumptions to another worldline',()=>{const v=buildNavExport(branches.map(b=>({...b,worldline:{...b.worldline,worldline_id:'new-'+b.kind}})),rows,100000,assumptions);expect(v.status).toBe('not_available');});
 it.each(['',' ','NaN','Infinity','1e3','-101','1001','10%','1,000'])('rejects invalid return %s',v=>expect(parseReturn(v)).toBe(null));
 it.each([['-100',-100],['0',0],['+25.5',25.5],['1000',1000]])('accepts explicit bounded return %s',(v,n)=>expect(parseReturn(v)).toBe(n));
 it('supports total loss without division by zero',()=>{const a=Object.fromEntries(branches.map(b=>[b.worldline.worldline_id,{[rows[0]!.instrumentId]:'-100',[rows[1]!.instrumentId]:'-100'}]));const v=build([{...rows[0]!,amount:50000},{...rows[1]!,amount:50000}],a);expect(v.main.at(-1)).toBe(0);expect(v.drawdown.main.at(-1)).toBe(-1);});
 it('drawdown follows the actual supplied path',()=>expect(toDrawdown([1,1.2,.6,.9])).toEqual([0,0,-.5,-.25]));
 it('uses actual worldline dates, with no today/history boundary',()=>{const v=build();expect(v.dates[0]).toBe(branches[0]!.worldline.baseline_date);expect(v.dates.at(-1)).toBe(branches[0]!.worldline.horizon_end);expect(v.boundary.historicalCount).toBe(0);});
 it.each([0,-1,NaN,Infinity])('fails closed for invalid capital %s',c=>expect(buildNavExport(branches,rows,c,assumptions).status).toBe('not_available'));
 it('rejects duplicate holdings, overspend and bad dates',()=>{expect(build([rows[0]!,rows[0]!]).status).toBe('not_available');expect(build([{...rows[0]!,amount:100001}]).status).toBe('not_available');expect(buildNavExport(branches.map(b=>({...b,worldline:{...b.worldline,horizon_end:'invalid'}})),rows,100000,assumptions).status).toBe('not_available');});
 it('exports exactly the same input-dependent paths as the UI, with lineage',()=>{const s=completed();expect(s.fmStatus).toBe('complete');const e=navExport(s,branches,result.cutoff,rows,assumptions);expect(e.strong).toEqual(build().strong);expect(e.scenarios).toEqual(build().scenarios);expect(e.labels).toEqual({historical:'none',simulated:'user_assumptions_uncalibrated'});expect((researchExport(s,branches,rows,result.cutoff,assumptions).nav as typeof e).strong).toEqual(e.strong);});
 it('direct export cannot bypass lifecycle confirmation',()=>{const e=navExport(initialState,branches,result.cutoff,rows,assumptions);expect(e.status).toBe('not_available');expect(e.strong).toEqual([]);expect(e.worldlines).toEqual([]);});
 it('renders actual endpoint and no fabricated history, quantile, fixed date or volatility',()=>{const html=renderToStaticMarkup(createElement(NavPanel,{branches,selected:'strong',onSelect:noop,stress:'main',onExport:noop,data:build(),assumptions,onAssumption:noop}));expect(html).toContain('14.00%');expect(html).toContain('series-group-strong is-emphasized');for(const bad of ['series-history','10–90%','252','WL-v1','2026-08-09','主情景中位数'])expect(html).not.toContain(bad);expect(html).toContain('aria-label="GRID 强情景 %"');});
 it('shows incomplete assumptions without a substitute chart',()=>{const html=renderToStaticMarkup(createElement(NavPanel,{branches,selected:'main',onSelect:noop,stress:'main',onExport:noop,data:build(rows,{})}));expect(html).toContain('待补全有效假设');expect(html).not.toContain('<svg');});
 it('portfolio stress uses the same terminal returns instead of hardcoded ranges',()=>{const html=renderToStaticMarkup(createElement(PortfolioPanel,{mode:'raw',currency:'CNY',rows,capital:100000,selectedBranch:'main',branches,stress:'strong',onMode:noop,onStress:noop,onCompute:noop,onNav:noop,onExport:noop,fmReady:true,navData:build()}));expect(html).toContain('14.00%');expect(html).not.toContain('+8% to +22%');});
});
describe('separate versioned assumption drafts',()=>{
 it('roundtrips values and isolates accounts and bundles',()=>{expect(decodeNavAssumptions(JSON.stringify(assumptions))).toEqual(assumptions);expect(navAssumptionsKey('a','bundle')).not.toBe(navAssumptionsKey('b','bundle'));expect(navAssumptionsKey('a','old')).not.toBe(navAssumptionsKey('a','new'));});
 it.each(['null','[]','{"__proto__":{}}','{"w":{"constructor":"1"}}','{"w":{"i":1}}','invalid'])('rejects malformed drafts %s',v=>expect(decodeNavAssumptions(v)).toEqual({}));
});

it('exports a valid five-asset live FM result when CASH is explicitly missing',()=>{
 const bundle={...MARKET_SNAPSHOT_BUNDLE,snapshots:MARKET_SNAPSHOT_BUNDLE.snapshots.map(r=>r.assetId==='CASH' ? {...r,asOf:null,price:null,close:null,features:{},status:'missing' as const}:r)};
 const five=computeFm(fmInput(bundle));expect(five.weights.weights.length).toBe(5);
 const s={...completed(),fmResult:five};
 const e=navExport(s,branches,five.cutoff,rows,assumptions);expect(e.status).toBe('ready');expect(e.strong).toEqual(build().strong);
 expect(researchExport(s,branches,rows,five.cutoff,assumptions).fm).toMatchObject({status:'computed'});
 const bad={...five,weights:{...five.weights,z:new Float64Array(6)}};
 expect(navExport({...s,fmResult:bad},branches,five.cutoff,rows,assumptions).status).toBe('not_available');
});
it('does not emit a flat all-cash substitute for disabled Public Default',()=>{
 expect(navExport({...completed(),portfolioMode:'public'},branches,result.cutoff,rows.map(r=>({...r,amount:0,publicExecutionAllowed:false})),assumptions).status).toBe('not_available');
});
