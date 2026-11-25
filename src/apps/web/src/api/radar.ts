import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, newIdempotencyKey } from "./http";

export type ThesisPillar = "growth" | "margin" | "cash_quality" | "capital_allocation" | "valuation" | "risk";
export type ThesisStance = "bull" | "cautious" | "bear" | "neutral";
export type ClaimStatus = "active" | "strengthened" | "weakened" | "invalidated" | "superseded";
export type WatchHealthStatus = "healthy" | "degraded" | "error";
export type InboxImpactLevel = "critical" | "high" | "medium" | "low";
export type InboxDirection = "positive" | "negative" | "mixed" | "neutral";
export type InboxReviewState = "unread" | "read" | "accepted" | "dismissed_with_note";
export type InboxDecisionAction = "accept" | "edit_and_accept" | "keep_original" | "mark_read";

export interface WatchTrigger {
  trigger_id: string;
  kind: "metric_rule" | "disclosure_rule";
  description: string;
  metric_key?: string | null;
  operator?: string | null;
  threshold_decimal?: string | null;
  horizon?: string | null;
  target_sections?: string[];
  last_eval_status?: string;
  last_observed_value?: string | null;
}

export interface ThesisClaim {
  claim_id: string;
  pillar: ThesisPillar;
  statement: string;
  stance: ThesisStance;
  watch_triggers: WatchTrigger[];
  invalidation_condition: string;
  status: ClaimStatus;
  evidence_refs: string[];
  user_note?: string | null;
}

export interface ThesisLedger {
  ledger_id: string;
  account_id: string;
  topic_id: string;
  entity_id: string;
  symbol?: string | null;
  cik?: string | null;
  company_name?: string | null;
  version: number;
  parent_version?: number | null;
  review_status: "draft" | "confirmed";
  base_dossier_id?: string | null;
  base_period_label?: string | null;
  claims: ThesisClaim[];
  active_red_yellow_flags: string[];
  source_inbox_item_id?: string | null;
  decision_summary?: string | null;
  confirmed_at?: string | null;
  created_at: string;
}

export interface ClaimDiffEntry {
  claim_id: string;
  pillar: ThesisPillar;
  change_kind: "added" | "removed" | "modified" | "unchanged";
  old_statement?: string | null;
  new_statement?: string | null;
  old_status?: ClaimStatus | null;
  new_status?: ClaimStatus | null;
  user_note?: string | null;
}

export interface ThesisLedgerDiff {
  topic_id: string;
  from_version: number;
  to_version: number;
  entries: ClaimDiffEntry[];
  decision_summary?: string | null;
}

export interface ThesisLedgerHistoryView {
  topic_id: string;
  latest_confirmed?: ThesisLedger | null;
  pending_draft?: ThesisLedger | null;
  versions: ThesisLedger[];
  latest_diff?: ThesisLedgerDiff | null;
}

export interface FilingWatchSubscription {
  subscription_id: string;
  account_id: string;
  topic_id?: string | null;
  cik: string;
  symbol?: string | null;
  company_name?: string | null;
  enabled: boolean;
  watched_channels: ("v1_events" | "earnings_delta" | "peer_relative")[];
  lookback_days: number;
  check_interval_minutes: number;
  last_checked_at?: string | null;
  last_signal_at?: string | null;
  health_status: WatchHealthStatus;
  last_error?: string | null;
}

export interface SubscriptionHealthView {
  subscription_id: string;
  topic_id?: string | null;
  cik: string;
  symbol?: string | null;
  company_name?: string | null;
  enabled: boolean;
  health_status: WatchHealthStatus;
  last_checked_at?: string | null;
  last_signal_at?: string | null;
  last_error?: string | null;
  failed_accession_count: number;
}

export interface RadarHealthSummary {
  total_subscriptions: number;
  enabled_subscriptions: number;
  healthy_count: number;
  degraded_count: number;
  error_count: number;
  unread_inbox_count: number;
  last_checked_at?: string | null;
  subscriptions: SubscriptionHealthView[];
}

export interface SignalQuoteView {
  source_id: string;
  accession?: string | null;
  location?: string | null;
  quotation: string;
  verified?: boolean;
  quote_context_warning?: string | null;
}

