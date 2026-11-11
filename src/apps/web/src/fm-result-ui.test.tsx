import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { computeFm } from '@foresightnine/fm-engine';
import { fmInput, MARKET_SNAPSHOT_BUNDLE } from './fixtures';
import { FmPanel } from './components/FmPanel';
import { AppShell } from './components/AppShell';

const noop=()=>undefined;
const result=computeFm({...fmInput(),factors:[{id:77,icMean:-0.125,icir:0.8,admitted:true,expression:{op:'field',name:'momentum'}}]});
function panel(status:'computing'|'complete'|'failed'|'cancelled', value:typeof result|null=null) {
 return renderToStaticMarkup(createElement(FmPanel,{status,progress:status==='complete'?100:8,step:'正在获取金融数据…',resultReady:!!value,result:value,onCompute:noop,onCancel:noop,onPortfolio:noop}));
}
describe('actual FM result presentation',()=>{
 it('does not fabricate a factor table during acquisition',()=>{const h=panel('computing');expect(h).not.toContain('0.072');expect(h).not.toContain('<table');expect(h).toContain('正在获取金融数据');});
 it('renders descriptors and applied quality weights from the worker',()=>{const h=panel('complete',result);expect(h).toContain('77');expect(h).toContain('-0.125');expect(h).toContain('0.800');expect(h).toContain('0.100');expect(h).not.toContain('盈利修正');});
 it('distinguishes calculation failure from idle',()=>{const h=panel('failed');expect(h).toContain('计算失败');expect(h).not.toContain('等待本地计算');expect(h).not.toContain('<table');});
 it('does not present an earlier result as the current retry result',()=>{expect(panel('computing',result)).not.toContain('<table');expect(panel('failed',result)).not.toContain('<table');});
 it('discloses equal weight fallback with no invented admitted factors',()=>{const noFactors=computeFm({...fmInput(),factors:[]});const h=panel('complete',noFactors);expect(h).toContain('等权回退');expect(h).not.toContain('0.072');});
 it('shows actual cutoff and never claims a digital signature',()=>{const h=panel('complete',result);expect(h).toContain(result.cutoff);expect(h).not.toContain('签名');expect(h).not.toContain('3 / 27');});
 it('offers recomputation rather than allocation without result evidence',()=>{const h=panel('complete');expect(h).not.toContain('查看配置方案');expect(h).toContain('开始本地计算');});
 it('offers a direct factor step in reached-stage navigation',()=>{const h=renderToStaticMarkup(createElement(AppShell,{stage:'fm',onStage:noop,canNavigate:()=>true,children:null}));expect(h).toMatch(/aria-current="page"[^>]*>.*?因子计算/);});
});

import { reconcileMarketFreshness } from './App';
import { createMarketRuntimeController } from '@foresightnine/market-data';
import { allocationRows } from './state';
const savedFx={runtimeAsOf:'2026-10-03T00:53:18.625Z',providerStatus:'unavailable' as const,fxProviderStatus:'unavailable' as const,fxAsOf:'2026-10-02',fxCnyPerUnit:{CNY:1,USD:6.73,EUR:7.85}};
it('retains saved FX for historical Raw display when a new tab runtime has no bundle',()=>{
 const next=reconcileMarketFreshness(savedFx,createMarketRuntimeController().getState());
 expect(next.fxCnyPerUnit).toEqual(savedFx.fxCnyPerUnit);
 expect(next.fxAsOf).toEqual(savedFx.fxAsOf);
 expect(next.providerStatus).toBe('unavailable'); expect(next.fxProviderStatus).toBe('unavailable');
});
it('does not silently change historical unit counts after a refresh',()=>{
 const next=reconcileMarketFreshness(savedFx,createMarketRuntimeController().getState());
 expect(allocationRows(result,100000,'CNY','raw',next).map(x=>[x.id,x.units,x.amount])).toEqual(allocationRows(result,100000,'CNY','raw',savedFx).map(x=>[x.id,x.units,x.amount]));
});

it('does not carry an old FX quote into a new bundle that lacks FX',()=>{
 const {fx: _ignored,...withoutFx}=MARKET_SNAPSHOT_BUNDLE;
 const next=reconcileMarketFreshness(savedFx,{...createMarketRuntimeController().getState(),status:'ready',mode:'public',bundle:withoutFx});
 expect(next.fxCnyPerUnit).toBeUndefined(); expect(next.fxProviderStatus).toBe('unavailable');
});
it('adopts a newly received FX snapshot rather than mixing it with the draft',()=>{
 const next=reconcileMarketFreshness(savedFx,{...createMarketRuntimeController().getState(),status:'ready',mode:'public',bundle:MARKET_SNAPSHOT_BUNDLE});
 expect(next.fxCnyPerUnit).toEqual(MARKET_SNAPSHOT_BUNDLE.fx?.cnyPerUnit);
 expect(next.fxAsOf).toEqual(MARKET_SNAPSHOT_BUNDLE.fx?.asOf);
});
