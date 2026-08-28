import { useMemo, type PropsWithChildren, type ReactNode } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View, useWindowDimensions, type ViewStyle } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router, usePathname } from "expo-router";
import { color, radius, space, typography } from "@foresightnine/design-system";
import { useMobileStore } from "./app-store";
import { t } from "./i18n";

export function Screen({ children, scroll = true }: PropsWithChildren<{ readonly scroll?: boolean }>): React.JSX.Element {
  const content = <View style={styles.page}>{children}</View>;
  return <SafeAreaView style={styles.safe} edges={["top", "bottom"]}>{scroll ? <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">{content}</ScrollView> : content}</SafeAreaView>;
}

export function Header({ title, subtitle, back }: { readonly title: string; readonly subtitle?: string; readonly back?: boolean }): React.JSX.Element {
  const { state, setLanguage } = useMobileStore();
  return <View style={styles.header}>
    <View style={styles.headerText}>{back ? <Pressable accessibilityRole="button" accessibilityLabel={t(state.language, "back")} accessibilityHint={t(state.language, "backHint")} style={styles.back} onPress={() => router.back()}><Text style={styles.backText}>‹</Text></Pressable> : null}<Text style={styles.brand}>ForesightNine</Text><Text style={styles.title}>{title}</Text>{subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}</View>
    <Pressable style={styles.language} accessibilityRole="button" accessibilityLabel={t(state.language, "switchLanguage")} accessibilityState={{ selected: state.language === "en" }} onPress={() => setLanguage(state.language === "zh" ? "en" : "zh")}><Text style={styles.languageText}>{state.language === "zh" ? "EN" : "中"}</Text></Pressable>
  </View>;
}

export function StageRail({ active }: { readonly active: number }): React.JSX.Element {
  const { state } = useMobileStore();
  const labels = state.language === "zh" ? ["观点", "审查", "证据", "世界线", "配置"] : ["Thesis", "Challenge", "Evidence", "Worldline", "Plan"];
  return <View style={styles.rail} accessibilityRole="progressbar" accessibilityValue={{ min: 1, max: labels.length, now: active + 1 }}>
    {labels.map((label, index) => <View key={label} style={styles.railItem}><View style={[styles.railDot, index === active && styles.railDotActive]}><Text style={[styles.railNumber, index === active && styles.railNumberActive]}>{index + 1}</Text></View><Text style={[styles.railLabel, index === active && styles.railLabelActive]} numberOfLines={1}>{label}</Text></View>)}
  </View>;
}

export function Card({ children, tone = "default", style }: PropsWithChildren<{ readonly tone?: "default" | "warning" | "soft"; readonly style?: ViewStyle }>): React.JSX.Element {
  return <View style={[styles.card, tone === "warning" && styles.warning, tone === "soft" && styles.soft, style]}>{children}</View>;
}

export function Button({ children, onPress, variant = "primary", disabled = false, label, hint, style }: PropsWithChildren<{ readonly onPress: () => void; readonly variant?: "primary" | "secondary" | "quiet" | "caution"; readonly disabled?: boolean; readonly label?: string; readonly hint?: string; readonly style?: ViewStyle }>): React.JSX.Element {
  const visual = useMemo(() => [styles.button, variant === "secondary" && styles.buttonSecondary, variant === "quiet" && styles.buttonQuiet, variant === "caution" && styles.buttonCaution, disabled && styles.disabled, style], [variant, disabled, style]);
  return <Pressable style={visual} onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityLabel={label} accessibilityHint={hint} accessibilityState={{ disabled }}><Text style={[styles.buttonText, (variant === "secondary" || variant === "quiet") && styles.buttonTextDark]}>{children}</Text></Pressable>;
}

export function Status({ children, tone = "neutral" }: PropsWithChildren<{ readonly tone?: "neutral" | "warning" | "good" }>): React.JSX.Element { return <View style={[styles.status, tone === "warning" && styles.statusWarning, tone === "good" && styles.statusGood]}><Text style={styles.statusText}>{children}</Text></View>; }

/**
 * The controls are deliberately disabled until a future server readiness
 * response verifies provider configuration and supplies a PKCE start action.
 * No client ID, redirect URI, or OAuth secret is bundled into the app.
 */
export function SocialLoginPanel(): React.JSX.Element {
  const { state } = useMobileStore();
  const { width } = useWindowDimensions();
  const stacked = width < 600;
  return <Card tone="soft">
    <Text style={styles.heading}>{t(state.language, "optionalSignIn")}</Text>
    <Text style={styles.strong}>{t(state.language, "anonymousResearch")}</Text>
    <Text style={styles.muted}>{t(state.language, "signInUnavailable")}</Text>
    <View style={stacked ? styles.stack : styles.row}>
      <Button disabled variant="secondary" style={styles.loginButton} onPress={() => undefined} label={`${t(state.language, "signInGoogle")} — ${t(state.language, "notConfigured")}`}>{t(state.language, "signInGoogle")}</Button>
      <Button disabled variant="secondary" style={styles.loginButton} onPress={() => undefined} label={`${t(state.language, "signInGithub")} — ${t(state.language, "notConfigured")}`}>{t(state.language, "signInGithub")}</Button>
    </View>
  </Card>;
}

export function BottomNav(): React.JSX.Element {
  const { state } = useMobileStore();
  const path = usePathname();
  const items = [{ path: "/", label: t(state.language, "new") }, { path: "/archive", label: t(state.language, "archive") }, { path: "/console", label: t(state.language, "console") }];
  return <View style={styles.bottomNav}>{items.map((item) => <Pressable key={item.path} style={styles.navItem} accessibilityRole="tab" accessibilityState={{ selected: path === item.path }} accessibilityLabel={item.label} onPress={() => router.replace(item.path as never)}><Text style={[styles.navIcon, path === item.path && styles.navActive]}>◇</Text><Text style={[styles.navText, path === item.path && styles.navActive]}>{item.label}</Text></Pressable>)}</View>;
}

export const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: color.canvas }, scroll: { flexGrow: 1, paddingBottom: 28 }, page: { flex: 1, paddingHorizontal: space[5], paddingTop: space[4], gap: space[4] }, header: { minHeight: 94, flexDirection: "row", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }, headerText: { flex: 1, gap: 3 }, brand: { color: color.ink, fontFamily: typography.displayFamily, fontSize: 25, fontWeight: "700" }, title: { color: color.ink, fontFamily: typography.displayFamily, fontSize: 30, lineHeight: 38, fontWeight: "700", marginTop: 10 }, subtitle: { color: color.inkMuted, fontSize: 15, lineHeight: 23 }, language: { width: 48, height: 48, borderColor: color.borderStrong, borderWidth: 1, borderRadius: radius.round, alignItems: "center", justifyContent: "center" }, languageText: { color: color.ink, fontSize: 14, fontWeight: "700" }, back: { width: 48, height: 48, justifyContent: "center" }, backText: { color: color.ink, fontSize: 42, lineHeight: 40 }, rail: { flexDirection: "row", justifyContent: "space-between", gap: 3 }, railItem: { alignItems: "center", flex: 1, gap: 5 }, railDot: { width: 29, height: 29, borderRadius: radius.round, borderWidth: 1, borderColor: color.borderStrong, alignItems: "center", justifyContent: "center", backgroundColor: color.surface }, railDotActive: { backgroundColor: color.accent, borderColor: color.accent }, railNumber: { color: color.inkMuted, fontSize: 12, fontWeight: "700" }, railNumberActive: { color: color.white }, railLabel: { color: color.inkMuted, fontSize: 11 }, railLabelActive: { color: color.accent, fontWeight: "700" }, card: { backgroundColor: color.surface, borderColor: color.border, borderWidth: 1, borderRadius: radius.lg, padding: space[4], gap: space[3] }, warning: { backgroundColor: "#f4e5cf", borderColor: color.warning }, soft: { backgroundColor: color.surfaceMuted }, button: { minHeight: 48, borderRadius: radius.md, backgroundColor: color.accent, justifyContent: "center", alignItems: "center", paddingHorizontal: space[4] }, buttonSecondary: { backgroundColor: color.surface, borderColor: color.borderStrong, borderWidth: 1 }, buttonQuiet: { backgroundColor: "transparent" }, buttonCaution: { backgroundColor: color.warning }, disabled: { opacity: 0.5 }, buttonText: { color: color.white, fontSize: 16, fontWeight: "700" }, buttonTextDark: { color: color.ink }, status: { alignSelf: "flex-start", paddingHorizontal: 10, paddingVertical: 6, borderRadius: radius.round, backgroundColor: color.surfaceMuted }, statusWarning: { backgroundColor: "#f4e5cf" }, statusGood: { backgroundColor: "#dfe9dc" }, statusText: { color: color.ink, fontSize: 12, fontWeight: "600" }, bottomNav: { marginTop: "auto", borderTopWidth: 1, borderTopColor: color.border, flexDirection: "row", justifyContent: "space-around", paddingTop: 10, paddingBottom: 2 }, navItem: { minWidth: 64, minHeight: 48, alignItems: "center", justifyContent: "center", gap: 2 }, navIcon: { color: color.inkMuted, fontSize: 18 }, navText: { color: color.inkMuted, fontSize: 12 }, navActive: { color: color.accent, fontWeight: "700" }, body: { color: color.ink, fontSize: 16, lineHeight: 24 }, muted: { color: color.inkMuted, fontSize: 14, lineHeight: 21 }, heading: { color: color.ink, fontFamily: typography.displayFamily, fontSize: 21, lineHeight: 28, fontWeight: "700" }, mono: { color: color.inkMuted, fontFamily: typography.monoFamily, fontSize: 12, lineHeight: 18 }, row: { flexDirection: "row", gap: 8, alignItems: "center" }, stack: { gap: 8, alignItems: "stretch" }, loginButton: { flex: 1 }, split: { flexDirection: "row", gap: 10 }, flex: { flex: 1 }, divider: { height: 1, backgroundColor: color.border }, input: { minHeight: 52, borderColor: color.borderStrong, borderWidth: 1, borderRadius: radius.md, backgroundColor: color.surface, padding: 12, color: color.ink, fontSize: 16, textAlignVertical: "top" }, label: { color: color.ink, fontSize: 15, fontWeight: "700" }, error: { color: color.danger, fontSize: 13, lineHeight: 19 }, strong: { fontWeight: "700", color: color.ink }, listRow: { gap: 4, paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: color.border }, selection: { borderColor: color.accent, borderWidth: 2 }, tiny: { color: color.inkMuted, fontSize: 11, lineHeight: 16 }, chart: { height: 116, borderLeftWidth: 1, borderBottomWidth: 1, borderColor: color.borderStrong, justifyContent: "flex-end", padding: 8 }, chartBars: { flex: 1, flexDirection: "row", alignItems: "flex-end", justifyContent: "space-around", gap: 4 }, chartBarColumn: { flex: 1, alignItems: "center", justifyContent: "flex-end", gap: 3 }, chartBar: { width: "72%", borderTopLeftRadius: 3, borderTopRightRadius: 3 }, chartHistorical: { backgroundColor: color.inkMuted }, chartSimulated: { backgroundColor: color.accent }, chartCaption: { color: color.inkMuted, fontSize: 8 }, check: { color: color.strong, fontWeight: "700" }, danger: { color: color.danger, fontWeight: "700" }, spacer: { height: 6 },
});