export interface ChangeInboxItem {
  inbox_item_id: string;
  account_id: string;
  subscription_id?: string | null;
  topic_id?: string | null;
  cik: string;
  symbol?: string | null;
  company_name?: string | null;
  source_channel: "v1_events" | "earnings_delta" | "peer_relative";
  event_thread_key: string;
  accession?: string | null;
  superseded_by_accession?: string | null;
  headline: string;
  summary: string;
  impact_level: InboxImpactLevel;
  direction: InboxDirection;
  affected_pillars: string[];
  linked_claim_ids: string[];
  skeptic_dissent?: string | null;
  signal_envelope?: {
    verbatim_quotes?: SignalQuoteView[];
    provenance?: { form?: string; sec_url?: string };
    grounding_status?: string;
  } | null;
  scorecard_diff?: Record<string, unknown> | null;
  review_state: InboxReviewState;
  user_decision_note?: string | null;
  resulting_ledger_version?: number | null;
  disclosed_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChangeInboxListResponse {
  items: ChangeInboxItem[];
  unread_count: number;
  total_count: number;
}

export interface RadarSweepOperationResponse {
  operation: { operation_id: string; status: string };
  sweep?: {
    subscriptions_checked: number;
    l1_zero_change_count: number;
    l2_queued_count: number;
    inbox_items_created: number;
    inbox_items_updated: number;
    degraded_count: number;
    error_count: number;
  } | null;
  error?: { message?: string } | null;
}

export const radarKeys = {
  health: ["radar", "health"] as const,
  subscriptions: ["radar", "subscriptions"] as const,
  inbox: (filters?: { symbol?: string; impact?: string; state?: string }) =>
    ["radar", "inbox", filters ?? {}] as const,
  ledger: (topicId: string) => ["radar", "ledger", topicId] as const,
};

export function useRadarHealth(enabled = true) {
  return useQuery({
    queryKey: radarKeys.health,
    queryFn: () => apiRequest<RadarHealthSummary>("/v2/research/radar/health"),
    enabled,
    staleTime: 30_000,
  });
}

export function useRadarSubscriptions(enabled = true) {
  return useQuery({
    queryKey: radarKeys.subscriptions,
    queryFn: () => apiRequest<FilingWatchSubscription[]>("/v2/research/radar/subscriptions"),
    enabled,
    staleTime: 30_000,
  });
}

export function useChangeInbox(filters?: { symbol?: string; impact?: string; state?: string }) {
  const qs = new URLSearchParams();
  if (filters?.symbol?.trim()) qs.set("symbol", filters.symbol.trim());
  if (filters?.impact && filters.impact !== "all") qs.set("impact_level", filters.impact);
  if (filters?.state && filters.state !== "all") qs.set("review_state", filters.state);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return useQuery({
    queryKey: radarKeys.inbox(filters),
    queryFn: () => apiRequest<ChangeInboxListResponse>(`/v2/research/inbox${suffix}`),
    staleTime: 15_000,
  });
}

export function useTopicThesisLedger(topicId: string | undefined) {
  return useQuery({
    queryKey: radarKeys.ledger(topicId ?? ""),
    queryFn: () => apiRequest<ThesisLedgerHistoryView>(`/v2/research/topics/${encodeURIComponent(topicId!)}/thesis-ledger`),
    enabled: Boolean(topicId),
    staleTime: 30_000,
  });
}

export function useRadarMutations() {
  const qc = useQueryClient();
  const invalidateRadar = () => {
    void qc.invalidateQueries({ queryKey: ["radar"] });
  };

  const createSubscription = useMutation({
    mutationFn: (payload: { symbol?: string; cik?: string; company_name?: string; topic_id?: string; lookback_days?: number }) =>
      apiRequest<FilingWatchSubscription>("/v2/research/radar/subscriptions", { method: "POST", body: payload }),
    onSuccess: invalidateRadar,
  });

  const patchSubscription = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: { enabled?: boolean; lookback_days?: number } }) =>
      apiRequest<FilingWatchSubscription>(`/v2/research/radar/subscriptions/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: patch,
      }),
    onSuccess: invalidateRadar,
  });

  const deleteSubscription = useMutation({
    mutationFn: (id: string) =>
      apiRequest<void>(`/v2/research/radar/subscriptions/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSuccess: invalidateRadar,
  });

  const checkNow = useMutation({
    mutationFn: (payload: { subscription_ids?: string[] } = {}) =>
      apiRequest<{ operation_id: string; status: string }>("/v2/research/radar/check-now", {
        method: "POST",
        body: payload,
        idempotencyKey: newIdempotencyKey(),
      }),
    onSuccess: invalidateRadar,
  });

  const decideInboxItem = useMutation({
    mutationFn: ({
      inboxItemId,
      action,
      user_note,
    }: {
      inboxItemId: string;
      action: InboxDecisionAction;
      user_note?: string;
    }) =>
      apiRequest<{ item: ChangeInboxItem; new_ledger?: ThesisLedger | null }>(
        `/v2/research/inbox/${encodeURIComponent(inboxItemId)}/decide`,
        { method: "POST", body: { action, user_note: user_note || undefined } },
      ),
    onSuccess: invalidateRadar,
  });

  const generateLedgerDraft = useMutation({
    mutationFn: (topicId: string) =>
      apiRequest<ThesisLedger>(`/v2/research/topics/${encodeURIComponent(topicId)}/thesis-ledger/draft`, {
        method: "POST",
        body: {},
      }),
    onSuccess: invalidateRadar,
  });

  const confirmLedger = useMutation({
    mutationFn: ({
      topicId,
      claims,
      decision_summary,
    }: {
      topicId: string;
      claims: ThesisClaim[];
      decision_summary?: string;
    }) =>
      apiRequest<ThesisLedger>(`/v2/research/topics/${encodeURIComponent(topicId)}/thesis-ledger/confirm`, {
        method: "POST",
        body: { claims, decision_summary },
      }),
    onSuccess: invalidateRadar,
  });

  return {
    createSubscription,
    patchSubscription,
    deleteSubscription,
    checkNow,
    decideInboxItem,
    generateLedgerDraft,
    confirmLedger,
  };
}

export async function pollRadarSweepOperation(operationId: string): Promise<RadarSweepOperationResponse> {
  return apiRequest<RadarSweepOperationResponse>(`/v2/research/radar/check-now/${encodeURIComponent(operationId)}`);
}
