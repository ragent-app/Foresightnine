import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ResearchV2 } from "@foresightnine/contracts";
import { nextPollDelay } from "../lib/polling";
import { ApiError } from "./http";
import { createCalculation, createVersion, getProgress, getRecoveryStatus, getTopic, getVersionSourceText, isActiveStatus, listRuns, listTopics, listVersionSources, recalculateCalculation, type CreateCalculationRequest } from "./research";
import { attachSources, reextractPdfSource } from "./sources";
import { openResearchRunStream, StreamFallbackError } from "./stream";
import type { CreatedSource, ProgressEvent, RecoveryStatus, RunProgress, SourceText, TransientStreamState, VersionSource } from "./types";
import { cancelRun, continueTurn, createResearch, idempotentWrite, recoverTurn, startTurn } from "./writes";
import type { CreateResearchResult } from "./writes";

export const queryKeys = {
  topics: ["topics"] as const,
  topic: (id: string) => ["topic", id] as const,
  runs: (id: string) => ["runs", id] as const,
  recovery: (topicId: string, opId: string) => ["recovery", topicId, opId] as const,
  versionSources: (topicId: string, versionId: string) => ["versionSources", topicId, versionId] as const,
  sourceText: (topicId: string, versionId: string, sourceId: string) =>
    ["sourceText", topicId, versionId, sourceId] as const,
};

export function useTopics() {
  return useQuery<ResearchV2.ResearchTopic[]>({
    queryKey: queryKeys.topics,
    queryFn: ({ signal }) => listTopics(signal),
  });
}

export function useTopic(topicId: string) {
  return useQuery<ResearchV2.ResearchTopic>({
    queryKey: queryKeys.topic(topicId),
    queryFn: ({ signal }) => getTopic(topicId, signal),
  });
}

export function useRuns(topicId: string) {
  return useQuery<ResearchV2.OperationResponse[]>({
    queryKey: queryKeys.runs(topicId),
    queryFn: ({ signal }) => listRuns(topicId, signal),
  });
}

export function useRecoveryStatus(topicId: string, operationId: string | null) {
  return useQuery<RecoveryStatus>({
    queryKey: queryKeys.recovery(topicId, operationId ?? ""),
    queryFn: ({ signal }) => getRecoveryStatus(topicId, operationId ?? "", signal),
    enabled: operationId !== null,
  });
}

export function useVersionSources(topicId: string, versionId: string | null) {
  return useQuery<VersionSource[]>({
    queryKey: queryKeys.versionSources(topicId, versionId ?? ""),
    queryFn: ({ signal }) => listVersionSources(topicId, versionId ?? "", signal),
    enabled: versionId !== null,
  });
}

export function useSourceText(
  topicId: string,
  versionId: string | null,
  sourceId: string | null,
  enabled: boolean,
) {
  return useQuery<SourceText>({
    queryKey: queryKeys.sourceText(topicId, versionId ?? "", sourceId ?? ""),
    queryFn: ({ signal }) => getVersionSourceText(topicId, versionId ?? "", sourceId ?? "", signal),
    enabled: enabled && versionId !== null && sourceId !== null,
    staleTime: Infinity,
  });
}

export function useActiveRun(topicId: string) {
  const query = useRuns(topicId);
  const runs = query.data;
  const activeRun = runs?.find((item) => isActiveStatus(item.operation.status))?.operation ?? null;
  const latestRun = runs?.[0]?.operation ?? null;
  return {
    activeRun,
    latestRun,
    isLoading: query.isLoading,
    error: query.error,
    refetch: query.refetch,
  };
}

interface ProgressState {
  key: string;
  progress: RunProgress | null;
  events: ProgressEvent[];
  error: unknown | null;
  stopped: boolean;
}

const SSE_BACKOFF_MS = [1000, 2000, 4000] as const;
const MAX_THINKING_CHARS = 24_000;
const MAX_TEXT_CHARS = 12_000;
const MAX_TOOL_STATUS_CHARS = 500;
const MAX_DRAFT_REPORT_CHARS = 100_000;
export const RETRACTED_SUBMISSION_NOTICE = "草稿引文或格式未通过核验，模型正在修正…";

