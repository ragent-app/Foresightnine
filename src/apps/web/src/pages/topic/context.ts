import type { ResearchV2 } from "@foresightnine/contracts";
import { createContext, useContext } from "react";
import type { AvailableVersionSource, ProgressEvent, RunProgress, TransientStreamState } from "../../api/types";
import type { AnswerTab, CitationIndex } from "../../lib/research";
import type { ReviewDraft } from "../../lib/review";
import type { AddSourceInitial } from "../research/AddSourcesPanel";

export interface LiveStreamStore {
  getSnapshot: () => TransientStreamState;
  subscribe: (listener: () => void) => () => void;
}

export type PanelState =
  | { kind: "record"; tab: AnswerTab; turnKey?: string }
  | { kind: "reader"; sourceId: string; range: readonly [number, number] | null; locBadge?: string | null }
  | { kind: "settings" }
  | { kind: "cite"; n: number; claimId: string; citationIndex: number }
  | { kind: "add"; initial?: AddSourceInitial }
  | { kind: "review" }
  | null;

export interface FocusCitation {
  claimId: string;
  citationIndex: number;
  n: number;
}

/** The running turn as the progress endpoint reports it; `turnKey` is "live" in the steps tab. */
export interface LiveRun {
  operation: ResearchV2.Operation;
  progress: RunProgress | null;
  events: ProgressEvent[];
  stream?: LiveStreamStore | undefined;
}

export interface TopicView {
  topicId: string;
  topic: ResearchV2.ResearchTopic;
  version: ResearchV2.ResearchVersion;
  content: ResearchV2.ResearchContent;
  turns: readonly ResearchV2.ResearchTurn[];
  citations: CitationIndex;
  sources: ReadonlyMap<string, AvailableVersionSource>;
  live: LiveRun | null;
  /** A run is active or starting: adding sources and other writes are unavailable (spec §7). */
  locked: boolean;
  turnNumber: (turnId: string | null | undefined) => number | null;
  sourceTitle: (sourceId: string) => string;
  openPanel: (panel: PanelState) => void;
  deepDive: (focus: FocusCitation) => void;
  gotoClaim: (claimId: string) => void;
  /** Per-turn override of whether the supporting evidence under an answer is expanded. */
  evidence: ReadonlyMap<string, boolean>;
  setEvidence: (turnId: string, open: boolean) => void;
  /** Re-extracts a retained PDF in the other text order and adds the result to the topic. */
  reextractPdf: (source: AvailableVersionSource) => Promise<void>;
  /** Unsaved review changes, kept as a patch over the latest version (spec §4.7). */
  draft: ReviewDraft;
  setDraft: (update: (draft: ReviewDraft) => ReviewDraft) => void;
  dirty: boolean;
  /** Saves the draft as a new version; resolves with the new version id. */
  saveDraft: () => Promise<string>;
  saving: boolean;
  /** Shows a transient banner (spec §4.9). */
  notify: (text: string, tone?: "info" | "error") => void;
}

export const TopicContext = createContext<TopicView | null>(null);

export function useTopicView(): TopicView {
  const view = useContext(TopicContext);
  if (!view) throw new Error("TopicContext missing");
  return view;
}

export const LIVE_TURN = "live";
