import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelCallAuditOperation,
  checkCallAuditFilings,
  getCallAuditOperation,
  getCallAuditReport,
  mergeCallAuditEntities,
  openCallAuditEventStream,
  patchCallAuditCommitment,
  previewCallAudit,
  runCallAudit,
  type CallVerificationReport,
  type CollisionSeverity,
  type GroundedQuoteRef,
} from "../../api/callVerification";
import { showToast } from "../../ui/toast";
import { CallVerificationPanel } from "./CallVerificationPanel";

export interface AttachedSourceTrigger {
  sourceIds: string[];
  seq: number;
}

export interface CallVerificationBarProps {
  topicId: string;
  sourceIds: readonly string[];
  symbol?: string | null;
  cik?: string | null;
  asOfIso?: string | null;
  locked?: boolean;
  initialExpanded?: boolean;
  attachedTrigger?: AttachedSourceTrigger | null;
  onAddSources: () => void;
  onInspectQuote: (quote: GroundedQuoteRef) => void;
}

const CHECK_FILINGS_COOLDOWN_MS = 30 * 60 * 1000;

const LIGHT_META: Record<CollisionSeverity, { label: string; cls: string }> = {
  red: { label: "警示", cls: "peer-light peer-light-red" },
  yellow: { label: "留意", cls: "peer-light peer-light-yellow" },
  green: { label: "印证", cls: "peer-light peer-light-green" },
  info: { label: "线索", cls: "peer-light peer-light-na" },
  not_applicable: { label: "不适用", cls: "peer-light peer-light-na" },
};

const STAGE_LABEL: Record<string, string> = {
  queued: "排队中",
  starting: "正在初始化",
  layer0_ready: "Layer 0 文档结构解析完成",
  layer0b_ocr: "Layer 0b 关键图表页 OCR 中",
  layer1_ready: "Layer 1 SEC 10-Q 账本对齐完成",
  layer2_extracting: "Layer 2 语义提取与 7 轨碰撞中",
  completed: "核验完成",
  cancelled: "已取消",
  failed: "核验失败",
};

function isCheckFilingsDue(lastCheckedAt?: string | null): boolean {
  if (!lastCheckedAt) return true;
  const parsed = Date.parse(lastCheckedAt);
  if (!Number.isFinite(parsed)) return true;
  return Date.now() - parsed > CHECK_FILINGS_COOLDOWN_MS;
}

/**
 * V3a Call Verification & Expectation-Gap Bar mounted on BOTH normal Topics and Squad Topics.
 *
 * Strict Mount Zero-Outbound Contract:
 * - On mount calls ONLY `GET /v2/research/call-audit/report?topic_id=...&cache_only=true`.
 * - Fires `POST /v2/research/call-audit/check-filings` ONLY asynchronously after initial paint
 *   when `awaiting_current_10q === true` AND `last_filing_fingerprint_check_at` is absent or >30m old.
 */
