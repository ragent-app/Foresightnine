import { useCallback, useRef, useState } from "react";
import { Switch, Text, TextInput, View } from "react-native";
import { router, useFocusEffect } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { canConfirmNormally, canConfirmReserved, canUseChallengeRoute, totalRubricScore } from "../src/validation";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";
import { mobileApiMode } from "../src/api-runtime";
import { ChallengeTranscript } from "../src/challenge-transcript";
import { continueAfterConfirmation } from "../src/confirmation-flow";

export default function ChallengeScreen(): React.JSX.Element {
  const { state, cancelChallenge, retryChallenge, reply, setReplyDraft, setUnresolvedIssuesAcknowledged, setReservationNote, confirmationBusy, evidenceBusy, confirm } = useMobileStore();
  const focused = useRef(false);
  useFocusEffect(useCallback(() => { focused.current = true; return () => { focused.current = false; }; }, []));
  const confirmAndContinue = (reserved: boolean) => continueAfterConfirmation(confirm, reserved, () => focused.current, () => router.navigate("/worldline")).catch(() => false);
  const [showRubric, setShowRubric] = useState(false);
  const message = state.replyDraft;
  const canConfirm = !confirmationBusy && !evidenceBusy && canConfirmNormally(state);
  const canReserve = !confirmationBusy && !evidenceBusy && canConfirmReserved(state);
  const apiMode = mobileApiMode() === "api";
  const challengeSubtitle = apiMode ? undefined : (state.language === "en" ? "Offline demo" : "离线演示");
  const canReply = !confirmationBusy && !evidenceBusy && state.confirmation === "pending" && state.challengeTurns.some((turn) => turn.author === "challenger") && (apiMode ? state.challengeStatus === "complete" : state.challengeStatus === "streaming" || state.challengeStatus === "recovering");
  if (!canUseChallengeRoute(state) && state.challengeStatus !== "streaming") return <Screen><Header back title={t(state.language, "challenge")} {...(challengeSubtitle ? { subtitle: challengeSubtitle } : {})} /><StageRail active={1} /><Status tone="warning">{t(state.language, "confirmationRequired")}</Status><Button onPress={() => router.replace("/")} label={t(state.language, "start")}>{t(state.language, "start")}</Button><BottomNav /></Screen>;
  const failure = state.challengeFailure ? `${t(state.language, "recoveryState")}: ${state.challengeFailure.replace("_", " ")}` : null;
  const status = state.challengeStatus === "streaming" ? (apiMode ? (state.language === "en" ? "Reviewing…" : "正在审查…") : t(state.language, "streaming")) : t(state.language, `status${state.challengeStatus[0]!.toUpperCase()}${state.challengeStatus.slice(1)}` as "statusIdle" | "statusComplete" | "statusCancelled" | "statusFailed");
  return <Screen><Header back title={t(state.language, "challenge")} {...(challengeSubtitle ? { subtitle: challengeSubtitle } : {})} /><StageRail active={1} />
    <Status tone={state.challengeStatus === "failed" ? "warning" : "neutral"}>{status}</Status>
    {state.exportNotice ? <Card tone="warning"><Text style={styles.body}>{state.exportNotice}</Text></Card> : null}
    {state.challengeTurns.some((turn) => turn.author !== "system") ? <Card><ChallengeTranscript turns={state.challengeTurns} language={state.language} /></Card> : null}
    <Card><Button variant="quiet" onPress={() => setShowRubric((value) => !value)} label={state.language === "en" ? "Toggle review scores" : "展开或收起审查评分"}>{`${t(state.language, "rubric")} · ${totalRubricScore(state.rubric)}/40 · ${showRubric ? "−" : "+"}`}</Button>{showRubric ? state.rubric.map((item) => <View key={item.id} style={styles.listRow}><Text style={styles.strong}>{item.label} · {item.score}/4{item.critical ? ` · ${t(state.language, "critical")}` : ""}</Text><Text style={styles.muted}>{item.rationale}</Text></View>) : null}</Card>
    <TextInput style={[styles.input, { minHeight: 88 }]} accessibilityLabel={t(state.language, "challengeResponse")} multiline value={message} onChangeText={setReplyDraft} placeholder={t(state.language, "challengePlaceholder")} placeholderTextColor="#74675d" /><View style={styles.split}><View style={styles.flex}><Button variant="secondary" onPress={cancelChallenge} label={t(state.language, "cancel")}>{t(state.language, "cancel")}</Button></View><View style={styles.flex}><Button onPress={() => { if (canReply && message.trim()) reply(message.trim()); }} disabled={!canReply || !message.trim()} label={t(state.language, "submitResponse")}>{t(state.language, "submitResponse")}</Button></View></View>

    {failure || state.challengeStatus === "cancelled" || state.challengeStatus === "failed" ? <Card tone="warning">{failure ? <Text style={styles.body}>{failure}</Text> : null}<Button onPress={retryChallenge} label={t(state.language, "recover")}>{t(state.language, "recover")}</Button></Card> : null}
    <Button variant="secondary" onPress={() => router.push("/evidence")} label={t(state.language, "evidence")}>{t(state.language, "evidence")}</Button>
    <Card tone="warning"><Text style={styles.heading}>{t(state.language, "reservationRequirements")}</Text><View style={styles.row}><Switch accessibilityLabel={t(state.language, "acknowledgeUnresolvedIssues")} disabled={confirmationBusy} value={state.unresolvedIssuesAcknowledged} onValueChange={setUnresolvedIssuesAcknowledged} /><Text style={[styles.body, styles.flex]}>{t(state.language, "acknowledgeUnresolvedIssues")}</Text></View><TextInput accessibilityLabel={t(state.language, "reservationNote")} style={[styles.input, { minHeight: 72 }]} multiline editable={!confirmationBusy} value={state.reservationNote} onChangeText={setReservationNote} placeholder={t(state.language, "reservationNoteHint")} placeholderTextColor="#74675d" /></Card>
    {confirmationBusy ? <Status>{state.language === "en" ? "Confirming and preparing worldlines…" : "正在确认并生成世界线…"}</Status> : null}
    <Button onPress={() => { void confirmAndContinue(false); }} disabled={!canConfirm} label={t(state.language, "confirm")}>{t(state.language, "confirm")}</Button>
    <Button variant="caution" onPress={() => { void confirmAndContinue(true); }} disabled={!canReserve} label={t(state.language, "reserved")}>{t(state.language, "reserved")}</Button><Text style={styles.tiny}>{t(state.language, "standardConfirmation")}</Text><BottomNav />
  </Screen>;
}
