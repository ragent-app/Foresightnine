import { describe, expect, it } from "vitest";
import { selectedCandidateDraft } from "./components/ChallengePanel";
import { evidenceHash } from "./fixtures";
import { configuredApiBaseUrlFromEnvironment } from "./runtime-config";
import { clientBundleFallback, searchEvidenceCandidates } from "./search-client";
import type { EvidenceDraft, SearchCandidate } from "./types";

const candidate: SearchCandidate = {
  search_result_hash: "a".repeat(64),
  search_provider: "project-search",
  title: "Grid investment filing",
  url: "https://example.com/filing",
  excerpt: "A dated public excerpt.",
  publisher: "Example regulator",
  published_at: "2026-08-01T00:00:00Z",
  retrieved_at: "2026-08-09T00:00:00Z",
  independently_reviewable: true,
};

const emptyDraft: EvidenceDraft = { url: "", title: "", publisher: "", published_at: "", excerpt: "", user_note: "", supports_or_challenges: "supports", grade: "B" };

describe("project search candidate boundary", () => {
  it("prefers the canonical API variable and preserves the legacy alias", () => {
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "https://canonical.example", legacy: "https://legacy.example" })).toBe("https://canonical.example");
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "", legacy: "https://legacy.example" })).toBe("https://legacy.example");
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "not-a-url", legacy: "https://legacy.example" })).toBeNull();
  });

  it("falls back without a configured public API and does not call fetch", async () => {
    let calls = 0;
    const result = await searchEvidenceCandidates("grid demand", { fetcher: (async () => { calls += 1; throw new Error("must not fetch"); }) as typeof fetch });
    expect(result).toEqual(clientBundleFallback());
    expect(calls).toBe(0);
  });

  it("uses the candidate endpoint with omitted credentials and retains only safe candidates", async () => {
    let request: RequestInfo | URL | undefined;
    let init: RequestInit | undefined;
    const response = {
      schema_version: "v1", provider: "project-search", project_search_available: true, fallback: "client_bundle",
      candidates: [candidate, { ...candidate, search_result_hash: "b".repeat(64), url: "https://127.0.0.1/private" }],
    };
    const result = await searchEvidenceCandidates("grid demand", {
      apiBaseUrl: "https://api.example.com",
      fetcher: (async (input: RequestInfo | URL, requestInit?: RequestInit) => { request = input; init = requestInit; return new Response(JSON.stringify(response), { status: 200 }); }) as typeof fetch,
    });
    expect(String(request)).toBe("https://api.example.com/v1/search/candidates?query=grid+demand&limit=10");
    expect(init).toMatchObject({ method: "GET", credentials: "omit", headers: { Accept: "application/json" } });
    expect(init?.headers).not.toHaveProperty("Authorization");
    expect(result).toMatchObject({ provider: "project-search", project_search_available: true, candidates: [expect.objectContaining({ url: candidate.url })] });
    expect(result.candidates).toHaveLength(1);
  });

  it("requires an explicit selection and binds its provenance into the audit hash", () => {
    const selected = selectedCandidateDraft(emptyDraft, candidate, "en");
    expect(selected).toMatchObject({ search_provider: candidate.search_provider, search_result_hash: candidate.search_result_hash, grade: "C", supports_or_challenges: "context" });
    const base = { evidence_id: "e1", challenge_id: "c1", source_type: "commentary" as const, url: selected.url, title: selected.title, publisher: selected.publisher, published_at: selected.published_at, retrieved_at: "2026-08-10T00:00:00Z", excerpt: selected.excerpt, user_note: selected.user_note, grade: selected.grade, supports_or_challenges: selected.supports_or_challenges, independently_reviewable: true };
    expect(evidenceHash({ ...base, search_provider: candidate.search_provider, search_result_hash: candidate.search_result_hash })).not.toBe(evidenceHash(base));
  });
});
