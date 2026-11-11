import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";
import { AppProvider } from "../src/app-store";
import { OAuthSessionProvider, useOAuthSession } from "../src/oauth-session";
import { researchOwnerScope } from "../src/research-owner";

export function AccountWorkspace(): React.JSX.Element {
  const { session, status } = useOAuthSession();
  const scope = researchOwnerScope(session, status);
  const loading = scope === null;
  // Account changes remount ALL workflow state, requests and draft UI; token refresh
  // for the same account keeps the workspace. The router stays mounted while loading.
  return <AppProvider key={scope ?? "session-loading"} storageScope={scope}>
    <StatusBar style="dark" />
    <View style={styles.root}>
      <View style={styles.root} pointerEvents={loading ? "none" : "auto"} accessibilityElementsHidden={loading} importantForAccessibility={loading ? "no-hide-descendants" : "auto"}>
        <Stack screenOptions={{ headerShown: false, animation: "slide_from_right" }} />
      </View>
      {loading ? <View style={styles.loading} accessibilityRole="progressbar" accessibilityLabel="正在恢复登录 / Restoring sign-in"><ActivityIndicator /><Text>正在恢复登录…</Text></View> : null}
    </View>
  </AppProvider>;
}
const styles = StyleSheet.create({ root: { flex: 1 }, loading: { position: "absolute", top: 0, bottom: 0, left: 0, right: 0, backgroundColor: "#fafafa", alignItems: "center", justifyContent: "center", gap: 12 } });
export default function RootLayout(): React.JSX.Element {
  return <OAuthSessionProvider><AccountWorkspace /></OAuthSessionProvider>;
}
