import { describe, expect, it, vi } from 'vitest';
import { computeFm } from '@foresightnine/fm-engine';
import { ASSET_FIXTURES, branches, fmInput } from './fixtures';
import { initialState, reducer, canEnterStage } from './state';
import { decodeWorkspace, encodeWorkspace, workspaceScope, workspaceKey, verifyWorkspace } from './research-workspace';
import type { AppState } from './types';

const scope = workspaceScope('https://api.example.test', 'account-a');
function finished(stage: AppState['stage'] = 'nav'): AppState {
  return {...initialState, stage, viewpoint:'Synthetic test premise', apiSessionId:'test-session', apiChallengeId:'test-challenge', apiOperationId:'test-operation', apiCompletedOperationId:'test-operation', apiPendingPhase:null,
    challengeStatus:'complete', challengeTurns:[{id:'u1',author:'user',time:'now',body:'Synthetic premise'}, {id:'a1',author:'challenger',time:'now',body:'Evidence?',review:{questions:['Evidence?'],objections:['Unverified'],missingEvidence:['Source']}},{id:'u2',author:'user',time:'now',body:'Still unverified'}],
    replyDraft:'Unsubmitted answer', confirmation:'reserved', unresolvedIssuesAcknowledged:true, reservationNote:'Synthetic QA, unresolved issues retained', serverBranches:branches,
    fmStatus:'complete', fmProgress:100, fmResult:computeFm(fmInput()), selectedBranch:'strong', stressScenario:'weak', drawdownVisible:false};
}
describe('research workspace persistence', () => {
  it.each(['worldline','portfolio','nav'] as const)('restores completed stages: %s', stage => {
    const restored = reducer(initialState, {type:'restore_workspace', state:finished(stage)});
    expect(restored.stage).toBe(stage);
    expect(canEnterStage(restored, 'nav')).toBe(true);
    expect(restored.replyDraft).toBe('Unsubmitted answer');
  });
  it('round trips FM typed arrays/maps, dialogue, evidence selections and navigation', () => {
    const state = finished();
    const saved = decodeWorkspace(encodeWorkspace(scope,state,ASSET_FIXTURES), scope)!;
    expect(saved).not.toBeNull();
    expect(saved.state.fmResult).toEqual(state.fmResult);
    expect(saved.state.fmResult?.forecasts).toBeInstanceOf(Float64Array);
    expect(saved.state.fmResult?.factorSignalsByTime).toBeInstanceOf(Map);
    expect(saved.state.challengeTurns).toEqual(state.challengeTurns);
    expect(saved.state.selectedBranch).toBe('strong');
    expect(saved.state.stage).toBe('nav');
    expect(saved.assets).toEqual(ASSET_FIXTURES);
  });
  it('never promotes stored freshness to live freshness', () => {
    const state = {...finished(),freshness:{runtimeAsOf:'2026-10-03',providerStatus:'fresh' as const,fxProviderStatus:'fresh' as const}};
    const saved = decodeWorkspace(encodeWorkspace(scope,state,ASSET_FIXTURES),scope)!;
    expect(saved.state.freshness.providerStatus).toBe('unavailable');
    expect(saved.state.freshness.fxProviderStatus).toBe('unavailable');
  });
  it('isolates accounts and API deployments', () => {
    const text=encodeWorkspace(scope,finished(),ASSET_FIXTURES);
    for (const other of [workspaceScope('https://api.example.test','account-b'),workspaceScope('https://staging.example.test','account-a'),workspaceScope('https://api.example.test',null)]) {
      expect(workspaceKey(other)).not.toBe(workspaceKey(scope)); expect(decodeWorkspace(text,other)).toBeNull();
    }
  });
  it('retains interrupted calculation as cancellable draft, not completed work', () => {
    const saved=decodeWorkspace(encodeWorkspace(scope,{...finished('fm'),fmStatus:'computing',fmResult:null},ASSET_FIXTURES),scope)!;
    expect(saved.state.fmStatus).toBe('cancelled');expect(canEnterStage(saved.state,'nav')).toBe(false);
  });
  it('keeps an accepted pending operation id without storing control tokens', () => {
    const state=reducer(finished('challenge'),{type:'api_operation_pending',operationId:'pending-operation',phase:'reply'});
    const saved=decodeWorkspace(encodeWorkspace(scope,state,ASSET_FIXTURES),scope)!;
    expect(saved.state.apiOperationId).toBe('pending-operation');expect(saved.state.apiPendingPhase).toBe('reply');
    const extended={...state,access_token:'not-a-real-credential'};
    expect(encodeWorkspace(scope,extended,ASSET_FIXTURES)).not.toContain('not-a-real-credential');
  });
  it.each(['not-json','{}','{"version":2}','x'.repeat(4*1024*1024+1)])('rejects malformed or oversized storage', text=>expect(decodeWorkspace(text,scope)).toBeNull());
  it('rejects structural corruption and prototype/credential keys',()=> {
    for (const change of [(w:any)=>{w.state.challengeTurns=null},(w:any)=>{w.state.fmResult.forecasts={$kind:'float64',values:['bad']}},(w:any)=>{w.state.cancel_token='not-a-real-credential'},(w:any)=>{w.state.rubric[0].score=100}]) {
      const w=JSON.parse(encodeWorkspace(scope,finished(),ASSET_FIXTURES));change(w);expect(decodeWorkspace(JSON.stringify(w),scope)).toBeNull();
    }
  });
});


