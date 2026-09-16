import { Linking, Pressable, Text } from "react-native";
import { useMobileStore } from "../src/app-store";
import { t } from "../src/i18n";
import { BottomNav, Card, Header, Screen, SocialLoginPanel, Status, styles } from "../src/ui";

export default function ConsoleScreen(): React.JSX.Element {
  const { state, marketDataState } = useMobileStore();
  return <Screen><Header title={t(state.language, "consoleTitle")} subtitle={t(state.language, "consoleLead")} />
    <SocialLoginPanel />
    <Card><Text style={styles.heading}>{t(state.language, "providerRouting")}</Text><Status tone="warning">{t(state.language, "notConfigured")}</Status><Text style={styles.strong}>{t(state.language, "glmServerOnly")}</Text><Text style={styles.body}>{t(state.language, "glmServerOnlyBody")}</Text></Card>
    <Card tone="soft"><Text style={styles.heading}>{t(state.language, "personalProvider")}</Text><Text style={styles.body}>{t(state.language, "personalProviderBody")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "clientFm")}</Text><Status tone={marketDataState.status === "error" ? "warning" : marketDataState.status === "ready" ? "good" : "neutral"}>{marketDataState.mode} · {marketDataState.status}</Status><Text style={styles.body}>{t(state.language, "cutoffFixture")}</Text></Card>
    <Card tone="soft"><Text style={styles.heading}>{t(state.language, "subscription")}</Text><Text style={styles.body}>{t(state.language, "subscriptionBody")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "privacyPolicy")}</Text><Text style={styles.body}>{t(state.language, "privacyPolicyBody")}</Text><Pressable accessibilityRole="link" accessibilityLabel={t(state.language, "privacyPolicy")} onPress={() => { void Linking.openURL("https://app.foresightnine.com/privacy.html"); }}><Text style={styles.strong}>{t(state.language, "openPrivacyPolicy")}</Text></Pressable></Card>
    <Card><Text style={styles.heading}>{t(state.language, "researchBasis")}</Text><Text style={styles.strong}>{t(state.language, "researchLoop")}</Text><Text style={styles.body}>{t(state.language, "researchLoopBody")}</Text><Text style={styles.strong}>{t(state.language, "clientFm")}</Text><Text style={styles.body}>{t(state.language, "clientFmBody")}</Text></Card>
    <Card tone="warning"><Text style={styles.heading}>{t(state.language, "releaseGates")}</Text><Text style={styles.muted}>{t(state.language, "releaseGatesBody")}</Text></Card><BottomNav />
  </Screen>;
}
