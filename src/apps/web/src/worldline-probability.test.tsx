import { expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { branches } from './fixtures';
import { worldlineBundleToBranches } from './api-runtime';
import { NavPanel } from './components/NavPanel';
import { WorldlinePanel } from './components/WorldlinePanel';
import type { WorldlineBundle } from '@foresightnine/contracts';
const bundle:WorldlineBundle={schema_version:'v1',created_at:'2026-10-02T00:00:00Z',source_refs:[],content_hash:'a'.repeat(64),bundle_id:'test-bundle',challenge_id:'test-challenge',worldlines:branches.map(b=>b.worldline),probability_assessments:branches.map(b=>b.assessment)};
it('uses conditional, not joint, probability in API branch labels',()=>{
  const view=worldlineBundleToBranches(bundle,'zh');
  for(const b of view){
    const r=b.assessment.conditional_probability_range;
    expect(b.range).toBe(`${(r.minimum*100).toFixed(1)}–${(r.maximum*100).toFixed(1)}%`);
  }
});
it('renders saved branches from their numeric assessment, not an obsolete cached label',()=>{
  const html=renderToStaticMarkup(createElement(WorldlinePanel,{branches:branches.map(b=>({...b,range:'STALE_JOINT_LABEL'})),selected:'main',onSelect:()=>{},onCompute:()=>{}}));
  expect(html).not.toContain('STALE_JOINT_LABEL');
  expect(html).toContain('45.00–62.00%');
});

it('uses actual branch titles and conditional ranges in the NAV selector',()=>{
  const custom=branches.map(b=>({...b,title:`API ${b.kind}`,assessment:{...b.assessment,conditional_probability_range:{minimum:0.11,maximum:0.22}}}));
  const html=renderToStaticMarkup(createElement(NavPanel,{branches:custom,selected:'strong',onSelect:()=>{},stress:'main',onExport:()=>{}}));
  expect(html).toContain('API strong · P(W | H) 11.00–22.00%');
  expect(html).not.toContain('45–62%');expect(html).not.toContain('主世界线下的组合路径');
});
