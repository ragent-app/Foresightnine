import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactNode } from "react";
import type { AppState, Branch } from "../src/types";
const ui = vi.hoisted(() => ({ state: null as AppState | null, branches: [] as readonly Branch[], navigate: vi.fn() }));
vi.mock("../src/app-store", () => ({ useMobileStore: () => ({ state: ui.state, branches: ui.branches, portfolio: null }) }));
vi.mock("../src/oauth-session", () => ({ useOAuthSession: () => ({}) }));
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
vi.mock("expo-router", () => ({ router: { navigate: ui.navigate }, usePathname: () => "/challenge" }));
vi.mock("react-native", () => ({ Alert: {}, Pressable: "Pressable", ScrollView: "ScrollView", Text: "Text", View: "View", StyleSheet: { create: (value: unknown) => value }, useWindowDimensions: () => ({ width: 390, height: 844 }) }));
vi.mock("react-native-safe-area-context", () => ({ SafeAreaView: "SafeAreaView" }));
import { researchStageAvailability, researchStageRoute } from "../src/stage-navigation";
import { EMPTY_EVIDENCE_WORKSPACE, patchEvidenceWorkspace } from "../src/evidence-workspace";
import { mobileInitialState } from "../src/lifecycle";
import { StageRail } from "../src/ui";
import PortfolioScreen from "../app/portfolio";
import WorldlineScreen from "../app/worldline";
import { makeBranches, fmInput } from "../src/fixtures";
import { computeFm } from "@foresightnine/fm-engine";

const reviewed: AppState = { ...mobileInitialState, stage: "worldline", challengeStatus: "complete", challengeTurns: [{ id: "s", author: "system", body: "Synthetic test" }, { id: "c", author: "challenger", body: "Question?" }, { id: "u", author: "user", body: "Synthetic reply" }], confirmation: "reserved", unresolvedIssuesAcknowledged: true, reservationNote: "Synthetic reservation", replyDraft: "Unsent reply" };
const complete: AppState = { ...reviewed, stage: "portfolio", fmStatus: "complete", fmResult: computeFm(fmInput()) };
type ElementProps = { children?: ReactNode; accessibilityRole?: string; accessibilityState?: { disabled?: boolean; selected?: boolean }; accessibilityLabel?: string; onPress?: () => void; disabled?: boolean };
function elements(node: ReactNode): ElementProps[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!isValidElement<ElementProps>(node)) return [];
  return [node.props, ...elements(node.props.children)];
}
function text(node: ReactNode): string[] {
  if (Array.isArray(node)) return node.flatMap(text);
  if (typeof node === "string" || typeof node === "number") return [String(node)];
  return isValidElement<ElementProps>(node) ? text(node.props.children) : [];
}

