import { useState } from "react";
import { Switch, Text, View } from "react-native";
import { useMobileStore } from "../src/app-store";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, Status, styles } from "../src/ui";

export default function ArchiveScreen(): React.JSX.Element {
  const { state, archive, archiveAvailable, saveArchive, restoreArchive, exportPublicDocument } = useMobileStore();
  const [includePrivate, setIncludePrivate] = useState(false);
  const [busy, setBusy] = useState(false);
  const save = async () => { setBusy(true); try { await saveArchive(includePrivate); } finally { setBusy(false); } };
  const restore = async () => { setBusy(true); try { await restoreArchive(); } finally { setBusy(false); } };
  return <Screen><Header title={t(state.language, "localArchive")} subtitle={t(state.language, "encryptedOffline")} />
    <Card tone="warning"><Text style={styles.body}>{t(state.language, "archiveCrypto")}</Text><Text style={styles.tiny}>{t(state.language, "archiveNativeOnly")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "privacySelection")}</Text><View style={styles.row}><View style={styles.flex}><Text style={styles.strong}>{t(state.language, "includePrivate")}</Text><Text style={styles.muted}>{t(state.language, "includePrivateHint")}</Text></View><Switch accessibilityLabel={t(state.language, "includePrivate")} value={includePrivate} onValueChange={setIncludePrivate} /></View></Card>
    <Button onPress={() => void save()} disabled={busy} label={t(state.language, "encryptSave")}>{busy ? t(state.language, "saving") : t(state.language, "encryptSave")}</Button>
    <Button variant="secondary" onPress={() => void restore()} disabled={busy || !archiveAvailable} label={t(state.language, "restoreArchive")}>{t(state.language, "restoreArchive")}</Button>
    <Button variant="secondary" onPress={() => void exportPublicDocument()} disabled={busy} label={t(state.language, "exportSafe")}>{t(state.language, "exportSafe")}</Button>
    {state.exportNotice ? <Status tone={archive ? "good" : "warning"}>{state.exportNotice}</Status> : null}
    {archive ? <Card><Text style={styles.heading}>{archive.title}</Text><Text style={styles.body}>{archive.viewpoint}</Text><Text style={styles.muted}>{archive.currency} · {archive.capital} · {archive.selectedBranch}</Text><Text style={styles.tiny}>{archive.evidence.length} {t(state.language, "evidenceItems")}; {archive.privatePremiseIncluded ? t(state.language, "privateOptedIn") : t(state.language, "privateExcluded")}.</Text>{archive.researchState ? <Text style={styles.tiny}>{t(state.language, "restoredWorkflow")} · {archive.researchState.stage} · {t(state.language, "restoredChallenge")} {archive.researchState.challengeStatus} · {t(state.language, "restoredFm")} {archive.researchState.fmStatus} · {archive.researchState.methodData.methodVersion} · {t(state.language, "dataAsOf")} {archive.researchState.methodData.dataAsOf}</Text> : null}</Card> : <Card tone="soft"><Text style={styles.muted}>{t(state.language, "noArchiveMemory")}</Text></Card>}<BottomNav />
  </Screen>;
}
