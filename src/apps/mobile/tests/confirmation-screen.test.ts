import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactNode } from "react";
import type { AppState } from "../src/types";
// Handler-level probe with controlled hooks/host components, not a native renderer.
const h = vi.hoisted(() => ({ state: null as AppState | null, busy: false, confirm: vi.fn(), navigate: vi.fn(), blur: null as (() => void) | null }));
vi.mock("react", async (original) => ({ ...await original<typeof import("react")>(), useState: (initial: unknown) => [initial, vi.fn()], useRef: (initial: unknown) => ({ current: initial }), useCallback: (callback: unknown) => callback }));
vi.mock("expo-router", () => ({ router: { push: h.navigate, navigate: h.navigate, replace: h.navigate }, useFocusEffect: (effect: () => () => void) => { h.blur = effect(); } }));
vi.mock("react-native", () => ({ Switch: "Switch", Text: "Text", TextInput: "TextInput", View: "View" }));
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
vi.mock("../src/app-store", () => ({ useMobileStore: () => ({ state: h.state, confirmationBusy: h.busy, confirm: h.confirm }) }));
vi.mock("../src/ui", () => ({ BottomNav: "BottomNav", Button: "Button", Card: "Card", Header: "Header", Screen: "Screen", StageRail: "StageRail", Status: "Status", styles: {} }));
import ChallengeScreen from "../app/challenge";
import { mobileInitialState } from "../src/lifecycle";
import { t } from "../src/i18n";
type Props = { children?: ReactNode; label?: string; disabled?: boolean; onPress?: () => void };
function all(node: ReactNode): Props[] { if (Array.isArray(node)) return node.flatMap(all); return isValidElement<Props>(node) ? [node.props, ...all(node.props.children)] : []; }
function reservedButton() { return all(ChallengeScreen()).find((item) => item.label === t("zh", "reserved"))!; }
const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
beforeEach(() => {
  vi.clearAllMocks(); h.busy = false; h.blur = null;
  h.state = { ...mobileInitialState, challengeStatus: "complete", challengeTurns: [{ id: "s", author: "system", body: "Synthetic" }, { id: "c", author: "challenger", body: "Question" }, { id: "u", author: "user", body: "Reply" }], unresolvedIssuesAcknowledged: true, reservationNote: "Synthetic reservation" };
});
describe("confirmation screen handler", () => {
  it("does not jump until confirmation resolves successfully", async () => {
    let finish!: (value: boolean) => void; h.confirm.mockReturnValue(new Promise<boolean>((resolve) => { finish = resolve; }));
    reservedButton().onPress?.(); expect(h.confirm).toHaveBeenCalledWith(true); expect(h.navigate).not.toHaveBeenCalled();
    finish(true); await flush(); expect(h.navigate).toHaveBeenCalledWith("/worldline");
  });
  it("stays put when confirmation reports failure", async () => {
    h.confirm.mockResolvedValue(false); reservedButton().onPress?.(); await flush(); expect(h.navigate).not.toHaveBeenCalled();
  });
  it("disables confirmation while one request is outstanding", () => { h.busy = true; expect(reservedButton().disabled).toBe(true); });
  it("does not take focus back when completion arrives after leaving review", async () => {
    let finish!: (value: boolean) => void; h.confirm.mockReturnValue(new Promise<boolean>((resolve) => { finish = resolve; }));
    reservedButton().onPress?.(); h.blur?.(); finish(true); await flush(); expect(h.navigate).not.toHaveBeenCalled();
  });
});
