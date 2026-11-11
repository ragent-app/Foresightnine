import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactNode } from "react";
import type { AppState } from "../src/types";
const h = vi.hoisted(() => ({ state: null as AppState | null, ready: true, status: "saved", start: vi.fn(), push: vi.fn(), retry: vi.fn() }));
vi.mock("react", async original => ({ ...await original<typeof import("react")>(), useMemo: (factory: () => unknown) => factory() }));
vi.mock("../src/app-store", () => ({ useMobileStore: () => ({ state: h.state, branches: [], workspaceReady: h.ready, workspaceStatus: h.status, retryWorkspace: h.retry, startChallenge: h.start, updateDraft: vi.fn() }) }));
vi.mock("../src/oauth-session", () => ({ useOAuthSession: () => ({}) }));
vi.mock("../src/api-runtime", () => ({ mobileApiMode: () => "api" }));
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
vi.mock("expo-router", () => ({ router: { push: h.push }, usePathname: () => "/" }));
vi.mock("react-native", () => ({ Alert: {}, Pressable: "Pressable", ScrollView: "ScrollView", TextInput: "TextInput", Text: "Text", View: "View", StyleSheet: { create: (value: unknown) => value }, useWindowDimensions: () => ({ width: 390, height: 844 }) }));
vi.mock("react-native-safe-area-context", () => ({ SafeAreaView: "SafeAreaView" }));
import { mobileInitialState } from "../src/lifecycle";
import ThesisScreen from "../app/index";
import { Screen, SocialLoginPanel } from "../src/ui";
import { t } from "../src/i18n";
type Props = { children?: ReactNode; label?: string; disabled?: boolean; onPress?: () => void };
function elements(node: ReactNode): Props[] { if (Array.isArray(node)) return node.flatMap(elements); return isValidElement<Props>(node) ? [node.props, ...elements(node.props.children)] : []; }
function words(node: ReactNode): string { if (Array.isArray(node)) return node.map(words).join(" "); if (typeof node === "string" || typeof node === "number") return String(node); return isValidElement<Props>(node) ? words(node.props.children) : ""; }
function containsComponent(node: ReactNode, component: unknown): boolean { if (Array.isArray(node)) return node.some(child => containsComponent(child, component)); return isValidElement<Props>(node) && (node.type === component || containsComponent(node.props.children, component)); }
beforeEach(() => { vi.clearAllMocks(); h.state = { ...mobileInitialState }; h.ready = true; h.status = "saved"; });
describe("workspace entry and save feedback (element tree)", () => {
  it("continues a recovered study instead of issuing another challenge", () => {
    h.state = { ...mobileInitialState, draftRestored: true, stage: "evidence", challengeStatus: "complete", challengeTurns: [{ id: "s", author: "system", body: "Synthetic" }, { id: "c", author: "challenger", body: "Question" }] };
    const button = elements(ThesisScreen()).find(p => p.label === "继续研究"); expect(button).toBeDefined(); button!.onPress!(); expect(h.push).toHaveBeenCalledWith("/evidence"); expect(h.start).not.toHaveBeenCalled();
  });
  it("still starts a deliberately new valid study", () => { const button = elements(ThesisScreen()).find(p => p.label === t("zh", "start")); expect(button?.disabled).toBe(false); button!.onPress!(); expect(h.start).toHaveBeenCalledTimes(1); expect(h.push).toHaveBeenCalledWith("/challenge"); });
  it("hides interactive study content until local recovery finishes", () => { h.ready = false; h.status = "loading"; const node = Screen({ children: "PRIVATE_TEST_CONTENT" }); expect(words(node)).not.toContain("PRIVATE_TEST_CONTENT"); expect(words(node)).toContain("正在恢复研究"); });
  it("offers a read retry instead of overwriting unreadable stored data", () => { h.ready = false; h.status = "error"; const node = Screen({ children: "PRIVATE_TEST_CONTENT" }); expect(words(node)).not.toContain("PRIVATE_TEST_CONTENT"); elements(node).find(p => p.label === "重试读取")!.onPress!(); expect(h.retry).toHaveBeenCalledTimes(1); });
  it("keeps account controls reachable when a stored study is unreadable", () => {
    h.ready = false; h.status = "error";
    expect(containsComponent(Screen({ children: "PRIVATE_TEST_CONTENT" }), SocialLoginPanel)).toBe(true);
    h.status = "loading";
    expect(containsComponent(Screen({ children: null }), SocialLoginPanel)).toBe(false);
  });
  it("does not label every edit as already saved", () => { expect(words(ThesisScreen())).not.toContain(t("zh", "draftSaved")); h.status = "saving"; expect(words(Screen({ children: null }))).toContain("正在保存草稿"); });
});
