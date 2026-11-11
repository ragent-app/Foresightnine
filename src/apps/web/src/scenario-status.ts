import type { BranchView } from './types';

/** Preserve old drafts, but don't propagate a known unguarded live scenario into
 * fresh downstream calculations. Offline fixtures remain explicitly fixtures. */
export function legacyLiveScenarios(branches: readonly BranchView[] | null | undefined): boolean {
  return Boolean(branches?.some(branch => branch.worldline.provenance.includes('live account-scoped')
    && !branch.worldline.provenance.includes('premise_guard=v2')));
}