const EMPTY_TRANSIENT_STREAM: TransientStreamState = {
  callSeq: null,
  thinking: "",
  text: "",
  toolStatus: null,
  draftReport: "",
  activeChannel: null,
  retractedNotice: null,
  lastSeq: -1,
};

function emptyTransientStream(): TransientStreamState {
  return { ...EMPTY_TRANSIENT_STREAM };
}

function appendTailCap(existing: string, delta: string, limit: number): string {
  if (!delta) return existing;
  const combined = existing + delta;
  if (combined.length <= limit) return combined;
  return `…\n${combined.slice(-(limit - 2))}`;
}

function appendPrefixCap(existing: string, delta: string, limit: number): string {
  if (!delta || existing.length >= limit) return existing;
  return existing + delta.slice(0, limit - existing.length);
}

function emptyProgress(key: string): ProgressState {
  return { key, progress: null, events: [], error: null, stopped: false };
}

function mergeEvents(existing: ProgressEvent[], incoming: ProgressEvent[]): ProgressEvent[] {
  if (incoming.length === 0) return existing;
  const byIndex = new Map<number, ProgressEvent>();
  for (const event of existing) byIndex.set(event.index, event);
  for (const event of incoming) byIndex.set(event.index, event);
  return [...byIndex.values()].sort((a, b) => a.index - b.index);
}

