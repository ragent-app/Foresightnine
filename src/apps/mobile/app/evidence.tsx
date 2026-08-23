import { useEffect, useState } from "react";
import { Text, TextInput, View } from "react-native";
import { router, useLocalSearchParams } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { normalizeShareToBundle } from "../src/evidence";
import { MOCK_EVIDENCE } from "../src/fixtures";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";

export default function EvidenceScreen(): React.JSX.Element {
  const { state, addEvidence, setSearchStatus, confirm } = useMobileStore();
  const params = useLocalSearchParams<{ shared?: string }>();
  const [sharedText, setSharedText] = useState(typeof params.shared === "string" ? params.shared : "");
  const [notice, setNotice] = useState("");
  const add = (bundle = MOCK_EVIDENCE[0]!) => setNotice(addEvidence(bundle) === "duplicate" ? "Duplicate evidence was not added." : "Evidence added locally.");
  useEffect(() => { if (!sharedText) return; const result = normalizeShareToBundle(sharedText); if (result.ok && result.bundle) add(result.bundle); else setNotice(`Incoming shared content rejected: ${"reason" in result ? result.reason : "missing_bundle"}`); }, []);
  return <Screen><Header back title={t(state.language, "evidence")} subtitle="Evidence stays local; URLs are never fetched automatically." /><StageRail active={2} />
    <Card tone="warning"><Text style={styles.body}><Text style={styles.strong}>Client/own search:</Text> deterministic local bundle only. API search is not configured in this mock build.</Text><Text style={styles.tiny}>Source URLs are user supplied and sanitized; no request is made to them.</Text></Card>
    <View style={styles.split}><View style={styles.flex}><Button onPress={() => { setSearchStatus("mock_ready"); add(); }} label="Add local SearchEvidenceBundle">Add local bundle</Button></View><View style={styles.flex}><Button variant="secondary" onPress={() => setSearchStatus("api_unavailable")} label="Show API search unavailable">API search</Button></View></View>
    {state.searchStatus === "api_unavailable" ? <Status tone="warning">API search unavailable — configure server adapter later.</Status> : null}
    <TextInput accessibilityLabel="Paste shared URL or browser text" style={[styles.input, { minHeight: 82 }]} multiline value={sharedText} onChangeText={setSharedText} placeholder="Paste a browser-shared http(s) link…" placeholderTextColor="#74675d" /><Button variant="secondary" onPress={() => { const result = normalizeShareToBundle(sharedText); if (result.ok && result.bundle) add(result.bundle); else setNotice(`Import rejected: ${"reason" in result ? result.reason : "missing_bundle"}`); }} label="Import local share text">Import shared text</Button>
    {notice ? <Status tone="good">{notice}</Status> : null}
    <Card><Text style={styles.heading}>Evidence ledger</Text>{state.evidence.length === 0 ? <Text style={styles.muted}>No evidence added yet.</Text> : state.evidence.map((item) => <View key={item.evidence_id} style={styles.listRow}><Text style={styles.strong}>Grade {item.grade} · {item.title}</Text><Text style={styles.muted}>{item.publisher}</Text><Text style={styles.tiny}>{item.url || "No public URL"}</Text>{item.grade === "D" || item.grade === "E" ? <Text style={styles.danger}>Not independently reviewable; cannot become a public fact.</Text> : null}</View>)}</Card>
    <Button onPress={() => { confirm(state.confirmation === "reserved"); router.push("/worldline"); }} label={t(state.language, "worldline")}>{t(state.language, "worldline")}</Button><BottomNav />
  </Screen>;
}
