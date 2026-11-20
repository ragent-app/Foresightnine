import type { ResearchV2 } from "@foresightnine/contracts";
import { apiRequest } from "./http";
import { useQuery } from "@tanstack/react-query";
import type { CreatedSource, HtmlUploadRequest, ResearchCapabilities, SecIdentitySearchResult, SourcePermissionFlags } from "./types";

// Source creation, attachment and original download. Each function mirrors one
// route in apps/api/app/main.py (create_research_source … get_research_source_original).

const enc = encodeURIComponent;

export function createTextSource(body: ResearchV2.SourceInput): Promise<CreatedSource> {
  return apiRequest<CreatedSource>("/v2/research/sources", { method: "POST", body });
}

export function uploadPdfSource(body: ResearchV2.PdfUploadRequest): Promise<CreatedSource> {
  return apiRequest<CreatedSource>("/v2/research/sources/pdf", { method: "POST", body });
}

export function uploadHtmlSource(body: HtmlUploadRequest): Promise<CreatedSource> {
  return apiRequest<CreatedSource>("/v2/research/sources/html", { method: "POST", body });
}

export interface StreamUploadFields {
  title: string;
  permissions: SourcePermissionFlags;
  is_simulated?: boolean;
  published_at?: string;
  event_at?: string;
  revised_at?: string;
  text_order?: "layout" | "reading";
}

/** Large documents (and plain text): multipart, streamed to disk by the API; POST /v2/research/sources/upload. */
export function uploadDocumentStream(file: File, fields: StreamUploadFields, signal?: AbortSignal): Promise<CreatedSource> {
  const form = new FormData();
  form.set("title", fields.title);
  form.set("permissions", JSON.stringify(fields.permissions));
  if (fields.is_simulated) form.set("is_simulated", "true");
  for (const key of ["published_at", "event_at", "revised_at", "text_order"] as const) {
    const value = fields[key];
    if (value) form.set(key, value);
  }
  form.set("file", file, file.name); // last, so the API has every field before the body
  return apiRequest<CreatedSource>("/v2/research/sources/upload", { method: "POST", form, ...(signal ? { signal } : {}) });
}

export function fetchOfficialSource(body: ResearchV2.FetchSourceRequest): Promise<CreatedSource> {
  return apiRequest<CreatedSource>("/v2/research/sources/fetch", { method: "POST", body });
}

export function searchSecIdentities(query: string, signal?: AbortSignal): Promise<SecIdentitySearchResult> {
  return apiRequest<SecIdentitySearchResult>("/v2/research/identities/sec-search", {
    method: "POST",
    body: { query },
    ...(signal ? { signal } : {}),
  });
}

export function fetchSecConceptSource(body: ResearchV2.SecConceptRequest): Promise<CreatedSource> {
  return apiRequest<CreatedSource>("/v2/research/sources/sec-concept", { method: "POST", body });
}

/** Adds sources to a topic; the backend records this as a new user version. */
export function attachSources(topicId: string, parentVersionId: string, sourceIds: readonly string[]): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`/v2/research/topics/${enc(topicId)}/sources`, {
    method: "POST",
    body: { parent_version_id: parentVersionId, source_ids: sourceIds },
  });
}

export function reextractPdfSource(sourceId: string, textOrder: "layout" | "reading", idempotencyKey: string): Promise<CreatedSource> {
  return apiRequest<CreatedSource>(`/v2/research/sources/${enc(sourceId)}/reextract`, {
    method: "POST",
    body: { text_order: textOrder },
    idempotencyKey,
  });
}

export async function downloadSourceOriginal(sourceId: string): Promise<{ blob: Blob; filename: string }> {
  const response = await apiRequest<Response>(`/v2/research/sources/${enc(sourceId)}/original`, { accept: "*/*", raw: true });
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return { blob: await response.blob(), filename: match?.[1] ?? "source" };
}

/** GET /v2/research/capabilities: which source capabilities the deployment has configured. */
export function getResearchCapabilities(signal?: AbortSignal): Promise<ResearchCapabilities> {
  return apiRequest<ResearchCapabilities>("/v2/research/capabilities", signal ? { signal } : {});
}

export function useResearchCapabilities() {
  return useQuery({ queryKey: ["researchCapabilities"], queryFn: ({ signal }) => getResearchCapabilities(signal), staleTime: 5 * 60 * 1000 });
}

/** SEC availability; unknown (still loading or failed) is treated as available and the backend decides. */
export function useSecAvailable(): boolean {
  return useResearchCapabilities().data?.sec_available ?? true;
}

/** Open public HTTPS web fetch availability; unknown defaults to enabled and the backend decides. */
export function useWebFetchAvailable(): boolean {
  return useResearchCapabilities().data?.web_fetch_enabled ?? true;
}
