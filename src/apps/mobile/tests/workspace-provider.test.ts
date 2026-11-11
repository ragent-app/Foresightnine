import { beforeEach, describe, expect, it, vi } from "vitest";
const h = vi.hoisted(() => ({ session: null as { account: { account_id: string } } | null, status: "loading" }));
vi.mock("../src/oauth-session", () => ({ useOAuthSession: () => h, OAuthSessionProvider: "OAuthSessionProvider" }));
vi.mock("../src/app-store", () => ({ AppProvider: "AppProvider" }));
vi.mock("expo-router", () => ({ Stack: "Stack" }));
vi.mock("expo-status-bar", () => ({ StatusBar: "StatusBar" }));
vi.mock("react-native", () => ({ View: "View", Text: "Text", ActivityIndicator: "ActivityIndicator", StyleSheet: { create: (value: unknown) => value } }));
import RootLayout, { AccountWorkspace } from "../app/_layout";
beforeEach(() => { h.session = null; h.status = "loading"; });
describe("account workspace boundary (element tree, not native renderer)", () => {
  it("keeps authentication outside the keyed study workspace", () => { const root = RootLayout(); expect(root.type).toBe("OAuthSessionProvider"); expect(root.props.children.type).toBe(AccountWorkspace); });
  it("blocks interaction and storage during hydration but keeps the router mounted", () => {
    const tree = AccountWorkspace(); expect(tree.props.storageScope).toBeNull();
    const root = tree.props.children[1]; const route = root.props.children[0];
    expect(route.props.pointerEvents).toBe("none"); expect(route.props.importantForAccessibility).toBe("no-hide-descendants"); expect(route.props.children.type).toBe("Stack"); expect(root.props.children[1]).not.toBeNull();
  });
  it("keys the whole provider differently for A, B and logout", () => {
    h.status = "signed_in"; h.session = { account: { account_id: "A" } }; const a = AccountWorkspace();
    h.session = { account: { account_id: "B" } }; const b = AccountWorkspace();
    h.session = null; h.status = "signed_out"; const guest = AccountWorkspace();
    expect(new Set([a.key, b.key, guest.key]).size).toBe(3); expect(a.props.storageScope).toBe("account:A"); expect(b.props.storageScope).toBe("account:B"); expect(guest.props.storageScope).toBe("anonymous");
  });
  it("preserves the workspace key for a replacement session of the same account", () => {
    h.status = "signed_in"; h.session = { account: { account_id: "A" } }; const first = AccountWorkspace(); h.session = { account: { account_id: "A" } };
    const second = AccountWorkspace(); expect(second.key).toBe(first.key); expect(second.props.children[1].props.children[0].props.pointerEvents).toBe("auto"); expect(second.props.children[1].props.children[1]).toBeNull();
  });
});