export function useRunProgress(topicId: string, operationId: string | null) {
  const queryClient = useQueryClient();
  const key = operationId === null ? "" : `${topicId}\u0000${operationId}`;
  const [state, setState] = useState<ProgressState>(() => emptyProgress(key));
  const streamRef = useRef<TransientStreamState>(emptyTransientStream());
  const streamKeyRef = useRef(key);
  const listenersRef = useRef(new Set<() => void>());
  const batchRef = useRef<{ kind: "raf" | "timeout"; handle: number | ReturnType<typeof setTimeout> } | null>(null);

  const flushStreamNotify = useCallback(() => {
    const pending = batchRef.current;
    if (pending) {
      batchRef.current = null;
      if (pending.kind === "raf" && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(pending.handle as number);
      } else {
        clearTimeout(pending.handle as ReturnType<typeof setTimeout>);
      }
    }
    for (const listener of listenersRef.current) listener();
  }, []);

  const scheduleStreamNotify = useCallback(() => {
    if (batchRef.current) return;
    if (typeof requestAnimationFrame === "function") {
      const handle = requestAnimationFrame(() => {
        batchRef.current = null;
        for (const listener of listenersRef.current) listener();
      });
      batchRef.current = { kind: "raf", handle };
      return;
    }
    const handle = setTimeout(() => {
      batchRef.current = null;
      for (const listener of listenersRef.current) listener();
    }, 48);
    batchRef.current = { kind: "timeout", handle };
  }, []);

  const getStreamSnapshot = useCallback(
    () => (streamKeyRef.current === key ? streamRef.current : EMPTY_TRANSIENT_STREAM),
    [key],
  );

  const subscribeStream = useCallback((listener: () => void) => {
    listenersRef.current.add(listener);
    return () => {
      listenersRef.current.delete(listener);
    };
  }, []);

  useEffect(() => {
    setState(emptyProgress(key));
    streamKeyRef.current = key;
    streamRef.current = emptyTransientStream();
    flushStreamNotify();
    if (operationId === null) return undefined;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let after = 0;
    let lastEventId: string | null = null;
    let sseRetries = 0;
    let delay = nextPollDelay(0);
    let invalidated = false;
    let terminalReceived = false;

    const schedule = (ms: number, fn: () => void) => {
      timer = setTimeout(() => {
        timer = null;
        if (!controller.signal.aborted) fn();
      }, ms);
    };

    const invalidateTerminal = () => {
      if (invalidated) return;
      invalidated = true;
      void queryClient.invalidateQueries({ queryKey: queryKeys.topic(topicId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.topics });
      void queryClient.invalidateQueries({ queryKey: ["recovery", topicId] });
    };

    const stop = (error: unknown | null) => {
      setState((prev) => (prev.key === key ? { ...prev, error, stopped: true } : prev));
    };

    const applyDelta = (msg: { call_seq: number; channel: "thinking" | "text" | "tool_status" | "draft_report"; delta: string; seq: number }) => {
      if (streamKeyRef.current !== key) return;
      let current = streamRef.current;
      if (current.callSeq !== msg.call_seq) {
        if (current.callSeq !== null && msg.call_seq < current.callSeq) return;
        current = { ...emptyTransientStream(), callSeq: msg.call_seq, retractedNotice: current.retractedNotice };
      } else if (msg.seq <= current.lastSeq) {
        return;
      }
      const next: TransientStreamState = { ...current, activeChannel: msg.channel, lastSeq: msg.seq };
      if (msg.channel === "thinking") {
        next.thinking = appendTailCap(next.thinking, msg.delta, MAX_THINKING_CHARS);
        next.retractedNotice = null;
      } else if (msg.channel === "text") {
        next.text = appendTailCap(next.text, msg.delta, MAX_TEXT_CHARS);
        next.retractedNotice = null;
      } else if (msg.channel === "tool_status") {
        next.toolStatus = msg.delta.slice(0, MAX_TOOL_STATUS_CHARS) || null;
      } else if (msg.channel === "draft_report") {
        next.draftReport = appendPrefixCap(next.draftReport, msg.delta, MAX_DRAFT_REPORT_CHARS);
        next.retractedNotice = null;
      }
      streamRef.current = next;
      scheduleStreamNotify();
    };

    const applyReset = (msg: { call_seq: number; reason: string }) => {
      if (streamKeyRef.current !== key) return;
      const current = streamRef.current;
      if (current.callSeq !== null && msg.call_seq < current.callSeq) return;
      const keepNotice =
        msg.reason === "submission_rejected"
          ? RETRACTED_SUBMISSION_NOTICE
          : msg.reason === "failed" || msg.reason === "cancelled" || msg.reason === "submission_partial"
            ? null
            : current.retractedNotice;
      streamRef.current = {
        ...emptyTransientStream(),
        callSeq: Math.max(current.callSeq ?? 0, msg.call_seq),
        lastSeq: msg.call_seq === current.callSeq ? current.lastSeq : -1,
        retractedNotice: keepNotice,
      };
      flushStreamNotify();
    };

    const poll = async () => {
      timer = null;
      try {
        const progress = await getProgress(topicId, operationId, after, controller.signal);
        if (controller.signal.aborted) return;
        after = Math.max(after, progress.event_count);
        delay = nextPollDelay(delay);
        const finished = !isActiveStatus(progress.operation_status);
        setState((prev) =>
          prev.key === key
            ? {
                ...prev,
                progress,
                events: mergeEvents(prev.events, progress.events),
                error: null,
                stopped: finished,
              }
            : prev,
        );
        if (finished) {
          streamRef.current = emptyTransientStream();
          flushStreamNotify();
          invalidateTerminal();
          return;
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) {
          stop(error);
          return;
        }
        delay = nextPollDelay(delay, error);
        setState((prev) => (prev.key === key ? { ...prev, error } : prev));
      }
      if (!controller.signal.aborted) schedule(delay, () => void poll());
    };

    const connectSse = async () => {
      const openedAt = Date.now();
      let progressed = false;
      try {
        const stream = await openResearchRunStream({
          topicId,
          operationId,
          after,
          lastEventId,
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        for await (const frame of stream) {
          if (controller.signal.aborted) return;
          if (frame.id) lastEventId = frame.id;
          if (frame.event === "progress.snapshot") {
            const snap = frame.data;
            after = Math.max(after, snap.event_count);
            const finished = !isActiveStatus(snap.operation_status);
            setState((prev) =>
              prev.key === key
                ? {
                    ...prev,
                    progress: snap,
                    events: mergeEvents(prev.events, snap.events),
                    error: null,
                    stopped: finished,
                  }
                : prev,
            );
            if (finished) {
              streamRef.current = emptyTransientStream();
              flushStreamNotify();
              terminalReceived = true;
              invalidateTerminal();
            }
          } else if (frame.event === "step.current") {
            progressed = true;
            sseRetries = 0;
            const msg = frame.data;
            setState((prev) => {
              if (prev.key !== key) return prev;
              const base = prev.progress;
              const nextProgress: RunProgress = base
                ? {
                    ...base,
                    operation_status: msg.operation_status,
                    current: msg.current,
                    usage: msg.usage,
                    interim: msg.interim ?? base.interim ?? null,
                    updated_at: msg.updated_at ?? base.updated_at ?? null,
                  }
                : {
                    operation_id: msg.operation_id,
                    operation_status: msg.operation_status,
                    turn: { index: 0, question: "" },
                    usage: msg.usage,
                    current: msg.current,
                    interim: msg.interim ?? null,
                    events: prev.events,
                    event_count: prev.events.length,
                    started_at: null,
                    updated_at: msg.updated_at ?? null,
                    version_id: null,
                  };
              return { ...prev, progress: nextProgress, error: null };
            });
          } else if (frame.event === "step.event") {
            progressed = true;
            sseRetries = 0;
            const msg = frame.data;
            after = Math.max(after, msg.event_count, msg.event.index + 1);
            setState((prev) => {
              if (prev.key !== key) return prev;
              const events = mergeEvents(prev.events, [msg.event]);
              const base = prev.progress;
              const nextProgress: RunProgress = base
                ? {
                    ...base,
                    usage: msg.usage,
                    interim: msg.interim ?? base.interim ?? null,
                    event_count: Math.max(base.event_count, msg.event_count),
                    updated_at: msg.updated_at ?? base.updated_at ?? null,
                  }
                : {
                    operation_id: msg.operation_id,
                    operation_status: "running",
                    turn: { index: 0, question: "" },
                    usage: msg.usage,
                    current: null,
                    interim: msg.interim ?? null,
                    events,
                    event_count: msg.event_count,
                    started_at: null,
                    updated_at: msg.updated_at ?? null,
                    version_id: null,
                  };
              return { ...prev, progress: nextProgress, events, error: null };
            });
          } else if (frame.event === "stream.delta") {
            progressed = true;
            sseRetries = 0;
            applyDelta(frame.data);
          } else if (frame.event === "stream.reset") {
            applyReset(frame.data);
          } else if (frame.event === "run.terminal") {
            const msg = frame.data;
            terminalReceived = true;
            streamRef.current = emptyTransientStream();
            flushStreamNotify();
            setState((prev) => {
              if (prev.key !== key) return prev;
              const base = prev.progress;
              const nextProgress: RunProgress = base
                ? {
                    ...base,
                    operation_status: msg.operation_status,
                    current: null,
                    version_id: msg.version_id ?? base.version_id,
                  }
                : {
                    operation_id: msg.operation_id,
                    operation_status: msg.operation_status,
                    turn: { index: 0, question: "" },
                    usage: { model_calls: 0, search_calls: 0, total_tokens: 0 },
                    current: null,
                    events: prev.events,
                    event_count: prev.events.length,
                    started_at: null,
                    updated_at: null,
                    version_id: msg.version_id,
                  };
              return { ...prev, progress: nextProgress, error: null, stopped: true };
            });
            invalidateTerminal();
            return;
          }
        }
        if (controller.signal.aborted || terminalReceived) return;
        if (progressed || Date.now() - openedAt >= 5000) sseRetries = 0;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) {
          stop(error);
          return;
        }
        if (
          error instanceof StreamFallbackError ||
          (error instanceof ApiError && (error.status === 405 || error.status === 429 || error.status === 501))
        ) {
          void poll();
          return;
        }
        if (error instanceof ApiError && error.status === 400) {
          lastEventId = null;
        }
        if (progressed || Date.now() - openedAt >= 5000) sseRetries = 0;
      }
      if (controller.signal.aborted) return;
      if (sseRetries < SSE_BACKOFF_MS.length) {
        const waitMs = SSE_BACKOFF_MS[sseRetries]!;
        sseRetries += 1;
        schedule(waitMs, () => void connectSse());
        return;
      }
      void poll();
    };

    void connectSse();

    return () => {
      controller.abort();
      if (timer !== null) clearTimeout(timer);
      const pending = batchRef.current;
      if (pending) {
        batchRef.current = null;
        if (pending.kind === "raf" && typeof cancelAnimationFrame === "function") {
          cancelAnimationFrame(pending.handle as number);
        } else {
          clearTimeout(pending.handle as ReturnType<typeof setTimeout>);
        }
      }
    };
  }, [topicId, operationId, key, queryClient, flushStreamNotify, scheduleStreamNotify]);

  const current = state.key === key ? state : emptyProgress(key);
  return {
    progress: current.progress,
    events: current.events,
    error: current.error,
    active: operationId !== null && !current.stopped,
    getStreamSnapshot,
    subscribeStream,
  };
}

export function useCreateResearch() {
  const queryClient = useQueryClient();
  return useMutation<
    CreateResearchResult,
    unknown,
    { input: ResearchV2.ResearchInput; run: Omit<ResearchV2.ResearchRunRequest, "parent_version_id"> }
  >({
    mutationFn: ({ input, run }) => createResearch(input, run),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.topics }),
  });
}

