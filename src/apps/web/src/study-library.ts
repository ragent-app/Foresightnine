import { decodeWorkspace, encodeWorkspace, type ResearchWorkspace } from './research-workspace';
import { canEnterStage } from './state';
import type { AppState, AssetFixture } from './types';
export interface StudyEntry { readonly id: string; readonly snapshot: string; readonly workspace: ResearchWorkspace; }
export type StudyStorage = Pick<Storage, 'getItem'|'setItem'>;
export const MAX_STUDIES = 30;
const MAX_BYTES = 12 * 1024 * 1024;
export const studiesKey = (scope: string): string => `foresightnine.web.studies.v1:${encodeURIComponent(scope)}`;
export function hasResearchProgress(state: AppState): boolean {
  return Boolean(state.apiSessionId || state.apiChallengeId || state.challengeTurns.length || state.evidence.length || state.serverBranches || state.fmResult);
}
export function hasResearchContent(state: AppState): boolean {
  return Boolean(state.viewpoint.trim() || hasResearchProgress(state) || state.replyDraft.trim() || state.evidenceDraft.title.trim() || state.evidenceDraft.excerpt.trim());
}
function sameResearch(a: AppState, b: AppState): boolean {
  if (a.apiSessionId || b.apiSessionId) return Boolean(a.apiSessionId && a.apiSessionId === b.apiSessionId);
  if (a.apiChallengeId || b.apiChallengeId) return Boolean(a.apiChallengeId && a.apiChallengeId === b.apiChallengeId);
  // Drafts/offline studies have no server identity: exact premise + input budget
  // identifies a branch, not a fuzzy title or a model-generated label.
  return a.viewpoint === b.viewpoint && a.capital === b.capital && a.currency === b.currency;
}
export function readStudies(storage: StudyStorage, scope: string): readonly StudyEntry[] {
  const text=storage.getItem(studiesKey(scope));
  if (text===null) return [];
  if (text.length>MAX_BYTES) throw new Error('Study library too large');
  const value:unknown=JSON.parse(text);
  const object=(v:unknown):v is Record<string,unknown>=>typeof v==='object' && v!==null && !Array.isArray(v);
  if (!object(value) || value.version!==1 || value.scope!==scope || !Array.isArray(value.entries) || value.entries.length>MAX_STUDIES) throw new Error('Invalid study library');
  const ids=new Set<string>();
  const entries=value.entries.map((e:unknown):StudyEntry=>{
    if (!object(e) || typeof e.id!=='string' || !/^[a-zA-Z0-9-]{1,80}$/.test(e.id) || ids.has(e.id) || typeof e.snapshot!=='string') throw new Error('Invalid study entry');
    const workspace=decodeWorkspace(e.snapshot,scope);
    if (!workspace) throw new Error('Invalid study snapshot');
    ids.add(e.id);return {id:e.id,snapshot:e.snapshot,workspace};
  });
  return entries.sort((a,b)=>b.workspace.savedAt.localeCompare(a.workspace.savedAt));
}
/** One localStorage replacement commits the complete library or leaves it intact.
 * No pruning on quota failure: callers must abort destructive navigation/editing. */
export function saveStudy(storage: StudyStorage, scope: string, snapshot: string, newId: ()=>string=()=>crypto.randomUUID()): readonly StudyEntry[] {
  const workspace=decodeWorkspace(snapshot,scope);
  if (!workspace) throw new Error('Invalid study snapshot');
  const entries=readStudies(storage,scope);
  if (!hasResearchContent(workspace.state)) return entries;
  const existing=entries.find(e=>sameResearch(e.workspace.state,workspace.state));
  if (!existing && entries.length>=MAX_STUDIES) throw new Error('Study library full');
  const id=existing?.id ?? newId();
  if (!/^[a-zA-Z0-9-]{1,80}$/.test(id) || (!existing && entries.some(e=>e.id===id))) throw new Error('Invalid study identity');
  const next=[{id,snapshot,workspace},...entries.filter(e=>e.id!==id)];
  const text=JSON.stringify({version:1,scope,entries:next.map(({id,snapshot})=>({id,snapshot}))});
  if (text.length>MAX_BYTES) throw new Error('Study library too large');
  storage.setItem(studiesKey(scope),text);
  return next;
}
export function saveCurrentStudy(storage: StudyStorage, scope: string, state: AppState, assets: readonly AssetFixture[], now?: Date): readonly StudyEntry[] {
  return saveStudy(storage,scope,encodeWorkspace(scope,state,assets,now));
}

export function reachedStudyStage(state: AppState): AppState['stage'] {
  for (const stage of ['nav','portfolio','fm','worldline'] as const) if (canEnterStage(state,stage)) return stage;
  return hasResearchProgress(state) ? 'challenge' : 'thesis';
}
