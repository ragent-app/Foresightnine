import { useMemo } from "react";
import { Text, TextInput, View } from "react-native";
import { router } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { t } from "../src/i18n";
import { hasValidationErrors, validateThesis } from "../src/validation";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";

export default function ThesisScreen(): React.JSX.Element {
  const { state, updateDraft, startChallenge } = useMobileStore();
  const errors = useMemo(() => validateThesis(state.viewpoint, state.capital), [state.viewpoint, state.capital]);
  const proceed = () => { if (!hasValidationErrors(errors)) { startChallenge(); router.push("/challenge"); } };
  return <Screen><Header title={t(state.language, "thesisTitle")} subtitle={t(state.language, "thesisLead")} /><StageRail active={0} />
    {state.draftRestored ? <Status tone="good">{t(state.language, "restored")}</Status> : null}
    <View style={{ gap: 8 }}><Text style={styles.label}>{t(state.language, "viewpoint")}</Text><TextInput accessibilityLabel={t(state.language, "viewpoint")} accessibilityHint={t(state.language, "viewpointHint")} style={[styles.input, { minHeight: 162 }]} multiline maxLength={2000} value={state.viewpoint} onChangeText={(viewpoint) => updateDraft({ viewpoint })} placeholder={t(state.language, "viewpointHint")} placeholderTextColor="#74675d" /><Text style={styles.tiny}>{state.viewpoint.length}/2000 · {t(state.language, "draftSaved")}</Text>{errors.viewpoint ? <Text style={styles.error}>{errors.viewpoint}</Text> : null}</View>
    <View style={{ gap: 8 }}><Text style={styles.label}>{t(state.language, "capital")}</Text><View style={styles.split}>{<TextInput accessibilityLabel={t(state.language, "capital")} keyboardType="decimal-pad" value={state.capital} onChangeText={(capital) => updateDraft({ capital })} style={[styles.input, styles.flex]} placeholder="1,000,000" placeholderTextColor="#74675d" />}</View><View style={styles.row}>{(["CNY", "USD", "EUR"] as const).map((currency) => <Button key={currency} variant={state.currency === currency ? "primary" : "secondary"} onPress={() => updateDraft({ currency })} label={currency}>{currency}</Button>)}</View>{errors.capital ? <Text style={styles.error}>{errors.capital}</Text> : null}</View>
    <Button onPress={proceed} disabled={hasValidationErrors(errors)} label={t(state.language, "start")} hint="Start a structured counterargument challenge">{t(state.language, "start")}</Button>
    <Card tone="soft"><Text style={styles.muted}>{t(state.language, "mockNotice")}</Text><Text style={styles.tiny}>Input is stored locally only. No broker connection, custody, ordering, or execution.</Text></Card><BottomNav />
  </Screen>;
}
