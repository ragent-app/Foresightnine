import { Text, View } from "react-native";
import type { Language, TranscriptTurn } from "./types";
import { styles } from "./ui";

/** Render validated fields separately; never parse or expose provider JSON as UI markup. */
export function ChallengeTranscript({ turns, language }: { readonly turns: readonly TranscriptTurn[]; readonly language: Language }): React.JSX.Element {
  const en = language === "en";
  return <View>{turns.filter((turn) => turn.author !== "system").map((turn) => {
    const sections = turn.author === "challenger" && turn.review ? [
      { label: en ? "Questions" : "需要回答的问题", items: turn.review.questions },
      { label: en ? "Objections" : "反方观点", items: turn.review.objections },
      { label: en ? "Evidence needed" : "待补充证据", items: turn.review.missingEvidence },
    ].filter((section) => section.items.length > 0) : [];
    return <View key={turn.id} style={styles.listRow}>
      <Text style={styles.tiny}>{turn.author === "user" ? (en ? "You" : "你") : (en ? "Review" : "反方审查")}</Text>
      {sections.length ? sections.map((section) => <View key={section.label} style={styles.stack}>
        <Text accessibilityRole="header" style={styles.strong}>{section.label}</Text>
        {section.items.map((item, index) => <View key={`${turn.id}:${section.label}:${index}`} style={styles.listRow}>
          <Text style={styles.body}>{`${index + 1}. ${item}`}</Text>
        </View>)}
      </View>) : <Text style={styles.body}>{turn.body}</Text>}
    </View>;
  })}</View>;
}
