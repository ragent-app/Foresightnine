import { buildResearchSnapshot, normalizeResearchSnapshot, restoreResearchState } from "./archive";
import { researchOwnerHash } from "./research-owner";
import type { AppState, AssetFixture, Branch, ResearchMethodData, ResearchSnapshot } from "./types";
import type { EvidenceWorkspace } from "./evidence-workspace";
import type { ScenarioDrafts } from "./scenario-inputs";

/** Device-private only. This format is never passed to the public export/share API. */
export interface WorkspaceDocument {
  readonly schema: "f9-mobile-workspace-v1";
  readonly research: ResearchSnapshot;
  readonly apiChallengeId: string | null;
  readonly apiOperationId?: string | null;
  readonly evidenceWorkspace: EvidenceWorkspace;
  readonly scenarioDrafts: ScenarioDrafts;
  readonly assets: readonly AssetFixture[];
}
const MAX_BYTES = 1024 * 1024;
const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown, max = 20_000): v is string => typeof v === "string" && v.length <= max && !v.includes("\0");
const only = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).every(key => keys.includes(key));
const date = (v: unknown) => text(v, 100) && Number.isFinite(Date.parse(v));
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
function validEvidenceWorkspace(v: unknown): v is EvidenceWorkspace {
  if (!record(v) || !only(v, ["sharedText", "searchQuery", "candidates"]) || !text(v.sharedText) || !text(v.searchQuery, 2000) || !Array.isArray(v.candidates) || v.candidates.length > 10) return false;
  return v.candidates.every(c => {
    if (!record(c) || !only(c, ["searchResultHash", "searchProvider", "title", "url", "excerpt", "publisher", "publishedAt", "retrievedAt", "independentlyReviewable"]) || !text(c.searchResultHash, 64) || !/^[a-f0-9]{64}$/.test(c.searchResultHash) || !text(c.searchProvider, 200) || !text(c.title, 500) || !text(c.excerpt, 10_000) || !(c.publisher === null || text(c.publisher, 500)) || !(c.publishedAt === null || date(c.publishedAt)) || !date(c.retrievedAt) || typeof c.independentlyReviewable !== "boolean" || !text(c.url, 4000)) return false;
    try { const u = new URL(c.url); return ["https:", "http:"].includes(u.protocol) && !u.username && !u.password; } catch { return false; }
  });
}
function validAssets(v: unknown): v is readonly AssetFixture[] {
  return Array.isArray(v) && v.length > 0 && v.length <= 100 && new Set(v.map(a => a?.id)).size === v.length && v.every(a => record(a) && only(a, ["id", "name", "role", "instrument", "quoteCurrency", "price", "lotSize", "fractional", "tradable", "proxy", "liquidity", "volatility", "assetClass", "asOf", "freshness"]) && text(a.id, 200) && a.id.length > 0 && text(a.name, 200) && text(a.role, 1000) && text(a.instrument, 200) && ["CNY", "USD", "EUR"].includes(String(a.quoteCurrency)) && finite(a.price) && a.price > 0 && finite(a.lotSize) && a.lotSize > 0 && typeof a.fractional === "boolean" && typeof a.tradable === "boolean" && (a.proxy === undefined || text(a.proxy, 200)) && ["high", "medium", "low"].includes(String(a.liquidity)) && ["low", "high"].includes(String(a.volatility)) && ["equity", "defensive", "commodity", "cash"].includes(String(a.assetClass)) && date(a.asOf) && ["current", "historical_only", "stale"].includes(String(a.freshness)));
}
function validScenarios(v: unknown, assets: readonly AssetFixture[]): v is ScenarioDrafts {
  if (!record(v) || !only(v, ["weak", "main", "strong"])) return false;
  const ids = new Set(assets.filter(a => a.assetClass !== "cash").map(a => a.id));
  return Object.values(v).every(branch => record(branch) && Object.entries(branch).every(([id, value]) => ids.has(id) && !["__proto__", "constructor", "prototype"].includes(id) && text(value, 24)));
}
export function normalizeWorkspaceDocument(value: unknown): WorkspaceDocument | null {
  if (!record(value) || !only(value, ["schema", "research", "apiChallengeId", "apiOperationId", "evidenceWorkspace", "scenarioDrafts", "assets"]) || value.schema !== "f9-mobile-workspace-v1" || !(value.apiChallengeId === null || text(value.apiChallengeId, 200) && value.apiChallengeId.length > 0) || !(value.apiOperationId === undefined || value.apiOperationId === null || text(value.apiOperationId, 160) && value.apiOperationId.length >= 8) || !validAssets(value.assets) || !validEvidenceWorkspace(value.evidenceWorkspace) || !validScenarios(value.scenarioDrafts, value.assets)) return null;
  const research = normalizeResearchSnapshot(value.research, { draftInputs: true });
  if (!research || research.methodData.assetIds.join("\0") !== value.assets.map(a => a.id).join("\0") || research.fmResult && research.fmResult.weights.length !== value.assets.length) return null;
  return { schema: "f9-mobile-workspace-v1", research, apiChallengeId: value.apiChallengeId, apiOperationId: typeof value.apiOperationId === "string" ? value.apiOperationId : null, evidenceWorkspace: value.evidenceWorkspace, scenarioDrafts: value.scenarioDrafts, assets: value.assets };
}
export function canRestoreWorkspaceDocument(value: unknown): value is WorkspaceDocument { return normalizeWorkspaceDocument(value) !== null; }
export function serializeWorkspaceDocument(value: WorkspaceDocument): Uint8Array {
  const json = JSON.stringify(value); const bytes = new TextEncoder().encode(json);
  if (bytes.length > MAX_BYTES) throw new Error("workspace_too_large");
  if (!canRestoreWorkspaceDocument(JSON.parse(json))) throw new Error("workspace_invalid");
  return bytes;
}
export function buildWorkspaceDocument(input: { state: AppState; branches: readonly Branch[]; methodData: ResearchMethodData; apiChallengeId: string | null; apiOperationId?: string | null; evidenceWorkspace: EvidenceWorkspace; scenarioDrafts: ScenarioDrafts; assets: readonly AssetFixture[] }): WorkspaceDocument {
  // Preserve actual inputs, private evidence and intermediate replies; normalize only on restore.
  const document: WorkspaceDocument = { schema: "f9-mobile-workspace-v1", research: buildResearchSnapshot(input, true), apiChallengeId: input.apiChallengeId, apiOperationId: input.apiOperationId ?? null, evidenceWorkspace: input.evidenceWorkspace, scenarioDrafts: input.scenarioDrafts, assets: input.assets };
  return JSON.parse(new TextDecoder().decode(serializeWorkspaceDocument(document))) as WorkspaceDocument;
}
export function restoreWorkspaceState(current: AppState, document: WorkspaceDocument): AppState {
  const normalized = normalizeWorkspaceDocument(document);
  if (!normalized) throw new Error("workspace_invalid");
  return { ...restoreResearchState(current, normalized.research), exportNotice: "", freshness: { ...current.freshness, providerStatus: "unavailable", fxProviderStatus: "unavailable" } };
}
export function workspaceStorageScope(account: string, apiBaseUrl: string | null): string { return `workspace:${researchOwnerHash(apiBaseUrl ?? "local")}:${account}`; }
export function scenarioWorkspaceScope(accountId: string, challengeId: string | null, state: AppState): string { return JSON.stringify([accountId, challengeId, state.viewpoint, state.branchSnapshot?.map(branch => [branch.baselineDate, branch.horizonEnd, branch.nodes.map(node => node.id)])]); }
