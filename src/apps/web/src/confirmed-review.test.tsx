import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it,vi} from 'vitest';
import {ChallengePanel} from './components/ChallengePanel';
import {initialState,reducer,rubricDefaults,canEnterStage} from './state';
import {privateEvidenceFixture} from './fixtures';
import {decodeWorkspace,encodeWorkspace,verifyWorkspace,workspaceScope} from './research-workspace';
import type {AppState} from './types';
const noop=()=>undefined;
const turns=[{id:'u1',author:'user' as const,time:'now',body:'Synthetic thesis'},{id:'r1',author:'challenger' as const,time:'now',body:'Question',review:{questions:['What would falsify this?'],objections:['Source missing'],missingEvidence:['Public record']}},{id:'u2',author:'user' as const,time:'now',body:'I retain uncertainty'}];
const props={status:'complete' as const,turns,replyDraft:'Unsubmitted answer',evidence:[],rubric:rubricDefaults,unresolvedIssuesAcknowledged:true,reservationNote:'Preserve unresolved conditions',onStart:noop,onCancel:noop,onRetry:noop,onRecover:noop,onReplyDraft:noop,onSubmitReply:noop,onEvidence:noop,onUnresolvedIssuesAcknowledged:noop,onReservationNote:noop,onConfirm:noop};
const confirmed:AppState={...initialState,stage:'challenge',viewpoint:'Synthetic thesis',challengeStatus:'complete',challengeTurns:turns,confirmation:'reserved',unresolvedIssuesAcknowledged:true,reservationNote:'Preserve unresolved conditions'};
describe('confirmed review is a read-only record',()=>{
 it('shows completed status rather than answer/decision forms',()=>{const h=renderToStaticMarkup(createElement(ChallengePanel,{...props,confirmation:'reserved',worldlinesReady:true,onOpenWorldlines:noop}));expect(h).toContain('已确认 · 含保留意见');expect(h).toContain('查看世界线');expect(h).not.toContain('<form');expect(h).not.toContain('等待你的答复');expect(h).not.toContain('决策门槛未满足');});
 it('retains structured review, reservations and unsent drafts without submitting them',()=>{const h=renderToStaticMarkup(createElement(ChallengePanel,{...props,confirmation:'reserved',savedEvidenceDraft:{...initialState.evidenceDraft,title:'Unsaved source title',excerpt:'QA-only excerpt'}}));expect(h).toContain('What would falsify this?');expect(h).toContain('Preserve unresolved conditions');expect(h).toContain('Unsubmitted answer');expect(h).toContain('Unsaved source title');expect(h).toContain('未提交草稿');expect(h).not.toContain('<textarea');});
 it('keeps generation recovery without claiming a bundle already exists',()=>{const h=renderToStaticMarkup(createElement(ChallengePanel,{...props,confirmation:'confirmed',worldlinesReady:false}));expect(h).toContain('审查已确认');expect(h).toContain('继续生成世界线');expect(h).not.toContain('查看世界线');expect(h).not.toContain('提交回复');});
 it.each([
 {type:'submit_reply',body:'unexpected network-bound edit'},
 {type:'begin_reply_submit',body:'unexpected network-bound edit'},
 {type:'add_evidence',evidence:privateEvidenceFixture},
 {type:'set_reservation_note',note:'overwrite confirmed note'},
 {type:'set_unresolved_issues_acknowledged',acknowledged:false},
 ] as const)('rejects mutation of a completed record: $type',action=>{expect(reducer(confirmed,action)).toBe(confirmed);});
 it('keeps an unconfirmed review editable',()=>{const h=renderToStaticMarkup(createElement(ChallengePanel,props));expect(h).toContain('<form');expect(h).toContain('提交回复');});
});

