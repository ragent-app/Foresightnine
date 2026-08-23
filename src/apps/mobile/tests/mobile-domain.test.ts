import { describe, expect, it } from "vitest";
import { computeFm } from "@foresightnine/fm-engine";
import { archiveIncludesPrivateEvidence, buildPrivacySafeDocument, canRestoreArchive, makeArchiveEnvelope } from "../src/archive";
import { isDuplicateEvidence, normalizeShareToBundle, normalizeSharedText } from "../src/evidence";
import { MOCK_EVIDENCE, fmInput, makeBranches } from "../src/fixtures";
import { t } from "../src/i18n";
import { createPortfolioPlan, publicWeights } from "../src/portfolio";
import { parseCapital, validateThesis } from "../src/validation";

describe("mobile thesis and evidence boundaries", () => {
  it("rejects invalid capital and enforces the thesis range", () => {
    expect(parseCapital("0")).toBeNull();
    expect(parseCapital("12,345.67")).toBe(12345.67);
    expect(validateThesis("short", "not-money")).toMatchObject({ viewpoint: expect.any(String), capital: expect.any(String) });
  });

  it("imports a safe https share without fetching and rejects unsafe schemes", () => {
    const share = normalizeShareToBundle("A report https://example.com/research?x=1");
    expect(share.ok).toBe(true);
    if (share.ok) expect(share.bundle?.url).toBe("https://example.com/research?x=1");
    expect(normalizeSharedText("javascript:alert(1)")).toMatchObject({ ok: false, reason: "unsupported_url" });
    if (share.ok && share.bundle) expect(isDuplicateEvidence([share.bundle], share.bundle)).toBe(true);
  });

  it("uses bilingual dictionary strings", () => {
    expect(t("zh", "start")).toContain("审查");
    expect(t("en", "start")).toBe("Start the challenge");
  });
});

describe("worldline, FM and Public Default", () => {
  it("retains auditable probability intervals and date anchors", () => {
    for (const branch of makeBranches("A testable premise")) {
      expect(branch.interval.low).toBeLessThan(branch.interval.high);
      expect(branch.conditional.low).toBeLessThan(branch.conditional.high);
      expect(branch.joint.low).toBeLessThan(branch.joint.high);
      expect(branch.nodes[0]?.occurrenceDate).toMatch(/^2026-/);
      expect(branch.nodes[0]?.revealDate).toMatch(/^2026-/);
    }
  });

  it("executes the shared client FM engine and applies public constraints separately", () => {
    const result = computeFm(fmInput());
    expect(result.weights.weights).toHaveLength(6);
    expect(result.selectedFactorIds).toEqual([10, 20, 30]);
    const safe = publicWeights(result.weights.weights);
    expect(safe).toHaveLength(6);
    expect(safe[4]).toBe(0); // stale / low-liquidity WORLD is excluded from Public Default
    expect(Math.max(...safe)).toBeLessThanOrEqual(0.2500001);
    const plan = createPortfolioPlan(result, 100, "CNY");
    expect(plan.rows.some((row) => row.reasons.some((reason) => reason.includes("资金太小")))).toBe(true);
  });
});

describe("privacy-safe archive envelope", () => {
  it("excludes private evidence by default and makes an explicit device-only opt-in visible", () => {
    const source = { title: "local", viewpoint: "private premise", capital: 100, currency: "CNY", selectedBranch: "main", evidence: MOCK_EVIDENCE };
    const publicDocument = buildPrivacySafeDocument(source);
    const localDocument = buildPrivacySafeDocument(source, true);
    expect(archiveIncludesPrivateEvidence(publicDocument)).toBe(false);
    expect(archiveIncludesPrivateEvidence(localDocument)).toBe(true);
    const envelope = makeArchiveEnvelope({ aad: "f9-archive-envelope-v1", combined: "sealed", createdAt: "2026-08-23T00:00:00Z", documentHash: "hash" });
    expect(canRestoreArchive(envelope)).toBe(true);
    expect(canRestoreArchive({ ...envelope, algorithm: "none" })).toBe(false);
  });
});
