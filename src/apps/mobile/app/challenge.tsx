import { useState } from "react";
import { Switch, Text, TextInput, View } from "react-native";
import { router } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { canConfirmNormally, canConfirmReserved, canUseChallengeRoute } from "../src/validation";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";

export default function ChallengeScreen(): React.JSX.Element {
  const { state, cancelChallenge, retryChallenge, setChallengeFailure, reply, setUnresolvedIssuesAcknowledged, setReservationNote, confirm } = useMobileStore();
  const [message, setMessage] = useState("");
  const canConfirm = canConfirmNormally(state);
  const canReserve = canConfirmReserved(state);
  if (!canUseChallengeRoute(state)) return <Screen><Header back title={t(state.language, "challenge")} subtitle={t(state.language, "structuredChallenge")} /><StageRail active={1} /><Status tone="warning">{t(state.language, "confirmationRequired")}</Status><Button onPress={() => router.replace("/")} label={t(state.language, "start")}>{t(state.language, "start")}</Button><BottomNav /></Screen>;
  const failure = state.challengeFailure ? `${t(state.language, "recoveryState")}: ${state.challengeFailure.replace("_", " ")}` : null;
  const status = state.challengeStatus === "streaming" ? t(state.language, "streaming") : t(state.language, `status${state.challengeStatus[0]!.toUpperCase()}${state.challengeStatus.slice(1)}` as "statusIdle" | "statusComplete" | "statusCancelled" | "statusFailed");
  return <Screen><Header back title={t(state.language, "challenge")} subtitle={t(state.language, "structuredChallenge")} /><StageRail active={1} />
    <Status tone={state.challengeStatus === "failed" ? "warning" : "neutral"}>{status}</Status>
    <Card tone="warning"><Text style={styles.body}><Text style={styles.strong}>{t(state.language, "challenged")}:</Text> {t(state.language, "challengedBody")}</Text><Text style={styles.tiny}>{t(state.language, "noLiveProvider")}</Text></Card>
    <Card>{state.challengeTurns.map((turn) => <View key={turn.id} style={styles.listRow}><Text style={styles.tiny}>{turn.author === "challenger" ? t(state.language, "challenger").toUpperCase() : turn.author.toUpperCase()}</Text><Text style={styles.body}>{turn.body}</Text></View>)}</Card>
    <Card><Text style={styles.heading}>{t(state.language, "rubric")}</Text>{state.rubric.map((item) => <View key={item.id} style={styles.listRow}><Text style={styles.strong}>{item.label} · {item.score}/4{item.critical ? ` · ${t(state.language, "critical")}` : ""}</Text><Text style={styles.muted}>{item.rationale}</Text></View>)}</Card>
    <TextInput style={[styles.input, { minHeight: 88 }]} accessibilityLabel={t(state.language, "challengeResponse")} multiline value={message} onChangeText={setMessage} placeholder={t(state.language, "challengePlaceholder")} placeholderTextColor="#74675d" /><View style={styles.split}><View style={styles.flex}><Button variant="secondary" onPress={cancelChallenge} label={t(state.language, "cancel")}>{t(state.language, "cancel")}</Button></View><View style={styles.flex}><Button onPress={() => { if (message.trim()) reply(message.trim()); }} disabled={!message.trim()} label={t(state.language, "submitResponse")}>{t(state.language, "submitResponse")}</Button></View></View>
    <View style={styles.row}><Button variant="quiet" onPress={() => setChallengeFailure("timeout")} label={t(state.language, "timeout")}>{t(state.language, "timeout")}</Button><Button variant="quiet" onPress={() => setChallengeFailure("invalid_provider")} label={t(state.language, "invalidOutput")}>{t(state.language, "invalidOutput")}</Button><Button variant="quiet" onPress={retryChallenge} label={t(state.language, "retry")}>{t(state.language, "retry")}</Button></View>
    {failure ? <Card tone="warning"><Text style={styles.body}>{failure}</Text><Button onPress={retryChallenge} label={t(state.language, "recover")}>{t(state.language, "recover")}</Button></Card> : null}
    <Button variant="secondary" onPress={() => router.push("/evidence")} label={t(state.language, "evidence")}>{t(state.language, "evidence")}</Button>
    <Card tone="warning"><Text style={styles.heading}>{t(state.language, "reservationRequirements")}</Text><View style={styles.row}><Switch accessibilityLabel={t(state.language, "acknowledgeUnresolvedIssues")} value={state.unresolvedIssuesAcknowledged} onValueChange={setUnresolvedIssuesAcknowledged} /><Text style={[styles.body, styles.flex]}>{t(state.language, "acknowledgeUnresolvedIssues")}</Text></View><TextInput accessibilityLabel={t(state.language, "reservationNote")} style={[styles.input, { minHeight: 72 }]} multiline value={state.reservationNote} onChangeText={setReservationNote} placeholder={t(state.language, "reservationNoteHint")} placeholderTextColor="#74675d" /></Card>
    <Button onPress={() => { confirm(false); router.push("/worldline"); }} disabled={!canConfirm} label={t(state.language, "confirm")}>{t(state.language, "confirm")}</Button>
    <Button variant="caution" onPress={() => { confirm(true); router.push("/worldline"); }} disabled={!canReserve} label={t(state.language, "reserved")}>{t(state.language, "reserved")}</Button><Text style={styles.tiny}>{t(state.language, "standardConfirmation")}</Text><BottomNav />
  </Screen>;
}
