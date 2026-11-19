import type { ResearchV2 } from "@foresightnine/contracts";
import { createContext, useContext } from "react";
import type { AvailableVersionSource, ProgressEvent, RunProgress } from "../../api/types";
import type { AnswerTab, CitationIndex } from "../../lib/research";
import type { AddSourceInitial } from "../research/AddSourcesPanel";

export type PanelState =
  | { kind: "record"; tab: AnswerTab; turnKey?: string }
  | { kind: "reader"; sourceId: string; range: readonly [number, number] | null }
  | { kind: "settings" }
  | { kind: "cite"; n: number; claimId: string; citationIndex: number }
  | { kind: "add"; initial?: AddSourceInitial }
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
  /** Re-extracts a retained PDF in the other text order and adds the result to the topic. */
  reextractPdf: (source: AvailableVersionSource) => Promise<void>;
}

export const TopicContext = createContext<TopicView | null>(null);

export function useTopicView(): TopicView {
  const view = useContext(TopicContext);
  if (!view) throw new Error("TopicContext missing");
  return view;
}

export const LIVE_TURN = "live";
