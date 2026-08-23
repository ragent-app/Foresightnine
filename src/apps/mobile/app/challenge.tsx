import { useState } from "react";
import { Text, TextInput, View } from "react-native";
import { router } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";

export default function ChallengeScreen(): React.JSX.Element {
  const { state, cancelChallenge, retryChallenge, setChallengeFailure, reply, confirm } = useMobileStore();
  const [message, setMessage] = useState("");
  const canConfirm = state.challengeStatus === "complete" && state.evidence.length > 0;
  const failure = state.challengeFailure ? `Mock recovery state: ${state.challengeFailure.replace("_", " ")}` : null;
  return <Screen><Header back title={t(state.language, "challenge")} subtitle="Structured counterargument · mock provider" /><StageRail active={1} />
    <Status tone={state.challengeStatus === "failed" ? "warning" : "neutral"}>{state.challengeStatus === "streaming" ? "Challenge is streaming deterministic mock output" : state.challengeStatus}</Status>
    <Card tone="warning"><Text style={styles.body}><Text style={styles.strong}>What is challenged:</Text> counterexamples, causal gaps, priced-in assumptions, and missing independently reviewable evidence.</Text><Text style={styles.tiny}>No live GLM/DeepSeek call is made in this build.</Text></Card>
    <Card>{state.challengeTurns.map((turn) => <View key={turn.id} style={styles.listRow}><Text style={styles.tiny}>{turn.author === "challenger" ? "CHALLENGER" : turn.author.toUpperCase()}</Text><Text style={styles.body}>{turn.body}</Text></View>)}</Card>
    <Card><Text style={styles.heading}>Visible rubric</Text>{state.rubric.map((item) => <View key={item.id} style={styles.listRow}><Text style={styles.strong}>{item.label} · {item.score}/4{item.critical ? " · critical" : ""}</Text><Text style={styles.muted}>{item.rationale}</Text></View>)}</Card>
    <TextInput style={[styles.input, { minHeight: 88 }]} accessibilityLabel="Challenge response" multiline value={message} onChangeText={setMessage} placeholder="Add observable evidence, an invalidation condition, or a reservation…" placeholderTextColor="#74675d" /><View style={styles.split}><View style={styles.flex}><Button variant="secondary" onPress={cancelChallenge} label={t(state.language, "cancel")}>{t(state.language, "cancel")}</Button></View><View style={styles.flex}><Button onPress={() => { if (message.trim()) reply(message.trim()); }} disabled={!message.trim()} label="Submit response">Submit response</Button></View></View>
    <View style={styles.row}><Button variant="quiet" onPress={() => setChallengeFailure("timeout")} label="Show timeout recovery">Timeout</Button><Button variant="quiet" onPress={() => setChallengeFailure("invalid_provider")} label="Show invalid provider output">Invalid output</Button><Button variant="quiet" onPress={retryChallenge} label={t(state.language, "retry")}>{t(state.language, "retry")}</Button></View>
    {failure ? <Card tone="warning"><Text style={styles.body}>{failure}</Text><Button onPress={retryChallenge} label="Recover mock operation">Recover</Button></Card> : null}
    <Button variant="secondary" onPress={() => router.push("/evidence")} label={t(state.language, "evidence")}>{t(state.language, "evidence")}</Button>
    <Button onPress={() => { confirm(false); router.push("/worldline"); }} disabled={!canConfirm} label={t(state.language, "confirm")}>{t(state.language, "confirm")}</Button>
    <Button variant="caution" onPress={() => { confirm(true); router.push("/worldline"); }} label={t(state.language, "reserved")}>{t(state.language, "reserved")}</Button><Text style={styles.tiny}>Standard confirmation requires completed challenge plus evidence. Reservation preserves unresolved issues.</Text><BottomNav />
  </Screen>;
}