export function useStartTurn(topicId: string) {
  const queryClient = useQueryClient();
  return useMutation<ResearchV2.OperationResponse, unknown, ResearchV2.ResearchRunRequest>({
    mutationFn: (body) => startTurn(topicId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) }),
  });
}

export function useCancelRun(topicId: string) {
  const queryClient = useQueryClient();
  return useMutation<ResearchV2.OperationResponse, unknown, ResearchV2.Operation>({
    mutationFn: (operation) => cancelRun(operation),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) }),
  });
}

function useRecoveryMutation(topicId: string, send: (topicId: string, operationId: string) => Promise<ResearchV2.OperationResponse>) {
  const queryClient = useQueryClient();
  return useMutation<ResearchV2.OperationResponse, unknown, string>({
    mutationFn: (operationId) => send(topicId, operationId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) }),
        queryClient.invalidateQueries({ queryKey: ["recovery", topicId] }),
      ]);
    },
  });
}

export function useContinueTurn(topicId: string) {
  return useRecoveryMutation(topicId, continueTurn);
}

export function useRecoverTurn(topicId: string) {
  return useRecoveryMutation(topicId, recoverTurn);
}

/** Adds sources to a topic (a new user version) and swaps in the returned topic. */
export function useAttachSources(topicId: string) {
  const queryClient = useQueryClient();
  return useMutation<ResearchV2.ResearchTopic, unknown, { parentVersionId: string; sourceIds: readonly string[] }>({
    mutationFn: ({ parentVersionId, sourceIds }) => attachSources(topicId, parentVersionId, sourceIds),
    onSuccess: (topic) => {
      queryClient.setQueryData(queryKeys.topic(topicId), topic);
      void queryClient.invalidateQueries({ queryKey: queryKeys.topics });
    },
    // A 409 means the topic moved on; refetch so the next attempt uses the latest version.
    onError: () => queryClient.invalidateQueries({ queryKey: queryKeys.topic(topicId) }),
  });
}

