import { Text, View } from "react-native";
import { router } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";

function pct(value: number): string { return `${Math.round(value * 100)}%`; }

export default function WorldlineScreen(): React.JSX.Element {
  const { state, branches, selectBranch, runFm } = useMobileStore();
  const branch = branches.find((item) => item.kind === state.selectedBranch)!;
  return <Screen><Header back title={t(state.language, "worldline")} subtitle="Intervals are mock estimates, not calibrated forecasts." /><StageRail active={3} />
    <Status tone="warning">{t(state.language, "mock")} · confidence: medium · calibration: model_estimate_unvalidated</Status>
    <View style={styles.split}>{branches.map((item) => <View key={item.kind} style={styles.flex}><Button variant={item.kind === state.selectedBranch ? "primary" : "secondary"} onPress={() => selectBranch(item.kind)} label={`Select ${item.title}`}>{item.title}</Button></View>)}</View>
    <Card><Text style={styles.heading}>{branch.title}</Text><Text style={styles.body}>{branch.description}</Text><Text style={styles.muted}>Reveal condition: {branch.reveal}</Text></Card>
    <Card><Text style={styles.heading}>Probability ledger</Text><Text style={styles.body}>P(premise): {pct(branch.interval.low)}–{pct(branch.interval.high)}</Text><Text style={styles.body}>P(worldline | premise): {pct(branch.conditional.low)}–{pct(branch.conditional.high)}</Text><Text style={styles.body}>Joint range: {pct(branch.joint.low)}–{pct(branch.joint.high)}</Text><Text style={styles.tiny}>Joint interval is an explicit range, not a false precise point estimate.</Text></Card>
    <Card><Text style={styles.heading}>Timeline & reveal schedule</Text>{branch.nodes.map((node) => <View key={node.id} style={styles.listRow}><Text style={styles.strong}>{node.time} · {node.event}</Text><Text style={styles.muted}>Occurs {node.occurrenceDate}; revealed {node.revealDate}</Text>{node.invalidation ? <Text style={styles.danger}>Invalidation: {node.invalidation}</Text> : null}</View>)}</Card>
    <Button onPress={() => { runFm(); router.push("/portfolio"); }} label="Run FM locally and prepare allocation">Run FM locally</Button><Text style={styles.tiny}>The shared TypeScript FM engine receives only a cutoff-valid local fixture. No server computes this portfolio.</Text><BottomNav />
  </Screen>;
}
