import type { EvidenceBundle } from "./types";

export interface PrivacySafeDocument {
  readonly schema: "f9-privacy-safe-research-v1";
  readonly title: string;
  readonly viewpoint: string;
  readonly capital: number;
  readonly currency: string;
  readonly selectedBranch: string;
  readonly evidence: readonly EvidenceBundle[];
  readonly privatePremiseIncluded: boolean;
  readonly disclaimer: string;
}

export interface ArchiveEnvelope {
  readonly schema: "f9-archive-envelope-v1";
  readonly algorithm: "AES-256-GCM";
  readonly keyRef: "secure-store:f9.archive.key";
  readonly aad: string;
  readonly combined: string;
  readonly createdAt: string;
  readonly documentHash: string;
}

export function buildPrivacySafeDocument(input: { readonly title: string; readonly viewpoint: string; readonly capital: number; readonly currency: string; readonly selectedBranch: string; readonly evidence: readonly EvidenceBundle[] }, includePrivatePremise = false): PrivacySafeDocument {
  return {
    schema: "f9-privacy-safe-research-v1",
    title: input.title,
    viewpoint: input.viewpoint,
    capital: input.capital,
    currency: input.currency,
    selectedBranch: input.selectedBranch,
    // Private evidence is excluded unless the person explicitly opts in to a
    // device-local archive. It is never part of a public/shareable export.
    evidence: includePrivatePremise ? input.evidence : input.evidence.filter((item) => item.source_type !== "user_private"),
    privatePremiseIncluded: includePrivatePremise,
    disclaimer: "仅用于情景研究与教育，不构成投资建议；概率与组合均为 mock / 未校准。",
  };
}

export function makeArchiveEnvelope(parts: Pick<ArchiveEnvelope, "aad" | "combined" | "createdAt" | "documentHash">): ArchiveEnvelope {
  return { schema: "f9-archive-envelope-v1", algorithm: "AES-256-GCM", keyRef: "secure-store:f9.archive.key", ...parts };
}

export function canRestoreArchive(envelope: unknown): envelope is ArchiveEnvelope {
  if (!envelope || typeof envelope !== "object") return false;
  const value = envelope as Record<string, unknown>;
  return value.schema === "f9-archive-envelope-v1" && value.algorithm === "AES-256-GCM" && value.keyRef === "secure-store:f9.archive.key" && typeof value.aad === "string" && typeof value.combined === "string" && typeof value.documentHash === "string";
}

export function archiveIncludesPrivateEvidence(document: PrivacySafeDocument): boolean {
  return document.evidence.some((item) => item.source_type === "user_private");
}
