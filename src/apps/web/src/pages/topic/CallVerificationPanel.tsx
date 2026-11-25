import { useMemo, useState } from "react";
import {
  formatQuoteLocationBadge,
  type CallDefaultBasis,
  type CallVerificationReport,
  type CollisionFinding,
  type CollisionSeverity,
  type CommitmentStatus,
  type ExpectationComparisonItem,
  type GroundedQuoteRef,
  type LedgerMatchStatus,
  type QaEvasionFinding,
  type VerbalCommitmentRecord,
} from "../../api/callVerification";

export type CallAuditTab = "collisions" | "evasion" | "commitments";

export interface CallVerificationPanelProps {
  report: CallVerificationReport;
  markdown?: string | null;
  busy?: boolean;
  onInspectQuote: (quote: GroundedQuoteRef) => void;
  onToggleMuteCommitment: (commitmentId: string, muted: boolean) => Promise<void>;
  onOverrideAndRerun: (override: { symbol?: string; fiscal_year?: number; fiscal_quarter?: number }) => void;
  onMergeEntityAlias: (targetEntityKey: string, aliasLabel?: string) => Promise<void>;
  onCopyMarkdown: () => void;
}

const SEVERITY_META: Record<CollisionSeverity, { label: string; cls: string }> = {
  red: { label: "警示", cls: "peer-light peer-light-red" },
  yellow: { label: "留意", cls: "peer-light peer-light-yellow" },
  green: { label: "印证", cls: "peer-light peer-light-green" },
  info: { label: "线索", cls: "peer-light peer-light-na" },
  not_applicable: { label: "不适用", cls: "peer-light peer-light-na" },
};

const LEDGER_STATUS_LABEL: Record<LedgerMatchStatus, string> = {
  same_period_10q_matched: "本期 10-Q 已核验",
  awaiting_current_10q_using_8k_and_prior_10q: "待本期 10-Q 归档（8-K + 上期 10-Q，已启用自动跟踪）",
  fpi_20f_6k_limited: "FPI 20-F/6-K 有限核验",
  financial_sic_gated: "金融股口径豁免",
  non_sec_entity: "非 SEC 实体（内部一致性 / 跨季承诺核验）",
};

const BASIS_LABEL: Record<CallDefaultBasis, string> = {
  non_gaap: "会议默认 Non-GAAP",
  gaap: "会议默认 GAAP",
  mixed: "会议混合口径",
  unspecified: "未声明默认口径",
};

const COLLISION_KIND_LABEL: Record<string, string> = {
  direct_contradiction: "账本冲突",
  ceo_cfo_divergence: "高管口径分歧",
  internal_walkback: "内部回撤",
  basis_divergence_nongaap_gap: "Non-GAAP/GAAP 调节差",
  period_scope_mismatch: "期间口径差异",
  currency_mismatch: "币种差异",
  filing_10q_omission: "10-Q 新增风险遗漏",
  awaiting_10q: "待 10-Q 核验",
  corroborated: "账本印证",
  observation_only_insufficient_evidence: "观察线索（证据不足）",
  not_applicable: "不适用",
};

const EVASION_LEVEL_LABEL: Record<QaEvasionFinding["evasion_level"], string> = {
  fully_evasive: "明显回避",
  intermediate: "部分回应",
  direct: "直接回答",
};

const EVASION_SUBTYPE_LABEL: Record<QaEvasionFinding["behavior_subtype"], string> = {
  none: "无回避",
  omission: "略过要点",
  selective_part_omission: "选择性遗漏子问题",
  vagueness_obfuscation: "模糊措辞",
  topic_pivot: "转移话题",
  restatement: "复述原话",
};

