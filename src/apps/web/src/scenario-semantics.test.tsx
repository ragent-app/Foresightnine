import { expect,it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { branches } from './fixtures';
import { WorldlinePanel } from './components/WorldlinePanel';

it('does not expose repeated raw calibration enum strings to readers',()=>{
  const html=renderToStaticMarkup(createElement(WorldlinePanel,{branches,selected:'main',onSelect:()=>{},onCompute:()=>{}}));
  expect(html).not.toContain('model_estimate_unvalidated');
});
it('flags retained v1 live scenarios for upgrade instead of offering factor calculation',()=>{
  const old=branches.map(b=>({...b,worldline:{...b.worldline,provenance:'live account-scoped scenario-v1; model inference; no live market observation; uncalibrated'}}));
  const html=renderToStaticMarkup(createElement(WorldlinePanel,{branches:old,selected:'main',onSelect:()=>{},onCompute:()=>{},canCompute:true}));
  expect(html).toContain('旧版草稿');
  expect(html).not.toContain('P(W | H)');
  expect(html).toMatch(/<button[^>]*disabled=""[^>]*>进入因子计算/);
});

import { canEnterStage, canStartFm, initialState, reducer, safeStageFor } from './state';
import { legacyLiveScenarios } from './scenario-status';
import { completeWorldlineFlow } from './worldline-flow';
import type { ApiClient } from '@foresightnine/api-client';
import { vi } from 'vitest';

const legacy=branches.map(b=>({...b,worldline:{...b.worldline,provenance:'live account-scoped scenario-v1; uncalibrated'}}));
const guarded=branches.map(b=>({...b,worldline:{...b.worldline,provenance:'live account-scoped scenario-v2; premise_guard=v2; uncalibrated',bundle_id:'bundle:guarded'}}));
const ready={...initialState,stage:'worldline' as const,viewpoint:'QA research',challengeStatus:'complete' as const,
  confirmation:'reserved' as const,unresolvedIssuesAcknowledged:true,reservationNote:'QA conditions',
  challengeTurns:[{id:'qa',time:'now',body:'QA',author:'user' as const},{id:'review',time:'now',body:'QA review',author:'challenger' as const},{id:'answer',time:'now',body:'QA answer',author:'user' as const}],serverBranches:legacy};
it('retains legacy research but gates dependent stages until an explicit upgrade',()=>{
  expect(canStartFm(ready)).toBe(false);
  for(const stage of ['fm','portfolio','nav'] as const){expect(canEnterStage(ready,stage)).toBe(false);expect(safeStageFor({...ready,stage})).toBe('worldline');}
  expect(canStartFm({...ready,serverBranches:guarded})).toBe(true);
  expect(legacyLiveScenarios(branches)).toBe(false); // Explicit offline fixtures remain fixtures.
});
it('clears stale derived calculations only when the bundle identity changes',()=>{
  const current={...ready,fmStatus:'complete' as const,fmProgress:100};
  const next=reducer(current,{type:'api_worldlines_ready',branches:guarded});
  expect(next.fmStatus).toBe('idle');expect(next.fmResult).toBeNull();
  expect(next.viewpoint).toBe(ready.viewpoint);expect(next.challengeTurns).toEqual(ready.challengeTurns);
  expect(reducer({...current,serverBranches:guarded},{type:'api_worldlines_ready',branches:guarded}).fmStatus).toBe('complete');
});
it('uses a v2 request identity to avoid replaying the known bad v1 result',async()=>{
  const client={getChallenge:vi.fn().mockResolvedValue({challenge:{status:'CONFIRMED'}}),createWorldlines:vi.fn().mockResolvedValue({})};
  const saved=new Map([['foresightnine.worldline-attempt.v1:challenge:qa:2028-08-31','3']]);
  await completeWorldlineFlow(client as unknown as ApiClient,'challenge:qa',{command:'CONFIRM',unresolved_issues_acknowledged:true},
    {operationId:'operation:confirmed',signal:new AbortController().signal,onConfirmed:()=>{},storage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>{saved.set(k,v)}}});
  expect(client.createWorldlines.mock.calls[0]![1].idempotencyKey).toBe('worldlines-v2:challenge:qa:2028-08-31:0');
  expect(saved.get('foresightnine.worldline-attempt.v1:challenge:qa:2028-08-31')).toBe('3');
});
it('renders dated checkpoints without a fabricated evenly-spaced time axis',()=>{
  const html=renderToStaticMarkup(createElement(WorldlinePanel,{branches:guarded,selected:'main',onSelect:()=>{},onCompute:()=>{}}));
  expect(html).toContain('<time dateTime=');expect(html).toContain('scenario-events');expect(html).not.toContain('timeline-axis');
  expect(html).toContain('假设检查点');expect(html).not.toContain('确认选择');
});
it('provides an explicit upgrade control and blocks duplicate generation',()=>{
  const html=renderToStaticMarkup(createElement(WorldlinePanel,{branches:legacy,selected:'main',onSelect:()=>{},onCompute:()=>{},onRegenerate:()=>{},generating:true}));
  expect(html).toContain('正在生成…');expect(html).toMatch(/<button[^>]*disabled=""[^>]*>正在生成/);
});

import { WorldlineExplanation } from './components/WorldlinePanel';
it('shows actual model reasons and missing sources, not invented generic evidence',()=>{
  const branch={...guarded[0]!,assessment:{...guarded[0]!.assessment,adjustment_reasons:['QA model reason'],base_rate_refs:[]}};
  const html=renderToStaticMarkup(createElement(WorldlineExplanation,{branch}));
  expect(html).toContain('QA model reason');expect(html).toContain('未附基准率来源');
  expect(html).not.toContain('基准率 · 中性起点');expect(html).not.toContain('专家约束');
});
