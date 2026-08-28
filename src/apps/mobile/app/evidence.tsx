import { useEffect, useRef, useState } from "react";
import { Text, TextInput, View } from "react-native";
import { router, useLocalSearchParams } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { isDuplicateEvidence, normalizeShareToBundle } from "../src/evidence";
import { mockEvidenceFor } from "../src/fixtures";
import { t } from "../src/i18n";
import { candidateToLocalBundle, searchProjectCandidates, type ProjectSearchCandidate } from "../src/search-client";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";
import { canEnterWorldline } from "../src/validation";

export default function EvidenceScreen(): React.JSX.Element {
  const { state, addEvidence, setSearchStatus } = useMobileStore();
  const params = useLocalSearchParams<{ shared?: string; share_error?: string }>();
  const [sharedText, setSharedText] = useState(typeof params.shared === "string" ? params.shared : "");
  const [notice, setNotice] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [candidates, setCandidates] = useState<readonly ProjectSearchCandidate[]>([]);
  const searchAbort = useRef<AbortController | null>(null);
  const add = (bundle = mockEvidenceFor(state.language)[0]!): boolean => {
    const duplicate = isDuplicateEvidence(state.evidence, bundle);
    addEvidence(bundle);
    setNotice(duplicate ? t(state.language, "duplicateEvidence") : t(state.language, "evidenceAdded"));
    return !duplicate;
  };
  const runProjectSearch = (): void => {
    if (!searchQuery.trim() || state.searchStatus === "loading") return;
    searchAbort.current?.abort();
    const controller = new AbortController();
    searchAbort.current = controller;
    setCandidates([]);
    setSearchStatus("loading");
    void searchProjectCandidates(searchQuery, { signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return;
      setCandidates(result.candidates);
      setSearchStatus(result.status);
    });
  };
  useEffect(() => {
    if (typeof params.share_error === "string") {
      setNotice(`${t(state.language, "incomingRejected")}: ${params.share_error}`);
      return;
    }
    const incomingShared = typeof params.shared === "string" ? params.shared : "";
    if (!incomingShared) return;
    setSharedText(incomingShared);
    const result = normalizeShareToBundle(incomingShared, undefined, state.language);
    if (result.ok && result.bundle) add(result.bundle);
    else setNotice(`${t(state.language, "incomingRejected")}: ${"reason" in result ? result.reason : "missing_bundle"}`);
  }, [params.shared, params.share_error, state.language]);
  useEffect(() => () => searchAbort.current?.abort(), []);
  return <Screen><Header back title={t(state.language, "evidence")} subtitle={t(state.language, "localOnlyEvidence")} /><StageRail active={2} />
    <Card tone="warning"><Text style={styles.body}><Text style={styles.strong}>{t(state.language, "ownSearch")}:</Text> {t(state.language, "ownSearchBody")}</Text><Text style={styles.tiny}>{t(state.language, "noFetch")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "searchCandidates")}</Text><Text style={styles.muted}>{t(state.language, "searchPrompt")}</Text><TextInput accessibilityLabel={t(state.language, "searchQuery")} style={styles.input} value={searchQuery} onChangeText={setSearchQuery} maxLength={70} placeholder={t(state.language, "searchQuery")} placeholderTextColor="#74675d" /><Button variant="secondary" disabled={!searchQuery.trim() || state.searchStatus === "loading"} onPress={runProjectSearch} label={t(state.language, "apiSearch")}>{t(state.language, "apiSearch")}</Button>
      {state.searchStatus === "loading" ? <Status>{t(state.language, "searchLoading")}</Status> : null}
      {state.searchStatus === "api_unavailable" ? <Status tone="warning">{t(state.language, "apiUnavailable")}</Status> : null}
      {state.searchStatus === "api_failed" ? <Status tone="warning">{t(state.language, "apiFailed")}</Status> : null}
      {state.searchStatus === "available" && candidates.length === 0 ? <Status tone="warning">{t(state.language, "searchEmpty")}</Status> : null}
      {candidates.map((candidate) => <View key={candidate.searchResultHash} style={styles.listRow}><Text style={styles.strong}>{candidate.title}</Text><Text style={styles.muted}>{candidate.publisher ?? candidate.searchProvider}</Text><Text style={styles.tiny} numberOfLines={4}>{candidate.excerpt}</Text><Text style={styles.tiny}>{candidate.url}</Text><Button variant="quiet" onPress={() => { if (add(candidateToLocalBundle(candidate, state.language))) setNotice(t(state.language, "candidateSelected")); }} label={t(state.language, "searchCandidate")}>{t(state.language, "searchCandidate")}</Button></View>)}</Card>
    <View style={styles.split}><View style={styles.flex}><Button onPress={() => { setSearchStatus("mock_ready"); add(); }} label={t(state.language, "addLocalBundle")}>{t(state.language, "addLocalBundle")}</Button></View></View>
    <TextInput accessibilityLabel={t(state.language, "importShare")} style={[styles.input, { minHeight: 82 }]} multiline value={sharedText} onChangeText={setSharedText} placeholder={t(state.language, "pasteShare")} placeholderTextColor="#74675d" /><Button variant="secondary" onPress={() => { const result = normalizeShareToBundle(sharedText, undefined, state.language); if (result.ok && result.bundle) add(result.bundle); else setNotice(`${t(state.language, "importRejected")}: ${"reason" in result ? result.reason : "missing_bundle"}`); }} label={t(state.language, "importShare")}>{t(state.language, "importShare")}</Button>
    {notice ? <Status tone="good">{notice}</Status> : null}
    <Card><Text style={styles.heading}>{t(state.language, "ledger")}</Text>{state.evidence.length === 0 ? <Text style={styles.muted}>{t(state.language, "noEvidence")}</Text> : state.evidence.map((item) => <View key={item.evidence_id} style={styles.listRow}><Text style={styles.strong}>Grade {item.grade} · {item.title}</Text><Text style={styles.muted}>{item.publisher}</Text><Text style={styles.tiny}>{item.url || t(state.language, "noPublicUrl")}</Text>{!item.independently_reviewable ? <Text style={styles.danger}>{t(state.language, "notReviewable")}</Text> : null}</View>)}</Card>
    <Button disabled={!canEnterWorldline(state)} onPress={() => router.push("/worldline")} label={t(state.language, "worldline")}>{t(state.language, "worldline")}</Button>{!canEnterWorldline(state) ? <Text style={styles.tiny}>{t(state.language, "confirmationRequired")}</Text> : null}<BottomNav />
  </Screen>;
}
