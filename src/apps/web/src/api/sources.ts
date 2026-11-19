import type { ResearchV2 } from "@foresightnine/contracts";
import { apiRequest } from "./http";
import type { CreatedSource, HtmlUploadRequest, SecIdentitySearchResult } from "./types";

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