// Synthetic receipts only; never exercise a real model or persist a control token.
function receipt(confirmation: 'pending'|'confirmed_with_reservations' = 'confirmed_with_reservations') {
  return {challenge:{schema_version:'v1', created_at:'2026-10-03T00:00:00Z',source_refs:[],content_hash:'a'.repeat(64),
    turn_id:'test-turn',challenge_id:'test-challenge',thesis_id:'test-thesis',claims:['Test'],objections:['Unverified'],questions:['What evidence?'],missing_evidence:['Source'],
    rubric_scores:['premise_clarity','time_boundary','trigger_observability','causal_chain','counterfactuals','evidence_quality','asset_transmission','already_priced','falsifiability','user_confirmation'].map(criterion=>({criterion,score:3,rationale:'Synthetic test'})),
    unresolved_issues:['Source missing'],citation_ids:[],public_fact_citation_ids:[],native_search_used:false,provenance:'test',user_confirmation:confirmation,private_evidence_declared:false,status:confirmation==='pending'?'EVIDENCE_REQUIRED':'CONFIRMED'}};
}
function workspace(state=finished()) {return decodeWorkspace(encodeWorkspace(scope,state,ASSET_FIXTURES),scope)!;}
function reader(result=receipt(), events: unknown[] = []) {
  return {getChallenge:vi.fn().mockResolvedValue(result),streamOperationEvents:vi.fn(async function*(){yield* events;})} as unknown as NonNullable<Parameters<typeof verifyWorkspace>[1]>;
}
const signal=()=>new AbortController().signal;
describe('authoritative workspace recovery',()=>{
  it('reads the owner-bound receipt, never invokes a model, and preserves unsubmitted evidence',async()=>{
    const state={...finished(), evidenceDraft:{...initialState.evidenceDraft,title:'Unsubmitted source',excerpt:'Synthetic draft'},evidenceSearchQuery:'QA query'};
    const client=reader();const abort=signal();const restored=await verifyWorkspace(workspace(state),client,abort);
    expect(client.getChallenge).toHaveBeenCalledOnce();expect(client.getChallenge).toHaveBeenCalledWith('test-challenge',{operationId:'test-operation',signal:abort});
    expect(client.streamOperationEvents).not.toHaveBeenCalled();
    expect(restored.evidenceDraft).toEqual(state.evidenceDraft);expect(restored.evidenceSearchQuery).toBe('QA query');
    expect(restored.stage).toBe('nav');
  });
  it('does not trust a cached confirmation or current-data eligibility',async()=>{
    const restored=await verifyWorkspace(workspace(),reader(receipt('pending')),signal());
    expect(restored.confirmation).toBe('pending');expect(restored.serverBranches).toBeNull();expect(restored.fmResult).toBeNull();
    expect(canEnterStage(restored,'nav')).toBe(false);
  });
  it.each([403,404,410])('retains the local workspace if server returns %s',async status=>{
    const w=workspace();const before=encodeWorkspace(scope,w.state,w.assets);const client=reader();
    vi.mocked(client.getChallenge).mockRejectedValue(new Error(String(status)));
    await expect(verifyWorkspace(w,client,signal())).rejects.toThrow(String(status));
    expect(encodeWorkspace(scope,w.state,w.assets,new Date(w.savedAt))).toBe(encodeWorkspace(scope,decodeWorkspace(before,scope)!.state,w.assets,new Date(w.savedAt)));
  });
  it('replays an accepted reply once, fetches its result, and clears only the submitted reply',async()=>{
    const w=workspace({...finished('challenge'),apiPendingPhase:'reply',apiOperationId:'pending-op',challengeStatus:'submitting_reply'});
    const result=receipt();result.challenge.challenge_id='challenge:recovered';
    const client=reader(result,[{data:{event:'operation.complete',result_ref:'challenge:recovered'}}]);
    const restored=await verifyWorkspace(w,client,signal());
    expect(client.streamOperationEvents).toHaveBeenCalledOnce();expect(restored.apiChallengeId).toBe('challenge:recovered');
    expect(restored.apiCompletedOperationId).toBe('pending-op');expect(restored.replyDraft).toBe('');expect(restored.apiPendingPhase).toBeNull();
    expect(restored.challengeTurns.filter(t=>t.body==='Unsubmitted answer')).toHaveLength(1);
    const twice=await verifyWorkspace(workspace(restored),reader(result),signal());expect(twice.challengeTurns).toEqual(restored.challengeTurns);
  });
  it('restores last successful receipt after a failed reply and retains its answer draft',async()=>{
    const w=workspace({...finished('challenge'),apiPendingPhase:'reply',apiOperationId:'failed-op',challengeStatus:'submitting_reply'});
    const client=reader(receipt(),[{data:{event:'operation.error'}}]);const restored=await verifyWorkspace(w,client,signal());
    expect(restored.apiOperationId).toBe('test-operation');expect(restored.replyDraft).toBe('Unsubmitted answer');expect(restored.apiError).toContain('草稿');
  });
  it('retains inputs without pretending an initial failed request succeeded',async()=>{
    const w=workspace({...initialState,viewpoint:'Synthetic premise',apiPendingPhase:'initial',apiOperationId:'failed-op'});
    const client=reader(receipt(),[{data:{event:'operation.error'}}]);const restored=await verifyWorkspace(w,client,signal());
    expect(restored.viewpoint).toBe('Synthetic premise');expect(restored.challengeStatus).toBe('idle');expect(client.getChallenge).not.toHaveBeenCalled();
  });
  it.each([{events:[]},{events:[{data:{event:'operation.complete',result_ref:'unknown:result'}}]}])('rejects an incomplete/non-challenge event stream',async ({events})=>{
    await expect(verifyWorkspace(workspace({...finished(),apiPendingPhase:'reply'}),reader(receipt(),events),signal())).rejects.toThrow('terminal result');
  });
  it('does not resubmit when a reload preceded receipt acceptance',async()=>{
    await expect(verifyWorkspace(workspace({...finished(),challengeStatus:'submitting_reply'}),reader(),signal())).rejects.toThrow('automatic resubmission is disabled');
  });
  it('does not unlock worldlines when reload interrupted their generation',async()=>{
    const restored=await verifyWorkspace(workspace({...finished('challenge'),serverBranches:null,fmResult:null}),reader(),signal());
    expect(restored.confirmation).toBe('pending');expect(canEnterStage(restored,'worldline')).toBe(false);
  });
  it('rejects mismatched research identity',async()=>{
    const wrong=receipt();wrong.challenge.challenge_id='different';
    await expect(verifyWorkspace(workspace(),reader(wrong),signal())).rejects.toThrow('identity mismatch');
  });
  it('discards restoration when account/scope hydration was aborted',async()=>{
    const controller=new AbortController();controller.abort();
    await expect(verifyWorkspace(workspace(),reader(),controller.signal)).rejects.toThrow('Aborted');
  });
});
