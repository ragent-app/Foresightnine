import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ResearchV2 } from "@foresightnine/contracts";
import { nextPollDelay } from "../lib/polling";
import { ApiError } from "./http";
import { getProgress, getRecoveryStatus, getTopic, getVersionSourceText, isActiveStatus, listRuns, listTopics, listVersionSources } from "./research";
import type { ProgressEvent, RecoveryStatus, RunProgress, SourceText, VersionSource } from "./types";
import { cancelRun, continueTurn, createResearch, recoverTurn, startTurn } from "./writes";
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

  useEffect(() => {
    setState(emptyProgress(key));
    if (operationId === null) return undefined;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let after = 0;
    let delay = nextPollDelay(0);

    const stop = (error: unknown | null) => {
      setState((prev) => (prev.key === key ? { ...prev, error, stopped: true } : prev));
    };

    const poll = async () => {
      timer = null;
      try {
        const progress = await getProgress(topicId, operationId, after, controller.signal);
        if (controller.signal.aborted) return;
        after = progress.event_count;
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
          void queryClient.invalidateQueries({ queryKey: queryKeys.topic(topicId) });
          void queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) });
          void queryClient.invalidateQueries({ queryKey: queryKeys.topics });
          void queryClient.invalidateQueries({ queryKey: ["recovery", topicId] });
          return;
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 404 || error.status === 403)) {
          stop(error);
          return;
        }
        delay = nextPollDelay(delay, error);
        setState((prev) => (prev.key === key ? { ...prev, error } : prev));
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), delay);
    };

    void poll();

    return () => {
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [topicId, operationId, key, queryClient]);

  const current = state.key === key ? state : emptyProgress(key);
  return {
    progress: current.progress,
    events: current.events,
    error: current.error,
    active: operationId !== null && !current.stopped,
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
