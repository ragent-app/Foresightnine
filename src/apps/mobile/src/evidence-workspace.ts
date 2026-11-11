import type { ProjectSearchCandidate } from "./search-client";

/** In-memory workspace belongs to AppProvider, not to a transient route component. */
export interface EvidenceWorkspace {
  readonly sharedText: string;
  readonly searchQuery: string;
  readonly candidates: readonly ProjectSearchCandidate[];
}
export const EMPTY_EVIDENCE_WORKSPACE: EvidenceWorkspace = { sharedText: "", searchQuery: "", candidates: [] };
export function patchEvidenceWorkspace(current: EvidenceWorkspace, patch: Partial<EvidenceWorkspace>): EvidenceWorkspace {
  // A changed query must not continue presenting results for the previous query.
  return { ...current, ...patch, ...(patch.searchQuery !== undefined && patch.searchQuery !== current.searchQuery ? { candidates: [] } : {}) };
}
