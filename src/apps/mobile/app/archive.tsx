import { useState } from "react";
import { Switch, Text, View } from "react-native";
import { useMobileStore } from "../src/app-store";
import { BottomNav, Button, Card, Header, Screen, Status, styles } from "../src/ui";

export default function ArchiveScreen(): React.JSX.Element {
  const { state, archive, archiveAvailable, saveArchive, restoreArchive, exportPublicDocument } = useMobileStore();
  const [includePrivate, setIncludePrivate] = useState(false);
  const [busy, setBusy] = useState(false);
  const save = async () => { setBusy(true); try { await saveArchive(includePrivate); } finally { setBusy(false); } };
  const restore = async () => { setBusy(true); try { await restoreArchive(); } finally { setBusy(false); } };
  return <Screen><Header title="Local archive" subtitle="Encrypted device storage · offline viewing" />
    <Card tone="warning"><Text style={styles.body}>A 256-bit AES-GCM key is stored in Android SecureStore; the sealed archive lives in the app document directory. It is not a cloud backup and is unavailable after a key/device reset.</Text><Text style={styles.tiny}>Native encryption is exercised only in an Android native build; Node tests cover envelope and privacy decisions.</Text></Card>
    <Card><Text style={styles.heading}>Privacy selection</Text><View style={styles.row}><View style={styles.flex}><Text style={styles.strong}>Include user-private evidence in this device-only archive</Text><Text style={styles.muted}>Off by default. It is never included in a public export or link.</Text></View><Switch accessibilityLabel="Include private evidence in the local encrypted archive" value={includePrivate} onValueChange={setIncludePrivate} /></View></Card>
    <Button onPress={() => void save()} disabled={busy} label="Encrypt and save local archive">{busy ? "Saving…" : "Encrypt and save"}</Button>
    <Button variant="secondary" onPress={() => void restore()} disabled={busy || !archiveAvailable} label="Restore encrypted local archive">Restore local archive</Button>
    <Button variant="secondary" onPress={() => void exportPublicDocument()} disabled={busy} label="Export privacy-safe research">Export privacy-safe research</Button>
    {state.exportNotice ? <Status tone={archive ? "good" : "warning"}>{state.exportNotice}</Status> : null}
    {archive ? <Card><Text style={styles.heading}>{archive.title}</Text><Text style={styles.body}>{archive.viewpoint}</Text><Text style={styles.muted}>{archive.currency} · {archive.capital} · {archive.selectedBranch}</Text><Text style={styles.tiny}>{archive.evidence.length} evidence item(s); private evidence {archive.privatePremiseIncluded ? "opted in" : "excluded"}.</Text></Card> : <Card tone="soft"><Text style={styles.muted}>No restored archive in memory. Offline restore remains available when a sealed file exists on this device.</Text></Card>}<BottomNav />
  </Screen>;
}