const COMMITMENT_STATUS_META: Record<CommitmentStatus, { label: string; cls: string }> = {
  missed: { label: "违背承诺", cls: "peer-light peer-light-red" },
  silent_drop_high_risk: { label: "高危 Silent Drop", cls: "peer-light peer-light-red" },
  manual_nongaap_review: { label: "待人工核对 Non-GAAP", cls: "peer-light peer-light-yellow" },
  pending_10q: { label: "待 10-Q 验证", cls: "peer-light peer-light-yellow" },
  pending_next_call: { label: "待下季电话会跟踪", cls: "peer-light peer-light-na" },
  met: { label: "已兑现", cls: "peer-light peer-light-green" },
  likely_concluded_or_unasked: { label: "可能已完结/未再被问", cls: "peer-light peer-light-na" },
  muted: { label: "已忽略", cls: "peer-light peer-light-na" },
};

const EXPECTATION_TIER_META: Record<ExpectationComparisonItem["tier"], { label: string; badgeCls: string }> = {
  v25_historical_credibility: { label: "V2.5 历史指引信用 · SEC 公开先验", badgeCls: "v3a-tier-badge v3a-tier-sec" },
  analyst_cluster_hotspot: { label: "分析师集群热区 · 电话会 Q&A 信号", badgeCls: "v3a-tier-badge v3a-tier-qa" },
  sellside_third_party: { label: "卖方研报预期 · 第三方观点", badgeCls: "v3a-tier-badge v3a-tier-sellside" },
  v3b_byok_consensus: { label: "BYOK 一致预期", badgeCls: "v3a-tier-badge v3a-tier-byok" },
};

function QuoteButton({
  quote,
  label,
  onInspect,
}: {
  quote: GroundedQuoteRef;
  label?: string;
  onInspect: (quote: GroundedQuoteRef) => void;
}) {
  const loc = formatQuoteLocationBadge(quote);
  return (
    <div className="v3a-quote-block">
      <button
        type="button"
        className="v3a-quote-btn"
        onClick={() => onInspect(quote)}
        title="点击在原文阅读器中定位该引文"
      >
        <span className="v3a-quote-head">
          {label ? <span className="v3a-quote-role">{label}</span> : null}
          <span className={quote.verified ? "quote-verify ok" : "quote-verify warn"}>
            {quote.verified ? "原文已核验" : "待核验"}
          </span>
          {loc ? <span className="v3a-loc-pill">{loc}</span> : null}
        </span>
        <span className="v3a-quote-text">“{quote.quotation}”</span>
      </button>
      {quote.quote_context_warning ? (
        <div className="quote-warn-note">语境提示：{quote.quote_context_warning}</div>
      ) : null}
    </div>
  );
}

