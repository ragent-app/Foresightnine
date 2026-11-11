import { expect,it,vi } from 'vitest';
import { ApiError, ApiTransportError, type ApiClient } from '@foresightnine/api-client';
import { completeWorldlineFlow } from './worldline-flow';

function setup(confirmed=true){
  const store=new Map<string,string>();
  const storage={getItem:(k:string)=>store.get(k)??null,setItem:(k:string,v:string)=>{store.set(k,v)}};
  const receipt={challenge:{status:confirmed?'CONFIRMED':'CHALLENGING',user_confirmation:'confirmed_with_reservations'},operation:{operation_id:'operation:confirmed'}};
  const client={getChallenge:vi.fn().mockResolvedValue(receipt),confirmChallenge:vi.fn().mockResolvedValue(receipt),createWorldlines:vi.fn().mockResolvedValue({bundle:{worldlines:[]}})};
  const options={operationId:'operation:receipt',storage,signal:new AbortController().signal,onConfirmed:vi.fn()};
  const run=()=>completeWorldlineFlow(client as unknown as ApiClient,'challenge:test',{command:'CONFIRM_WITH_RESERVATIONS',unresolved_issues_acknowledged:true,reservation_note:'QA'},options);
  return {client,options,run,store};
}
it('resumes confirmed research without issuing an illegal second confirmation',async()=>{
  const {client,run,options}=setup();await run();
  expect(client.getChallenge).toHaveBeenCalledWith('challenge:test',expect.objectContaining({operationId:'operation:receipt'}));
  expect(client.confirmChallenge).not.toHaveBeenCalled();expect(options.onConfirmed).toHaveBeenCalledOnce();expect(client.createWorldlines).toHaveBeenCalledOnce();
});
it('confirms once before worldline generation when needed',async()=>{
  const {client,run}=setup(false);await run();
  expect(client.confirmChallenge).toHaveBeenCalledOnce();
  expect(client.confirmChallenge.mock.invocationCallOrder[0]).toBeLessThan(client.createWorldlines.mock.invocationCallOrder[0]!);
});
it.each(['network','gateway','abort','malformed'] as const)('retains request identity after %s uncertainty',async(kind)=>{
  const {client,run}=setup();
  const error=kind==='network'?new ApiTransportError('network'):kind==='gateway'?new ApiError(524,null):kind==='abort'?new DOMException('Aborted','AbortError'):new Error('Malformed response');
  client.createWorldlines.mockRejectedValueOnce(error);await expect(run()).rejects.toBe(error);await run();
  expect(client.createWorldlines.mock.calls[0]![1].idempotencyKey).toEqual(client.createWorldlines.mock.calls[1]![1].idempotencyKey);
});
it('permits a fresh explicit attempt only after authoritative failure',async()=>{
  const {client,run}=setup();
  const error=new ApiError(503,{schema_version:'v1',error_code:'internal_error',message:'Failed',request_id:'request:known',retryable:true,details:[]});
  client.createWorldlines.mockRejectedValueOnce(error);await expect(run()).rejects.toBe(error);await run();
  expect(client.createWorldlines.mock.calls[0]![1].idempotencyKey).not.toEqual(client.createWorldlines.mock.calls[1]![1].idempotencyKey);
});
it('writes replay identity before any potentially billable request',async()=>{
  const {client,run,options}=setup();options.storage.setItem=()=>{throw new Error('Storage unavailable')};
  await expect(run()).rejects.toThrow('Storage unavailable');expect(client.createWorldlines).not.toHaveBeenCalled();
});
it('reuses saved identity in a fresh controller after refresh',async()=>{
  const {client,run,options}=setup();await run();
  await completeWorldlineFlow(client as unknown as ApiClient,'challenge:test',{command:'CONFIRM',unresolved_issues_acknowledged:true},{...options,signal:new AbortController().signal});
  expect(client.createWorldlines.mock.calls[0]![1].idempotencyKey).toEqual(client.createWorldlines.mock.calls[1]![1].idempotencyKey);
});

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ChallengePanel } from './components/ChallengePanel';
import { LanguageProvider } from './i18n';
import { initialState } from './state';
it('shows generation progress and disables duplicate confirmation controls',()=>{
  const noop=()=>{};
  const html=renderToStaticMarkup(createElement(LanguageProvider,{language:'zh',setLanguage:noop},createElement(ChallengePanel,{
    status:'complete',turns:[{id:'one',author:'user',time:'now',body:'A bounded scenario'}],replyDraft:'',evidence:[],rubric:initialState.rubric,
    unresolvedIssuesAcknowledged:true,reservationNote:'QA',worldlineSubmitting:true,
    onStart:noop,onCancel:noop,onRetry:noop,onRecover:noop,onReplyDraft:noop,onSubmitReply:noop,onEvidence:noop,onUnresolvedIssuesAcknowledged:noop,onReservationNote:noop,onConfirm:noop,
  })));
  expect(html).toContain('正在生成三条世界线');
  expect(html).toMatch(/<button[^>]*disabled=""[^>]*>继续生成世界线/);
  expect(html).toMatch(/<button[^>]*disabled=""[^>]*>带保留意见确认/);
});

it('replays an uncertain confirmation using the same operation identity',async()=>{
  const {client,run}=setup(false);
  client.confirmChallenge.mockRejectedValueOnce(new ApiTransportError('network'));
  await expect(run()).rejects.toBeInstanceOf(ApiTransportError);await run();
  expect(client.confirmChallenge.mock.calls[0]![2].idempotencyKey).toEqual(client.confirmChallenge.mock.calls[1]![2].idempotencyKey);
});