describe("reached-stage navigation", () => {
  beforeEach(() => { ui.state = complete; ui.branches = makeBranches(complete.viewpoint, complete.language); vi.clearAllMocks(); });
  it("opens only the thesis for a fresh study", () => { expect(researchStageAvailability(mobileInitialState, false)).toEqual([true, false, false, false, false]); });
  it("allows a pending review route without opening evidence or downstream stages", () => { expect(researchStageAvailability({ ...mobileInitialState, challengeStatus: "streaming" }, false)).toEqual([true, true, false, false, false]); });
  it("allows prior review/evidence drafts but not worldlines before confirmation", () => { expect(researchStageAvailability({ ...reviewed, confirmation: "pending" }, true)).toEqual([true, true, true, false, false]); });
  it("does not open an absent worldline or unfinished portfolio", () => { expect(researchStageRoute(reviewed, false, 3)).toBeNull(); expect(researchStageRoute(reviewed, true, 4)).toBeNull(); });
  it("lets the user revisit every reached stage without mutating the study", () => {
    const before = JSON.stringify(complete);
    expect(researchStageAvailability(complete, true)).toEqual([true, true, true, true, true]);
    expect([0, 1, 2, 3, 4].map((index) => researchStageRoute(complete, true, index))).toEqual(["/", "/challenge", "/evidence", "/worldline", "/portfolio"]);
    expect(JSON.stringify(complete)).toBe(before); expect(complete.replyDraft).toBe("Unsent reply");
  });
  it("rejects invalid stage indices and confirmation tampering", () => {
    for (const index of [-1, 5, 2.2, NaN]) expect(researchStageRoute(complete, true, index)).toBeNull();
    expect(researchStageRoute({ ...complete, unresolvedIssuesAcknowledged: false }, true, 3)).toBeNull();
  });
  it("renders five accessible buttons and navigates without rerunning calculations", () => {
    const before = JSON.stringify(complete);
    const buttons = elements(StageRail({ active: 4 })).filter((item) => item.accessibilityRole === "button");
    expect(buttons).toHaveLength(5); expect(buttons[4]?.accessibilityState?.selected).toBe(true);
    buttons[1]?.onPress?.(); expect(ui.navigate).toHaveBeenCalledWith("/challenge");
    buttons[4]?.onPress?.(); expect(ui.navigate).toHaveBeenCalledOnce();
    expect(JSON.stringify(ui.state)).toBe(before);
  });
  it("keeps unreached steps disabled and their handlers fail closed", () => {
    ui.state = mobileInitialState; ui.branches = [];
    const buttons = elements(StageRail({ active: 0 })).filter((item) => item.accessibilityRole === "button");
    expect(buttons.slice(1).every((item) => item.disabled && item.accessibilityState?.disabled)).toBe(true);
    buttons[4]?.onPress?.(); expect(ui.navigate).not.toHaveBeenCalled();
  });
  it("shows factor calculation progress instead of telling the user to return and calculate again", () => {
    ui.state = { ...reviewed, stage: "portfolio", fmStatus: "computing", fmProgress: 20 };
    expect(researchStageRoute(ui.state, true, 4)).toBe("/portfolio");
    const output = PortfolioScreen(); expect(text(output)).toContain("正在计算因子…");
    expect(elements(output).some((item) => item.accessibilityRole === "progressbar")).toBe(true);
    expect(text(output).join(" ")).not.toContain("请先运行");
  });
  it("renders a missing worldline state without dereferencing an absent branch", () => {
    ui.state = reviewed; ui.branches = [];
    expect(() => WorldlineScreen()).not.toThrow();
    expect(text(WorldlineScreen())).toContain("世界线数据尚未就绪。");
  });
});

describe("evidence workspace survives route changes in AppProvider", () => {
  const candidate = { searchResultHash: "fixture", searchProvider: "synthetic", title: "Synthetic source", url: "https://example.test/source", excerpt: "Not verified", publisher: null, publishedAt: null, retrievedAt: "2026-10-03T00:00:00Z", independentlyReviewable: false };
  it("keeps search text, pasted draft and candidate results together without mutating previous state", () => {
    const draft = patchEvidenceWorkspace(EMPTY_EVIDENCE_WORKSPACE, { searchQuery: "grid evidence", sharedText: "Unsubmitted pasted text" });
    const ready = patchEvidenceWorkspace(draft, { candidates: [candidate] });
    expect(ready).toEqual({ searchQuery: "grid evidence", sharedText: "Unsubmitted pasted text", candidates: [candidate] });
    expect(draft.candidates).toEqual([]); expect(EMPTY_EVIDENCE_WORKSPACE.sharedText).toBe("");
  });
  it("invalidates only outdated candidates when the query changes", () => {
    const before = { searchQuery: "old", sharedText: "Keep this draft", candidates: [candidate] };
    expect(patchEvidenceWorkspace(before, { searchQuery: "new" })).toEqual({ searchQuery: "new", sharedText: "Keep this draft", candidates: [] });
    expect(patchEvidenceWorkspace(before, { searchQuery: "old" }).candidates).toEqual([candidate]);
  });
  it("editing the pasted draft does not erase a completed search", () => {
    const before = { searchQuery: "query", sharedText: "old", candidates: [candidate] };
    expect(patchEvidenceWorkspace(before, { sharedText: "new" })).toEqual({ ...before, sharedText: "new" });
  });
});