function CollisionCard({
  finding,
  onInspectQuote,
}: {
  finding: CollisionFinding;
  onInspectQuote: (quote: GroundedQuoteRef) => void;
}) {
  const sev = SEVERITY_META[finding.severity] ?? SEVERITY_META.info;
  return (
    <article className={`v3a-collision-card v3a-sev-${finding.severity}`}>
      <header className="v3a-card-head">
        <span className={sev.cls}>{sev.label}</span>
        <span className="v3a-kind-pill">{COLLISION_KIND_LABEL[finding.collision_kind] ?? finding.collision_kind}</span>
        {finding.basis_badge ? <span className="v3a-meta-pill">{finding.basis_badge}</span> : null}
        {finding.period_badge ? <span className="v3a-meta-pill">{finding.period_badge}</span> : null}
        <h4 className="v3a-card-title">{finding.title}</h4>
      </header>

      <div className="v3a-split-grid">
        <div className="v3a-col-mgmt">
          <div className="v3a-col-label">管理层表述（Talk）</div>
          <p className="v3a-body-text">{finding.management_statement}</p>
          {finding.quotes.length > 0 ? (
            <div className="v3a-quote-list">
              {finding.quotes.map((q, idx) => (
                <QuoteButton key={`${finding.finding_id}-q-${idx}`} quote={q} onInspect={onInspectQuote} />
              ))}
            </div>
          ) : null}
        </div>
        <div className="v3a-col-ledger">
          <div className="v3a-col-label">SEC 账本与客观证据（Walk）</div>
          <p className="v3a-body-text">{finding.ledger_evidence}</p>
          {finding.formula_note ? (
            <div className="v3a-formula-note">计算与口径：{finding.formula_note}</div>
          ) : null}
          {finding.restated_later && finding.restatement_note ? (
            <div className="v3a-restatement-note">历史重述提示：{finding.restatement_note}</div>
          ) : null}
          {finding.skeptic_note ? (
            <div className="skeptic-callout v3a-compact-skeptic">
              <strong>Skeptic 校验：</strong>
              <span>{finding.skeptic_note}</span>
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function EvasionCard({
  item,
  onInspectQuote,
}: {
  item: QaEvasionFinding;
  onInspectQuote: (quote: GroundedQuoteRef) => void;
}) {
  const sev = SEVERITY_META[item.display_severity] ?? SEVERITY_META.info;
  return (
    <article className={`v3a-evasion-card v3a-sev-${item.display_severity}`}>
      <header className="v3a-card-head">
        <span className={sev.cls}>{EVASION_LEVEL_LABEL[item.evasion_level]}</span>
        <span className="v3a-kind-pill">Q{item.exchange_index}</span>
        <span className="v3a-meta-pill">
          {item.analyst_name}
          {item.analyst_firm ? ` (${item.analyst_firm})` : ""} → {item.executive_name}
        </span>
        {item.behavior_subtype !== "none" ? (
          <span className="v3a-meta-pill">{EVASION_SUBTYPE_LABEL[item.behavior_subtype]}</span>
        ) : null}
        {item.followup_count > 0 ? (
          <span className="v3a-meta-pill v3a-warn-pill">追问 {item.followup_count} 次</span>
        ) : null}
        {item.legitimate_confidentiality ? (
          <span className="v3a-meta-pill">合规保密豁免</span>
        ) : null}
      </header>

      <p className="v3a-body-text">{item.rationale}</p>

      {item.sub_questions.length > 0 ? (
        <div className="v3a-subq-list">
          {item.sub_questions.map((sq, idx) => {
            const missed = item.unanswered_sub_questions.includes(sq);
            return (
              <span key={`${item.finding_id}-sq-${idx}`} className={missed ? "v3a-subq-chip is-missed" : "v3a-subq-chip"}>
                {missed ? "未答：" : "已及："}
                {sq}
              </span>
            );
          })}
        </div>
      ) : null}

      <div className="v3a-split-grid">
        <QuoteButton quote={item.question_quote} label="分析师提问" onInspect={onInspectQuote} />
        <QuoteButton quote={item.answer_quote} label="高管回答" onInspect={onInspectQuote} />
      </div>
    </article>
  );
}

function CommitmentRow({
  item,
  mutingId,
  onInspectQuote,
  onToggleMute,
}: {
  item: VerbalCommitmentRecord;
  mutingId: string | null;
  onInspectQuote: (quote: GroundedQuoteRef) => void;
  onToggleMute: (commitmentId: string, muted: boolean) => Promise<void>;
}) {
  const st = COMMITMENT_STATUS_META[item.status] ?? COMMITMENT_STATUS_META.pending_10q;
  const isBusy = mutingId === item.commitment_id;
  return (
    <article className={`v3a-commitment-card${item.muted ? " is-muted" : ""}`}>
      <header className="v3a-card-head">
        <span className={st.cls}>{st.label}</span>
        <strong className="v3a-metric-key">{item.kpi_key || item.metric_canonical}</strong>
        {item.target_period_key ? <span className="v3a-meta-pill">目标期 {item.target_period_key}</span> : null}
        {item.source_fiscal_period ? <span className="v3a-meta-pill">承诺于 {item.source_fiscal_period}</span> : null}
        <span className="v3a-meta-pill">{item.effective_basis}</span>
        <span className="grow" />
        <button
          type="button"
          className="btn plain peer-coord-btn"
          disabled={isBusy}
          onClick={() => void onToggleMute(item.commitment_id, !item.muted)}
        >
          {isBusy ? "更新中…" : item.muted ? "恢复跟踪" : "忽略"}
        </button>
      </header>
      <p className="v3a-body-text">{item.commitment_text}</p>
      {item.actual_value || item.delta_note ? (
        <div className="v3a-formula-note">
          {item.actual_value ? `实际值：${item.actual_value}` : ""}
          {item.actual_value && item.delta_note ? " · " : ""}
          {item.delta_note ?? ""}
        </div>
      ) : null}
      {item.range_drift_note ? (
        <div className="v3a-restatement-note">区间漂移：{item.range_drift_note}</div>
      ) : null}
      {item.quote ? <QuoteButton quote={item.quote} onInspect={onInspectQuote} /> : null}
    </article>
  );
}

/**
 * V3a L2 3-Tab High-Density Audit Panel.
 * Strictly zero inline style attributes to satisfy Vite CSP `style-src 'self'`.
 */
export function CallVerificationPanel({
  report,
  markdown,
  busy = false,
  onInspectQuote,
  onToggleMuteCommitment,
  onOverrideAndRerun,
  onMergeEntityAlias,
  onCopyMarkdown,
}: CallVerificationPanelProps) {
  const [activeTab, setActiveTab] = useState<CallAuditTab>("collisions");
  const [showCollapsedEvasions, setShowCollapsedEvasions] = useState(false);
  const [showCorrection, setShowCorrection] = useState(false);
  const [overrideSymbol, setOverrideSymbol] = useState(report.symbol ?? "");
  const [overrideFy, setOverrideFy] = useState("");
  const [overrideFq, setOverrideFq] = useState("");
  const [mergeTargetKey, setMergeTargetKey] = useState("");
  const [merging, setMerging] = useState(false);
  const [mutingId, setMutingId] = useState<string | null>(null);

  const visibleEvasions = useMemo(
    () => report.qa_evasions.filter((ev) => !ev.collapsed_by_default),
    [report.qa_evasions],
  );
  const collapsedEvasions = useMemo(
    () => report.qa_evasions.filter((ev) => ev.collapsed_by_default),
    [report.qa_evasions],
  );

  const distSegments = useMemo(() => {
    const direct = report.evasion_distribution.direct ?? 0;
    const intermediate = report.evasion_distribution.intermediate ?? 0;
    const evasive = report.evasion_distribution.fully_evasive ?? 0;
    const total = Math.max(1, direct + intermediate + evasive);
    const wDirect = (direct / total) * 100;
    const wInter = (intermediate / total) * 100;
    const wEv = Math.max(0, 100 - wDirect - wInter);
    return {
      direct,
      intermediate,
      evasive,
      total: direct + intermediate + evasive,
      wDirect,
      wInter,
      wEv,
    };
  }, [report.evasion_distribution]);

  const groupedCommitments = useMemo(() => {
    const flagged: VerbalCommitmentRecord[] = [];
    const pending: VerbalCommitmentRecord[] = [];
    const closed: VerbalCommitmentRecord[] = [];
    for (const c of report.commitments) {
      if (c.muted || c.status === "met" || c.status === "likely_concluded_or_unasked") {
        closed.push(c);
      } else if (c.status === "missed" || c.status === "silent_drop_high_risk" || c.status === "manual_nongaap_review") {
        flagged.push(c);
      } else {
        pending.push(c);
      }
    }
    return { flagged, pending, closed };
  }, [report.commitments]);

  const handleMute = async (commitmentId: string, muted: boolean) => {
    setMutingId(commitmentId);
    try {
      await onToggleMuteCommitment(commitmentId, muted);
    } finally {
      setMutingId(null);
    }
  };

  const handleApplyOverride = () => {
    const fyNum = overrideFy.trim() ? Number(overrideFy.trim()) : undefined;
    const fqNum = overrideFq.trim() ? Number(overrideFq.trim()) : undefined;
    onOverrideAndRerun({
      ...(overrideSymbol.trim() ? { symbol: overrideSymbol.trim().toUpperCase() } : {}),
      ...(fyNum && Number.isInteger(fyNum) ? { fiscal_year: fyNum } : {}),
      ...(fqNum && fqNum >= 1 && fqNum <= 4 ? { fiscal_quarter: fqNum } : {}),
    });
    setShowCorrection(false);
  };

  const handleApplyMerge = async () => {
    const target = mergeTargetKey.trim();
    if (!target || target === report.entity_key) return;
    setMerging(true);
    try {
      await onMergeEntityAlias(target, report.company_name ?? report.symbol ?? undefined);
      setMergeTargetKey("");
      setShowCorrection(false);
    } finally {
      setMerging(false);
    }
  };

  return (
    <div className="v3a-panel" data-testid="call-verification-panel">
      <div className="v3a-panel-top">
        <div className="v3a-status-chips">
          <span className="v3a-meta-pill">{LEDGER_STATUS_LABEL[report.ledger_status]}</span>
          <span className="v3a-meta-pill">{BASIS_LABEL[report.call_default_basis]}</span>
          {report.fiscal_period_key ? <span className="v3a-meta-pill">{report.fiscal_period_key}</span> : null}
          {report.call_date ? <span className="v3a-meta-pill">会议日 {report.call_date}</span> : null}
        </div>
        <div className="v3a-panel-tools">
          <button
            type="button"
            className="btn plain peer-coord-btn"
            onClick={() => setShowCorrection((v) => !v)}
          >
            {showCorrection ? "收起校准" : "实体 / 财季校准"}
          </button>
          <button
            type="button"
            className="btn peer-coord-btn"
            disabled={!markdown}
            onClick={onCopyMarkdown}
          >
            复制 Markdown 审计简报
          </button>
        </div>
      </div>

      {showCorrection ? (
        <div className="v3a-correction-box">
          <div className="v3a-correction-row">
            <label className="v3a-field">
              <span>代码 (Ticker)</span>
              <input
                type="text"
                className="inbox-input v3a-input-sm"
                value={overrideSymbol}
                onChange={(e) => setOverrideSymbol(e.target.value)}
                placeholder="如 NVDA"
              />
            </label>
            <label className="v3a-field">
              <span>财年 (FY)</span>
              <input
                type="number"
                className="inbox-input v3a-input-xs"
                value={overrideFy}
                onChange={(e) => setOverrideFy(e.target.value)}
                placeholder="2026"
              />
            </label>
            <label className="v3a-field">
              <span>季度 (1-4)</span>
              <input
                type="number"
                min={1}
                max={4}
                className="inbox-input v3a-input-xs"
                value={overrideFq}
                onChange={(e) => setOverrideFq(e.target.value)}
                placeholder="3"
              />
            </label>
            <button type="button" className="btn primary peer-coord-btn" disabled={busy} onClick={handleApplyOverride}>
              应用并重跑核验
            </button>
          </div>
          {report.entity_key.startsWith("nonsec:") || report.ledger_status === "non_sec_entity" ? (
            <div className="v3a-correction-row">
              <label className="v3a-field v3a-field-grow">
                <span>合并非 SEC 实体别名（当前 {report.entity_key} → 目标实体键）</span>
                <input
                  type="text"
                  className="inbox-input"
                  value={mergeTargetKey}
                  onChange={(e) => setMergeTargetKey(e.target.value)}
                  placeholder="例如 nonsec:hk:00700 或 10位CIK"
                />
              </label>
              <button
                type="button"
                className="btn peer-coord-btn"
                disabled={merging || !mergeTargetKey.trim()}
                onClick={() => void handleApplyMerge()}
              >
                {merging ? "合并中…" : "合并跨季台账"}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="v3a-tabs" role="tablist" aria-label="电话会核验视图">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "collisions"}
          className={activeTab === "collisions" ? "v3a-tab is-active" : "v3a-tab"}
          onClick={() => setActiveTab("collisions")}
        >
          Tab 1 · Talk vs. Walk（{report.collision_findings.length}）
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "evasion"}
          className={activeTab === "evasion" ? "v3a-tab is-active" : "v3a-tab"}
          onClick={() => setActiveTab("evasion")}
        >
          Tab 2 · Q&amp;A 回避雷达（{report.evasion_red_yellow_count}/{report.evasion_total_exchanges}）
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "commitments"}
          className={activeTab === "commitments" ? "v3a-tab is-active" : "v3a-tab"}
          onClick={() => setActiveTab("commitments")}
        >
          Tab 3 · 跨季承诺与预期差（{report.commitments.length}）
        </button>
      </div>

      {activeTab === "collisions" ? (
        <div className="v3a-tab-body" role="tabpanel">
          {report.collision_findings.length === 0 ? (
            <p className="v3a-empty">本轮未发现显著账本冲突或未核验主张。</p>
          ) : (
            <div className="v3a-card-stack">
              {report.collision_findings.map((finding) => (
                <CollisionCard key={finding.finding_id} finding={finding} onInspectQuote={onInspectQuote} />
              ))}
            </div>
          )}
        </div>
      ) : null}

      {activeTab === "evasion" ? (
        <div className="v3a-tab-body" role="tabpanel">
          <div className="v3a-dist-card">
            <div className="v3a-dist-head">
              <strong>响应分布（共 {distSegments.total} 轮 Q&amp;A）</strong>
              <span className="peer-coord-muted">
                直接回答 {distSegments.direct} · 部分回应 {distSegments.intermediate} · 明显回避 {distSegments.evasive}
                {report.confidentiality_whitelisted_count > 0
                  ? ` · 合规保密豁免 ${report.confidentiality_whitelisted_count}`
                  : ""}
              </span>
            </div>
            <svg
              viewBox="0 0 100 8"
              preserveAspectRatio="none"
              className="v3a-dist-svg"
              role="img"
              aria-label={`直接回答 ${distSegments.direct}，部分回应 ${distSegments.intermediate}，明显回避 ${distSegments.evasive}`}
            >
              <rect x="0" y="0" width={distSegments.wDirect} height="8" className="v3a-dist-direct" />
              <rect x={distSegments.wDirect} y="0" width={distSegments.wInter} height="8" className="v3a-dist-inter" />
              <rect
                x={distSegments.wDirect + distSegments.wInter}
                y="0"
                width={distSegments.wEv}
                height="8"
                className="v3a-dist-evasive"
              />
            </svg>
          </div>

          {report.speaker_baseline_shifts.length > 0 ? (
            <div className="v3a-shift-box">
              <div className="v3a-section-label">跨季高管个人基线漂移（≥2 季历史对照）</div>
              {report.speaker_baseline_shifts.map((shift, idx) => (
                <div key={`${shift.executive_name}-${idx}`} className="v3a-shift-row">
                  <span className={(SEVERITY_META[shift.severity] ?? SEVERITY_META.info).cls}>
                    {(SEVERITY_META[shift.severity] ?? SEVERITY_META.info).label}
                  </span>
                  <strong>{shift.executive_name}</strong>
                  <span>{shift.summary}</span>
                </div>
              ))}
            </div>
          ) : null}

          {visibleEvasions.length === 0 ? (
            <p className="v3a-empty">未检出明显回避或选择性遗漏子问题的 Q&amp;A 轮次。</p>
          ) : (
            <div className="v3a-card-stack">
              {visibleEvasions.map((ev) => (
                <EvasionCard key={ev.finding_id} item={ev} onInspectQuote={onInspectQuote} />
              ))}
            </div>
          )}

          {collapsedEvasions.length > 0 ? (
            <div className="v3a-collapsed-wrap">
              <button
                type="button"
                className="btn plain peer-coord-btn"
                onClick={() => setShowCollapsedEvasions((v) => !v)}
              >
                {showCollapsedEvasions
                  ? `收起直接回答与合规保密轮次（${collapsedEvasions.length} 轮）`
                  : `展开直接回答与合规保密轮次（${collapsedEvasions.length} 轮）`}
              </button>
              {showCollapsedEvasions ? (
                <div className="v3a-card-stack">
                  {collapsedEvasions.map((ev) => (
                    <EvasionCard key={ev.finding_id} item={ev} onInspectQuote={onInspectQuote} />
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {activeTab === "commitments" ? (
        <div className="v3a-tab-body" role="tabpanel">
          {groupedCommitments.flagged.length > 0 ? (
            <section className="v3a-group">
              <h4 className="v3a-section-label">违背 / Silent Drop / 待人工核对（{groupedCommitments.flagged.length}）</h4>
              <div className="v3a-card-stack">
                {groupedCommitments.flagged.map((c) => (
                  <CommitmentRow
                    key={c.commitment_id}
                    item={c}
                    mutingId={mutingId}
                    onInspectQuote={onInspectQuote}
                    onToggleMute={handleMute}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {groupedCommitments.pending.length > 0 ? (
            <section className="v3a-group">
              <h4 className="v3a-section-label">跟踪中的未来承诺（{groupedCommitments.pending.length}）</h4>
              <div className="v3a-card-stack">
                {groupedCommitments.pending.map((c) => (
                  <CommitmentRow
                    key={c.commitment_id}
                    item={c}
                    mutingId={mutingId}
                    onInspectQuote={onInspectQuote}
                    onToggleMute={handleMute}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {groupedCommitments.closed.length > 0 ? (
            <section className="v3a-group">
              <h4 className="v3a-section-label">已兑现 / 已归档 / 已忽略（{groupedCommitments.closed.length}）</h4>
              <div className="v3a-card-stack">
                {groupedCommitments.closed.map((c) => (
                  <CommitmentRow
                    key={c.commitment_id}
                    item={c}
                    mutingId={mutingId}
                    onInspectQuote={onInspectQuote}
                    onToggleMute={handleMute}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {report.commitments.length === 0 ? (
            <p className="v3a-empty">本账号下该公司暂无可追踪的量化或里程碑口头承诺。</p>
          ) : null}

          {report.expectation_benchmarks.length > 0 ? (
            <section className="v3a-group">
              <h4 className="v3a-section-label">四层隔离预期基准（Expectation Benchmarks）</h4>
              <div className="v3a-card-stack">
                {report.expectation_benchmarks.map((eb, idx) => {
                  const meta = EXPECTATION_TIER_META[eb.tier] ?? EXPECTATION_TIER_META.sellside_third_party;
                  return (
                    <article key={`${eb.tier}-${idx}`} className="v3a-expectation-card">
                      <header className="v3a-card-head">
                        <span className={meta.badgeCls}>{meta.label}</span>
                        {eb.sellside_firm ? <span className="v3a-meta-pill">{eb.sellside_firm}</span> : null}
                        {eb.sellside_timing === "pre_earnings_preview" ? (
                          <span className="v3a-meta-pill">绩前前瞻</span>
                        ) : eb.sellside_timing === "post_earnings_review" ? (
                          <span className="v3a-meta-pill">绩后点评</span>
                        ) : null}
                        <h4 className="v3a-card-title">{eb.title}</h4>
                      </header>
                      <p className="v3a-body-text">{eb.summary}</p>
                      {eb.quote ? <QuoteButton quote={eb.quote} onInspect={onInspectQuote} /> : null}
                    </article>
                  );
                })}
              </div>
            </section>
          ) : null}

          {report.skeptic_calibration.length > 0 ? (
            <div className="skeptic-callout">
              <strong>Skeptic 反方与语境校准：</strong>
              <ul className="v3a-bullet-list">
                {report.skeptic_calibration.map((note, idx) => (
                  <li key={`sk-${idx}`}>{note}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {report.open_gaps.length > 0 ? (
            <div className="v3a-gaps-box">
              <strong>数据缺口与未覆写项：</strong>
              <ul className="v3a-bullet-list">
                {report.open_gaps.map((gap, idx) => (
                  <li key={`gap-${idx}`}>{gap}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}

      <footer className="v3a-footer">
        <span className="peer-coord-muted">
          原文引文核验 {report.verified_quote_count}/{report.total_quote_count} · 规则 {report.evaluation_rules_version} (
          {report.rules_sha256.slice(0, 8)})
          {report.model_id ? ` · ${report.model_id}` : ""}
          {report.total_tokens > 0 ? ` · ${report.total_tokens} tok` : ""}
        </span>
        <span className="v3a-disclaimer">{report.text_only_evasion_disclaimer}</span>
      </footer>
    </div>
  );
}
