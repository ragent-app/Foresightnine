import { Text } from "react-native";
import { BottomNav, Card, Header, Screen, styles } from "../src/ui";

export default function MethodsScreen(): React.JSX.Element {
  return <Screen><Header title="Method & limits" subtitle="What this app can and cannot do" />
    <Card><Text style={styles.heading}>Research loop</Text><Text style={styles.body}>A user thesis is challenged against counterexamples, missing evidence and priced-in assumptions. A mock workflow creates weak/main/strong paths with audited intervals and reveal dates.</Text></Card>
    <Card><Text style={styles.heading}>Client-side FM</Text><Text style={styles.body}>The Android app calls the shared TypeScript FM engine on a cutoff-valid fixture. Selection, ranks, NaN handling, IC/ICIR, signal combination, long-only weights, 3 bps costs and dead-band semantics are shared with Web golden tests.</Text></Card>
    <Card tone="warning"><Text style={styles.heading}>Limits and public-release gates</Text><Text style={styles.muted}>This build is deterministic mock research: no live provider, live search, licensed market data, broker, execution or custody. Current market-data licensing and jurisdictional legal review are required before a public production release. Probability estimates are explicitly unvalidated.</Text></Card><BottomNav />
  </Screen>;
}