const scope=workspaceScope('https://example.test','synthetic-account');
function receipt(status='CONFIRMED',confirmation='confirmed_with_reservations') {
 return {challenge:{schema_version:'v1',created_at:'2026-10-03T00:00:00Z',source_refs:[],content_hash:'a'.repeat(64),turn_id:'synthetic-turn',challenge_id:'synthetic-challenge',thesis_id:'synthetic-thesis',claims:['Synthetic premise'],objections:['Missing source'],questions:['What evidence?'],missing_evidence:['Public source'],rubric_scores:['premise_clarity','time_boundary','trigger_observability','causal_chain','counterfactuals','evidence_quality','asset_transmission','already_priced','falsifiability','user_confirmation'].map(criterion=>({criterion,score:2,rationale:'Synthetic only'})),unresolved_issues:['Missing evidence'],citation_ids:[],public_fact_citation_ids:[],native_search_used:false,provenance:'test',user_confirmation:confirmation,private_evidence_declared:false,status}};
}
function client(response=receipt()) {return {getChallenge:vi.fn().mockResolvedValue(response),streamOperationEvents:vi.fn()} as unknown as NonNullable<Parameters<typeof verifyWorkspace>[1]>;}
function workspace(){return decodeWorkspace(encodeWorkspace(scope,{...confirmed,apiChallengeId:'synthetic-challenge',apiOperationId:'synthetic-operation'},[]),scope)!;}
it('recovers a confirmed review even when worldline generation was interrupted',async()=>{
 const w=workspace();const api=client();const restored=await verifyWorkspace(w,api,new AbortController().signal);
 expect(restored.reviewConfirmation).toBe('reserved');expect(restored.confirmation).toBe('pending');expect(restored.serverBranches).toBeNull();expect(canEnterStage(restored,'worldline')).toBe(false);
 expect(reducer(restored,{type:'begin_reply_submit',body:'forbidden reply'})).toBe(restored);
 expect(api.getChallenge).toHaveBeenCalledOnce();expect(api.streamOperationEvents).not.toHaveBeenCalled();
});
it('does not accept user_confirmation when the server lifecycle is not CONFIRMED',async()=>{
 const restored=await verifyWorkspace(workspace(),client(receipt('EVIDENCE_REQUIRED')),new AbortController().signal);
 expect(restored.reviewConfirmation).toBe('pending');expect(restored.confirmation).toBe('pending');
});
it('upgrades an older workspace without deleting its drafts',()=>{
 const text=encodeWorkspace(scope,{...confirmed,replyDraft:'Preserved prior answer'},[]);
 const old=JSON.parse(text);delete old.state.reviewConfirmation;
 const restored=decodeWorkspace(JSON.stringify(old),scope);
 expect(restored?.state.reviewConfirmation).toBe('pending');expect(restored?.state.replyDraft).toBe('Preserved prior answer');
});
it('retains the server confirmation receipt after generation failure',()=>{
 const receiptState=reducer({...confirmed,confirmation:'pending'},{type:'api_review_confirmed',confirmation:'reserved'});
 const failed=reducer(receiptState,{type:'api_error',notice:'Provider timed out'});
 expect(failed.reviewConfirmation).toBe('reserved');expect(failed.reservationNote).toBe(confirmed.reservationNote);expect(failed.serverBranches).toBeNull();
});

it('disables generation recovery while the accepted attempt is still running',()=>{
 const h=renderToStaticMarkup(createElement(ChallengePanel,{...props,confirmation:'reserved',worldlinesReady:false,worldlineSubmitting:true}));
 expect(h).toContain('正在生成世界线');expect(h).toMatch(/<button[^>]*disabled=""[^>]*>继续生成世界线/);expect(h).not.toContain('提交回复');
});
it('rejects an invalid persisted confirmation enum',()=>{
 const value=JSON.parse(encodeWorkspace(scope,confirmed,[]));value.state.reviewConfirmation='looks-confirmed';expect(decodeWorkspace(JSON.stringify(value),scope)).toBeNull();
});

it('keeps the v1 persisted workspace readable by the rollback deployment',()=>{
 const text=encodeWorkspace(scope,{...confirmed,reviewConfirmation:'reserved',replyDraft:'Keep draft on rollback'},[]);
 const raw=JSON.parse(text);expect(raw.state).not.toHaveProperty('reviewConfirmation');expect(raw.state.replyDraft).toBe('Keep draft on rollback');
});
