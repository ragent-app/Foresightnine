import { Text, View } from "react-native";
import { router } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";
import { canEnterWorldline } from "../src/validation";
import { mobileApiMode } from "../src/api-runtime";

function pct(value: number): string { return `${Math.round(value * 100)}%`; }

export default function WorldlineScreen(): React.JSX.Element {
  const { state, branches, selectBranch, runFm } = useMobileStore();
  if (!canEnterWorldline(state)) return <Screen><Header back title={t(state.language, "worldline")} subtitle={t(state.language, "mockIntervals")} /><StageRail active={3} /><Status tone="warning">{t(state.language, "confirmationRequired")}</Status><Button onPress={() => router.replace("/challenge")} label={t(state.language, "challenge")}>{t(state.language, "challenge")}</Button><BottomNav /></Screen>;
  const branch = branches.find((item) => item.kind === state.selectedBranch);
  if (!branch) return <Screen><Header back title={t(state.language, "worldline")} /><StageRail active={3} /><Status tone="warning">{state.exportNotice || (state.language === "en" ? "Worldline data is not ready." : "世界线数据尚未就绪。")}</Status><Button onPress={() => router.navigate("/challenge")} label={t(state.language, "challenge")}>{t(state.language, "challenge")}</Button><BottomNav /></Screen>;
  return <Screen><Header back title={t(state.language, "worldline")} subtitle={t(state.language, "mockIntervals")} /><StageRail active={3} />
    <Status tone="warning">{mobileApiMode() === "api" ? (state.branchSnapshot ? (state.language === "en" ? "API worldline" : "API 世界线") : (state.language === "en" ? "API worldline pending" : "API 世界线生成中")) : t(state.language, "mock")} · {t(state.language, "confidence")}: {t(state.language, "medium")} · {t(state.language, "calibration")}: {t(state.language, "unvalidated")} · {t(state.language, "dataAsOf")} {branch.dataAsOf}</Status>
    <View style={styles.split}>{branches.map((item) => <View key={item.kind} style={styles.flex}><Button variant={item.kind === state.selectedBranch ? "primary" : "secondary"} onPress={() => selectBranch(item.kind)} label={`${t(state.language, "select")} ${item.title}`}>{item.title}</Button></View>)}</View>
    <Card><Text style={styles.heading}>{branch.title}</Text><Text style={styles.body}>{branch.description}</Text><Text style={styles.muted}>{t(state.language, "revealCondition")}: {branch.reveal}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "probabilityLedger")}</Text><Text style={styles.body}>{t(state.language, "premiseProbability")}: {pct(branch.premise.low)}–{pct(branch.premise.high)}</Text><Text style={styles.body}>{t(state.language, "conditionalProbability")}: {pct(branch.conditional.low)}–{pct(branch.conditional.high)}</Text><Text style={styles.body}>{t(state.language, "jointRange")}: {pct(branch.joint.low)}–{pct(branch.joint.high)}</Text><Text style={styles.tiny}>{t(state.language, "explicitRange")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "timeline")}</Text><Text style={styles.tiny}>{t(state.language, "fixtureHistoricalSimulated")} · {t(state.language, "dataAsOf")} {branch.dataAsOf} · {branch.baselineDate}–{branch.horizonEnd}</Text>{branch.nodes.map((node) => <View key={node.id} style={styles.listRow}><Text style={styles.strong}>{node.time} · {node.event}</Text><Text style={styles.muted}>{t(state.language, "occurs")} {node.occurrenceDate}; {t(state.language, "revealed")} {node.revealDate}</Text><Text style={styles.tiny}>{t(state.language, "observable")}: {node.observableIndicators.join(" · ")}</Text><Text style={styles.tiny}>{t(state.language, "causalParents")}: {node.causalParents.join(", ") || "—"}</Text><Text style={styles.tiny}>{t(state.language, "assetAnchors")}: {node.assetAnchors.join(", ")}</Text><Text style={styles.tiny}>{t(state.language, "sources")}: {node.sourceRefs.join(", ")}</Text>{node.invalidation ? <Text style={styles.danger}>{t(state.language, "invalidation")}: {node.invalidation}</Text> : null}</View>)}</Card>
    <Button onPress={() => { runFm(); router.push("/portfolio"); }} label={t(state.language, "runFm")}>{t(state.language, "runFm")}</Button><Text style={styles.tiny}>{t(state.language, "cutoffFixture")}</Text><BottomNav />
  </Screen>;
}
