import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactNode } from "react";
import type { AppState } from "../src/types";
const h = vi.hoisted(() => ({ state: null as AppState | null, busy: false, addEvidence: vi.fn(), notice: vi.fn(), noticeGood: vi.fn(), hooks: 0, effectCleanups: [] as (() => void)[] }));
vi.mock("react", async (original) => ({ ...await original<typeof import("react")>(), useState: (initial: unknown) => [initial, h.hooks++ === 0 ? h.notice : h.noticeGood], useRef: (initial: unknown) => ({ current: initial }), useEffect: (effect: () => (() => void) | undefined) => { const cleanup = effect(); if (cleanup) h.effectCleanups.push(cleanup); } }));
vi.mock("react-native", () => ({ Text: "Text", TextInput: "TextInput", View: "View" }));
vi.mock("expo-router", () => ({ router: { push: vi.fn() }, useLocalSearchParams: () => ({}) }));
vi.mock("../src/app-store", () => ({ useMobileStore: () => ({ state: h.state, addEvidence: h.addEvidence, evidenceBusy: h.busy, confirmationBusy: false, setSearchStatus: vi.fn(), evidenceWorkspace: { sharedText: "https://example.com/source Synthetic evidence", searchQuery: "", candidates: [] }, updateEvidenceWorkspace: vi.fn() }) }));
vi.mock("../src/api-runtime", () => ({ mobileApiMode: () => "api" }));
vi.mock("../src/ui", () => ({ BottomNav: "BottomNav", Button: "Button", Card: "Card", Header: "Header", Screen: "Screen", StageRail: "StageRail", Status: "Status", styles: {} }));
import EvidenceScreen from "../app/evidence";
import { mobileInitialState } from "../src/lifecycle";
import { t } from "../src/i18n";
type Props = { children?: ReactNode; label?: string; disabled?: boolean; onPress?: () => void };
function all(node: ReactNode): Props[] { if (Array.isArray(node)) return node.flatMap(all); return isValidElement<Props>(node) ? [node.props, ...all(node.props.children)] : []; }
function submitButton() { return all(EvidenceScreen()).find((props) => props.label === t("zh", "importShare"))!; }
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
beforeEach(() => { vi.clearAllMocks(); h.hooks = 0; h.busy = false; h.effectCleanups = []; h.state = { ...mobileInitialState, challengeStatus: "complete" }; });
describe("evidence screen submit feedback (controlled hooks, not native rendering)", () => {
  it("does not announce success until the async submission accepts the evidence", async () => {
    let finish!: (result: string) => void; h.addEvidence.mockReturnValue(new Promise<string>((resolve) => { finish = resolve; }));
    submitButton().onPress?.(); expect(h.notice).not.toHaveBeenCalledWith(t("zh", "evidenceAdded"));
    finish("added"); await flush(); expect(h.notice).toHaveBeenCalledWith(t("zh", "evidenceAdded")); expect(h.noticeGood).toHaveBeenLastCalledWith(true);
  });
  it("reports rejection rather than success", async () => {
    h.addEvidence.mockResolvedValue("failed"); submitButton().onPress?.(); await flush();
    expect(h.notice).not.toHaveBeenCalledWith(t("zh", "evidenceAdded")); expect(h.notice).toHaveBeenLastCalledWith(expect.stringContaining("证据未添加"));
  });
  it("disables submit while evidence is pending", () => { h.busy = true; expect(submitButton().disabled).toBe(true); });
  it("handles a duplicate without claiming a new success", async () => {
    h.addEvidence.mockResolvedValue("duplicate"); submitButton().onPress?.(); await flush(); expect(h.notice).toHaveBeenLastCalledWith(t("zh", "duplicateEvidence"));
  });
  it("does not update a disposed screen with a late success", async () => {
    let finish!: (result: string) => void; h.addEvidence.mockReturnValue(new Promise<string>((resolve) => { finish = resolve; }));
    submitButton().onPress?.(); h.effectCleanups.forEach((cleanup) => cleanup()); h.notice.mockClear();
    finish("added"); await flush(); expect(h.notice).not.toHaveBeenCalled();
  });
});