export function CallVerificationBar({
  topicId,
  sourceIds,
  symbol,
  cik,
  asOfIso,
  locked = false,
  initialExpanded = false,
  attachedTrigger = null,
  onAddSources,
  onInspectQuote,
}: CallVerificationBarProps) {
  const safeSourceIds = Array.isArray(sourceIds) ? sourceIds : [];
  const [report, setReport] = useState<CallVerificationReport | null>(null);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<boolean>(initialExpanded);
  const [runningOpId, setRunningOpId] = useState<string | null>(null);
  const [stageText, setStageText] = useState<string | null>(null);
  const [autoBanner, setAutoBanner] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const streamAbortRef = useRef<AbortController | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSniffSeqRef = useRef<number>(0);

  const stopLiveTracking = useCallback(() => {
    if (streamAbortRef.current) {
      streamAbortRef.current.abort();
      streamAbortRef.current = null;
    }
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => () => stopLiveTracking(), [stopLiveTracking]);

  useEffect(() => {
    if (initialExpanded) setExpanded(true);
  }, [initialExpanded]);

  const loadCachedReport = useCallback(
    async (signal?: AbortSignal) => {
      const env = await getCallAuditReport(
        {
          topic_id: topicId,
          ...(symbol ? { symbol } : {}),
          cache_only: true,
          ...(asOfIso ? { as_of: asOfIso.slice(0, 10) } : {}),
        },
        signal,
      );
      if (signal?.aborted) return env;
      if (env.available && env.report) {
        setReport(env.report);
        setMarkdown(env.markdown ?? null);
      }
      return env;
    },
    [topicId, symbol, asOfIso],
  );

  // Mount: strictly cache_only GET /report; conditionally trigger /check-filings ONLY if awaiting_current_10q & >30m cooldown.
  useEffect(() => {
    if (!topicId) return undefined;
    const ctrl = new AbortController();
    let checkTimer: ReturnType<typeof setTimeout> | null = null;
    void loadCachedReport(ctrl.signal)
      .then((env) => {
        if (ctrl.signal.aborted || !env) return;
        if (env.awaiting_current_10q && isCheckFilingsDue(env.last_filing_fingerprint_check_at)) {
          checkTimer = setTimeout(() => {
            if (ctrl.signal.aborted) return;
            void checkCallAuditFilings({ topic_id: topicId })
              .then((res) => {
                if (ctrl.signal.aborted) return;
                if (res.triggered_ciks.length > 0) {
                  showToast("检测到最新 10-Q/10-K 已归档，已自动完成电话会承诺重核。");
                  void loadCachedReport(ctrl.signal).catch(() => undefined);
                }
              })
              .catch(() => undefined);
          }, 0);
        }
      })
      .catch(() => undefined);
    return () => {
      ctrl.abort();
      if (checkTimer) clearTimeout(checkTimer);
    };
  }, [topicId, loadCachedReport]);

  const finishOperation = useCallback(async (opId: string) => {
    try {
      const op = await getCallAuditOperation(opId);
      if (op.status === "completed" && op.report) {
        setReport(op.report);
        setMarkdown(op.markdown ?? null);
        setRunningOpId(null);
        setStageText(null);
        setAutoBanner(null);
        setErrorMsg(null);
        return true;
      }
      if (op.status === "failed" || op.status === "cancelled") {
        setRunningOpId(null);
        setStageText(null);
        setAutoBanner(null);
        if (op.status === "failed") setErrorMsg(op.error || "电话会核验未能完成，请稍后重试。");
        return true;
      }
      if (op.stage) setStageText(STAGE_LABEL[op.stage] ?? op.stage);
      return false;
    } catch {
      return false;
    }
  }, []);

  const trackOperation = useCallback(
    (opId: string) => {
      stopLiveTracking();
      const ctrl = new AbortController();
      streamAbortRef.current = ctrl;
      setRunningOpId(opId);
      setStageText(STAGE_LABEL.queued ?? "排队中");
      setErrorMsg(null);

      const startPollingFallback = () => {
        if (ctrl.signal.aborted) return;
        const tick = async () => {
          if (ctrl.signal.aborted) return;
          const done = await finishOperation(opId);
          if (!done && !ctrl.signal.aborted) {
            pollTimerRef.current = setTimeout(() => void tick(), 3000);
          }
        };
        pollTimerRef.current = setTimeout(() => void tick(), 1500);
      };

      void (async () => {
        let terminalSeen = false;
        try {
          const stream = openCallAuditEventStream({ operationId: opId, after: 0, signal: ctrl.signal });
          for await (const frame of stream) {
            if (ctrl.signal.aborted) return;
            if (frame.event === "run.terminal") {
              terminalSeen = true;
              await finishOperation(opId);
              return;
            }
            const stageName = typeof frame.data.stage === "string" ? frame.data.stage : frame.event;
            setStageText(STAGE_LABEL[stageName] ?? stageName);
          }
        } catch {
          // SSE unavailable or dropped; fall through to 3s polling
        }
        if (!terminalSeen && !ctrl.signal.aborted) {
          startPollingFallback();
        }
      })();
    },
    [finishOperation, stopLiveTracking],
  );

  const startVerification = useCallback(
    async (opts?: {
      targetSourceIds?: string[];
      symbol?: string;
      fiscal_year?: number;
      fiscal_quarter?: number;
      autoTriggered?: boolean;
      expandOnComplete?: boolean;
    }) => {
      const ids = opts?.targetSourceIds && opts.targetSourceIds.length > 0 ? opts.targetSourceIds : safeSourceIds;
      if (ids.length === 0) {
        onAddSources();
        return;
      }
      setErrorMsg(null);
      if (opts?.autoTriggered) {
        setAutoBanner("已识别电话会材料，正在后台执行 4 层核验…");
      }
      try {
        const res = await runCallAudit({
          source_ids: ids,
          topic_id: topicId,
          ...(opts?.symbol ? { symbol: opts.symbol } : symbol ? { symbol } : {}),
          ...(cik ? { cik } : {}),
          ...(opts?.fiscal_year ? { fiscal_year: opts.fiscal_year } : {}),
          ...(opts?.fiscal_quarter ? { fiscal_quarter: opts.fiscal_quarter } : {}),
          ...(asOfIso ? { as_of: asOfIso.slice(0, 10) } : {}),
        });
        if (res.status === "completed" && res.report) {
          setReport(res.report);
          setMarkdown(res.markdown ?? null);
          setAutoBanner(null);
          setRunningOpId(null);
          setStageText(null);
          if (opts?.expandOnComplete) setExpanded(true);
          return;
        }
        if (res.operation_id) {
          if (opts?.expandOnComplete) setExpanded(true);
          trackOperation(res.operation_id);
        }
      } catch (err) {
        setAutoBanner(null);
        setRunningOpId(null);
        const msg = err instanceof Error ? err.message : "启动电话会核验失败";
        setErrorMsg(msg);
        showToast(`电话会核验失败：${msg}`, { tone: "error" });
      }
    },
    [safeSourceIds, topicId, symbol, cik, asOfIso, onAddSources, trackOperation],
  );

  // R2-D1: Automatic 0-LLM <150ms sniff whenever user attaches new source(s) to this Topic
  useEffect(() => {
    if (!attachedTrigger || attachedTrigger.seq <= lastSniffSeqRef.current) return;
    if (attachedTrigger.sourceIds.length === 0) return;
    lastSniffSeqRef.current = attachedTrigger.seq;
    const ctrl = new AbortController();
    const combinedIds = Array.from(new Set([...safeSourceIds, ...attachedTrigger.sourceIds]));
    void previewCallAudit(combinedIds, ctrl.signal)
      .then((preview) => {
        if (ctrl.signal.aborted) return;
        if (preview.auto_trigger_eligible) {
          void startVerification({
            targetSourceIds: combinedIds,
            autoTriggered: true,
            expandOnComplete: false,
          });
        }
      })
      .catch(() => undefined);
    return () => ctrl.abort();
  }, [attachedTrigger, safeSourceIds, startVerification]);

  const handleCancel = async () => {
    if (!runningOpId || cancelling) return;
    setCancelling(true);
    try {
      await cancelCallAuditOperation(runningOpId);
      stopLiveTracking();
      setRunningOpId(null);
      setStageText(null);
      setAutoBanner(null);
      showToast("已取消电话会核验");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "取消失败";
      showToast(`取消核验失败：${msg}`, { tone: "error" });
    } finally {
      setCancelling(false);
    }
  };

  const handleToggleMuteCommitment = async (commitmentId: string, muted: boolean) => {
    if (!report) return;
    try {
      await patchCallAuditCommitment(commitmentId, report.entity_key, muted);
      setReport((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          commitments: prev.commitments.map((c) =>
            c.commitment_id === commitmentId
              ? { ...c, muted, status: muted ? "muted" : c.status === "muted" ? "pending_10q" : c.status }
              : c,
          ),
        };
      });
      showToast(muted ? "已忽略该承诺跟踪" : "已恢复该承诺跟踪");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "操作失败";
      showToast(`更新承诺状态失败：${msg}`, { tone: "error" });
    }
  };

  const handleMergeEntityAlias = async (targetEntityKey: string, aliasLabel?: string) => {
    if (!report) return;
    try {
      const res = await mergeCallAuditEntities({
        source_entity_key: report.entity_key,
        target_entity_key: targetEntityKey,
        ...(aliasLabel ? { aliases: [aliasLabel] } : {}),
      });
      showToast(`已合并至 ${res.target_entity_key}（合并 ${res.merged_commitments} 条历史承诺）`);
      await loadCachedReport();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "实体合并失败";
      showToast(`实体合并失败：${msg}`, { tone: "error" });
    }
  };

  const handleCopyMarkdown = () => {
    if (!markdown) return;
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      void navigator.clipboard
        .writeText(markdown)
        .then(() => showToast("已复制 Markdown 电话会审计简报"))
        .catch(() => showToast("复制失败，请手动导出", { tone: "error" }));
    } else {
      showToast("当前环境不支持剪贴板写入", { tone: "error" });
    }
  };

  const isBusy = Boolean(runningOpId);
  const light = report ? LIGHT_META[report.overall_light] ?? LIGHT_META.info : null;

  return (
    <section className="v3a-wrap" aria-label="电话会 Q&A 核验与预期差" data-testid="call-verification-bar">
      <div className="peer-coord-bar v3a-bar">
        <div className="peer-coord-left">
          <span className="peer-coord-tag">电话会核验</span>
          {report && light ? (
            <>
              <span className={light.cls}>{light.label}</span>
              <span className="peer-coord-summary">{report.headline}</span>
              {report.awaiting_current_10q ? <span className="v3a-meta-pill">待 10-Q 自动跟踪</span> : null}
            </>
          ) : isBusy ? (
            <span className="v3a-progress-pill">{stageText ?? "正在执行电话会核验…"}</span>
          ) : (
            <span className="peer-coord-muted">
              {safeSourceIds.length > 0
                ? `已挂载 ${safeSourceIds.length} 份资料，可核验管理层表述、Q&A 回避与 10-Q 账本差异`
                : "上传电话会纪要、PPTX 路演稿、字幕或卖方研报即可核验言行差异"}
            </span>
          )}
        </div>

        <div className="peer-coord-actions">
          {isBusy ? (
            <>
              {report && stageText ? <span className="v3a-progress-pill">{stageText}</span> : null}
              <button
                type="button"
                className="btn peer-coord-btn"
                disabled={cancelling}
                onClick={() => void handleCancel()}
              >
                {cancelling ? "正在取消…" : "取消"}
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn peer-coord-btn"
                disabled={locked}
                onClick={onAddSources}
              >
                追加材料
              </button>
              <button
                type="button"
                className={report ? "btn peer-coord-btn" : "btn primary peer-coord-btn"}
                disabled={safeSourceIds.length === 0}
                onClick={() => void startVerification({ expandOnComplete: true })}
              >
                {report ? "重新核验" : "开始核验"}
              </button>
              {report ? (
                <button
                  type="button"
                  className="btn peer-coord-btn"
                  aria-expanded={expanded}
                  onClick={() => setExpanded((v) => !v)}
                >
                  {expanded ? "收起核验面板" : "展开核验面板"}
                </button>
              ) : null}
            </>
          )}
        </div>
      </div>

      {autoBanner && isBusy ? (
        <div className="v3a-auto-banner" role="status">
          <span>{autoBanner}</span>
          {stageText ? <span className="v3a-progress-pill">{stageText}</span> : null}
          <button type="button" className="btn plain peer-coord-btn" disabled={cancelling} onClick={() => void handleCancel()}>
            取消
          </button>
        </div>
      ) : null}

      {errorMsg ? (
        <div className="v3a-error-banner" role="alert">
          {errorMsg}
        </div>
      ) : null}

      {report && expanded ? (
        <CallVerificationPanel
          report={report}
          markdown={markdown}
          busy={isBusy}
          onInspectQuote={onInspectQuote}
          onToggleMuteCommitment={handleToggleMuteCommitment}
          onOverrideAndRerun={(ov) =>
            void startVerification({
              ...ov,
              expandOnComplete: true,
            })
          }
          onMergeEntityAlias={handleMergeEntityAlias}
          onCopyMarkdown={handleCopyMarkdown}
        />
      ) : null}
    </section>
  );
}