/** Re-extracts a retained PDF in another text order; the key survives retries of the same request. */
export function useReextractPdf() {
  return useMutation<CreatedSource, unknown, { sourceId: string; textOrder: "layout" | "reading" }>({
    mutationFn: ({ sourceId, textOrder }) =>
      idempotentWrite(`reextract:${sourceId}:${textOrder}`, { text_order: textOrder }, (key) => reextractPdfSource(sourceId, textOrder, key)),
  });
}

function useTopicWrite<TVariables>(topicId: string, send: (variables: TVariables) => Promise<ResearchV2.ResearchTopic>) {
  const queryClient = useQueryClient();
  return useMutation<ResearchV2.ResearchTopic, unknown, TVariables>({
    mutationFn: send,
    onSuccess: (topic) => {
      queryClient.setQueryData(queryKeys.topic(topicId), topic);
      void queryClient.invalidateQueries({ queryKey: queryKeys.topics });
    },
    // A 409 means the topic moved on; refetch so the user sees the latest version.
    onError: () => queryClient.invalidateQueries({ queryKey: queryKeys.topic(topicId) }),
  });
}

/** Saves review changes as a new user version (spec §4.7 保存为新版本). */
export function useSaveVersion(topicId: string) {
  return useTopicWrite<ResearchV2.VersionInput>(topicId, (input) => createVersion(topicId, input));
}

export function useCreateCalculation(topicId: string) {
  return useTopicWrite<CreateCalculationRequest>(topicId, (body) => createCalculation(topicId, body));
}

export function useRecalculate(topicId: string) {
  return useTopicWrite<{ parentVersionId: string; index: number }>(topicId, ({ parentVersionId, index }) => recalculateCalculation(topicId, parentVersionId, index));
}
