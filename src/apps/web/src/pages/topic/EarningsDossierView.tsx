import type { ResearchV2 } from "@foresightnine/contracts";
import { useEffect, useId, useMemo, useState } from "react";
import {
  downloadEarningsExport,
  type EarningsExportFormat,
} from "../../api/earningsSquad";
import { formatInteger, formatNumberString } from "../../lib/format";
import { MarkdownContent } from "../../ui/MarkdownContent";
import { Disclosure } from "../../ui/controls";
import { Icon } from "../../ui/icons";

export type DossierReportMode = "memo" | "deep_report";

export interface EarningsDossierViewProps {
  dossier: ResearchV2.EarningsDossier;
  topicId?: string | undefined;
  reportMode?: DossierReportMode | undefined;
  onReportModeChange?: ((mode: DossierReportMode) => void) | undefined;
  onRequestDeepReport?: (() => Promise<unknown> | unknown) | undefined;
  deepReportLoading?: boolean | undefined;
  deepReportError?: string | null | undefined;
  onInspectSource?: ((sourceId: string, location?: string | null) => void) | undefined;
  onExport?: ((format: EarningsExportFormat, reportMode: DossierReportMode) => Promise<void> | void) | undefined;
  slotRoutingSummary?: Readonly<Record<string, unknown>> | null | undefined;
  className?: string | undefined;
}

const SECTOR_LABEL: Readonly<Record<ResearchV2.SectorApplicability["sector_category"], string>> = {
  non_financial: "非金融企业",
  bank: "银行 / 信贷机构",
  insurance: "保险机构",
  reit: "REITs 房地产信托",
};

const L2_PACK_LABEL: Readonly<Record<string, string>> = {
  kpi_saas_cloud: "软件 / 云 (SaaS & Cloud)",
  kpi_semis_hardware: "半导体 / 硬件 (Semis & Hardware)",
  kpi_internet_ads: "互联网 / 数字广告 (Internet & Ads)",
  kpi_consumer_retail: "消费 / 零售 (Consumer & Retail)",
  kpi_pharma_biotech: "医药 / 生物科技 (Pharma & Biotech)",
};

const MARGIN_LINE_LABEL: Readonly<Record<ResearchV2.MarginBridgeItem["line"], string>> = {
  gross_margin: "毛利率 (Gross Margin)",
  rd_pct: "研发费用率 (R&D %)",
  sga_pct: "销售管理费用率 (SG&A %)",
  operating_margin: "营业利润率 (Operating Margin)",
  net_margin: "净利率 (Net Margin)",
};

const FLAG_GROUP_LABEL: Readonly<Record<ResearchV2.DiagnosticFlag["group"], string>> = {
  margin_bridge: "利润率变动桥",
  earnings_quality: "盈利含金量与 FCF",
  working_capital: "营运资本排雷",
  capital_allocation: "资本配置与稀释",
  balance_sheet: "偿债与杠杆",
};

const ANCHOR_KIND_LABEL: Readonly<Record<ResearchV2.BearCaseItem["anchor_kind"], string>> = {
  flag: "盈利质量旗标 (flag)",
  redline: "披露红线变动 (redline)",
  guidance_gap: "指引与预期差 (guidance_gap)",
  valuation_gap: "估值隐含增速差 (valuation_gap)",
};

const SLOT_LABEL: Readonly<Record<ResearchV2.SlotUsageRecord["slot"], string>> = {
  fast_extractor: "取证提取 (fast_extractor)",
  deep_reasoner: "Lead 规划复核 (deep_reasoner)",
  bear_reviewer: "独立空头审查 (bear_reviewer)",
  writer: "底稿撰稿 (writer)",
};

const REDLINE_SECTION_LABEL: Readonly<Record<string, string>> = {
  item_1a: "Item 1A · 风险因素 (Risk Factors)",
  item_2: "Item 2 · 管理层讨论与分析 (MD&A)",
  item_7: "Item 7 · 管理层讨论与分析 (MD&A)",
  liquidity: "流动性与资本资源 (Liquidity & Capital Resources)",
  critical_accounting: "关键会计政策与估计 (Critical Accounting Estimates)",
};

const STATEMENT_FIELDS: ReadonlyArray<{
  key: keyof ResearchV2.PeriodFinancials;
  label: string;
}> = [
  { key: "revenue", label: "营业收入 (Revenue)" },
  { key: "cost_of_revenue", label: "营业成本 (Cost of Revenue)" },
  { key: "gross_profit", label: "毛利 (Gross Profit)" },
  { key: "rd_expense", label: "研发费用 (R&D)" },
  { key: "sga_expense", label: "销管费用 (SG&A)" },
  { key: "operating_income", label: "营业利润 (Operating Income)" },
  { key: "ebit", label: "EBIT" },
  { key: "ebitda", label: "EBITDA" },
  { key: "interest_expense", label: "利息费用 (Interest Expense)" },
  { key: "net_income", label: "净利润 (Net Income)" },
  { key: "sbc", label: "股权激励 (SBC)" },
  { key: "operating_cash_flow", label: "经营现金流 (OCF)" },
  { key: "capex", label: "资本开支 (CapEx)" },
  { key: "free_cash_flow", label: "自由现金流 (FCF)" },
  { key: "share_repurchases", label: "股票回购 (Buybacks)" },
  { key: "dividends_paid", label: "现金分红 (Dividends)" },
  { key: "cash", label: "现金及等价物 (Cash)" },
  { key: "short_term_investments", label: "短期投资 (ST Investments)" },
  { key: "accounts_receivable", label: "应收账款 (AR)" },
  { key: "inventory", label: "存货 (Inventory)" },
  { key: "accounts_payable", label: "应付账款 (AP)" },
  { key: "total_debt", label: "总债务 (Total Debt)" },
  { key: "shares_diluted", label: "稀释股本 (Diluted Shares)" },
];

function isFiniteSignedNumericString(val: string): boolean {
  return /^-?\d+(?:\.\d+)?$/.test(val.trim());
}

function formatQuantityDisplay(
  rawValue: string | number | null | undefined,
  unit?: string | null,
  currency?: string | null,
): string {
  if (rawValue === null || rawValue === undefined || rawValue === "") return "—";
  const str = String(rawValue).trim();
  const formatted = isFiniteSignedNumericString(str) ? formatNumberString(str) : str;
  const u = (unit ?? "").trim();
  if (u === "percent" || u === "%") return `${formatted}%`;
  if (u === "multiple" || u === "ratio") return `${formatted}x`;
  if (u === "bps") {
    const prefix = isFiniteSignedNumericString(str) && !str.startsWith("-") && Number(str) > 0 ? "+" : "";
    return `${prefix}${formatted} bps`;
  }
  if (u === "pp") {
    const prefix = isFiniteSignedNumericString(str) && !str.startsWith("-") && Number(str) > 0 ? "+" : "";
    return `${prefix}${formatted} pp`;
  }
  if (u === "days") return `${formatted} 天`;
  if (u) {
    if (currency && !u.toUpperCase().includes(currency.toUpperCase())) {
      return `${formatted} ${currency} (${u})`;
    }
    return `${formatted} ${u}`;
  }
  return currency ? `${formatted} ${currency}` : formatted;
}

function formatDiagnosticMetricValue(metric: ResearchV2.DiagnosticMetric | null | undefined): {
  text: string;
  sub: string | null;
  status: ResearchV2.DiagnosticMetric["status"] | "missing";
} {
  if (!metric) return { text: "—", sub: "未提供指标", status: "missing" };
  if (metric.status === "disabled_by_sector") {
    return { text: "行业门控禁用", sub: metric.reason ?? null, status: "disabled_by_sector" };
  }
  if (metric.status === "unavailable" || metric.value === null || metric.value === undefined) {
    return { text: "暂不可用", sub: metric.reason ?? null, status: "unavailable" };
  }
  return {
    text: formatQuantityDisplay(metric.value, metric.unit, metric.currency ?? null),
    sub: metric.formula || null,
    status: "computed",
  };
}

function guidanceDirectionTone(dir: ResearchV2.NewGuidanceDirectionResult["direction"]): string {
  switch (dir) {
    case "Raise":
      return "support";
    case "Cut":
      return "error";
    case "Maintain":
      return "counter";
    default:
      return "neutral";
  }
}

function guidanceDirectionLabel(dir: ResearchV2.NewGuidanceDirectionResult["direction"]): string {
  switch (dir) {
    case "Raise":
      return "Raise (上调)";
    case "Maintain":
      return "Maintain (维持)";
    case "Cut":
      return "Cut (下调)";
    default:
      return "Not Provided (未提供)";
  }
}

function surpriseTone(outcome: ResearchV2.SurpriseComparison["outcome"]): string {
  switch (outcome) {
    case "Beat":
      return "support";
    case "Miss":
      return "error";
    case "Inline":
      return "counter";
    default:
      return "neutral";
  }
}

function surpriseLabel(outcome: ResearchV2.SurpriseComparison["outcome"]): string {
  switch (outcome) {
    case "Beat":
      return "Beat";
    case "Miss":
      return "Miss";
    case "Inline":
      return "Inline";
    default:
      return "Unavailable";
  }
}

function severityTone(sev: "red" | "yellow" | "green" | "high" | "medium" | "low"): string {
  if (sev === "red" || sev === "high") return "error";
  if (sev === "yellow" || sev === "medium") return "review";
  return "support";
}

function flagSeverityLabel(sev: "red" | "yellow" | "green"): string {
  if (sev === "red") return "红灯";
  if (sev === "yellow") return "黄灯";
  return "绿灯";
}

function bearSeverityLabel(sev: "high" | "medium" | "low"): string {
  if (sev === "high") return "高风险";
  if (sev === "medium") return "中风险";
  return "低风险";
}

function extractDeepReportFromResponse(res: unknown): string | null {
  if (typeof res === "string" && res.trim()) return res;
  if (typeof res !== "object" || res === null) return null;
  const record = res as Record<string, unknown>;
  if (typeof record["deep_report_markdown"] === "string" && record["deep_report_markdown"].trim()) {
    return record["deep_report_markdown"];
  }
  const nestedDossier = record["dossier"];
  if (typeof nestedDossier === "object" && nestedDossier !== null) {
    const md = (nestedDossier as Record<string, unknown>)["deep_report_markdown"];
    if (typeof md === "string" && md.trim()) return md;
  }
  return null;
}

function formatCitationLocation(cit: ResearchV2.EvidenceCitation): string | null {
  if (cit.location && cit.location.trim()) return cit.location.trim();
  if (cit.character_start !== null && cit.character_start !== undefined && cit.character_end !== null && cit.character_end !== undefined) {
    return `${cit.character_start}-${cit.character_end}`;
  }
  return null;
}

function formatMetricName(name: string): string {
  const map: Readonly<Record<string, string>> = {
    revenue: "营业收入 (Revenue)",
    eps: "每股收益 (EPS)",
    gaap_eps: "GAAP EPS",
    non_gaap_eps: "Non-GAAP EPS",
    diluted_eps: "稀释 EPS",
    operating_income: "营业利润 (Operating Income)",
    net_income: "净利润 (Net Income)",
    gross_margin: "毛利率 (Gross Margin)",
    operating_margin: "营业利润率 (Operating Margin)",
    free_cash_flow: "自由现金流 (FCF)",
  };
  return map[name.toLowerCase()] ?? name;
}

function collectNonExportableSources(dossier: ResearchV2.EarningsDossier): string[] {
  const set = new Set<string>(dossier.gate_report.non_exportable_sources ?? []);
  const snap = dossier.snapshot;
  for (const ref of snap.prefetched_sources ?? []) {
    if (ref.permissions?.["export"] === false) {
      set.add(ref.source_id);
    }
  }
  if (snap.consensus_summary?.permissions?.["export"] === false) {
    set.add(snap.consensus_summary.source_id || "fmp_consensus");
  }
  for (const rec of snap.consensus_summary?.records ?? []) {
    if (rec.permissions?.export === false) {
      set.add(snap.consensus_summary?.source_id || `${rec.source_provider}_consensus`);
    }
  }
  if (snap.transcript_index?.permissions?.["export"] === false) {
    set.add(snap.transcript_index.source_id || "fmp_transcript");
  }
  return [...set].sort();
}

export function EarningsDossierView({
  dossier,
  topicId,
  reportMode,
  onReportModeChange,
  onRequestDeepReport,
  deepReportLoading = false,
  deepReportError = null,
  onInspectSource,
  onExport,
  slotRoutingSummary,
  className,
}: EarningsDossierViewProps) {
  const [internalMode, setInternalMode] = useState<DossierReportMode>(reportMode ?? "memo");
  const [localDeepReport, setLocalDeepReport] = useState<string | null>(dossier.deep_report_markdown ?? null);
  const [internalDeepLoading, setInternalDeepLoading] = useState(false);
  const [internalDeepError, setInternalDeepError] = useState<string | null>(null);
  const [matrixOpen, setMatrixOpen] = useState(false);
  const [exportBusy, setExportBusy] = useState<EarningsExportFormat | null>(null);
  const [exportFeedback, setExportFeedback] = useState<{ tone: "info" | "error"; text: string } | null>(null);
  const [redactionTipOpen, setRedactionTipOpen] = useState(false);

  const tooltipId = useId();
  const activeMode = reportMode ?? internalMode;

  useEffect(() => {
    setLocalDeepReport(dossier.deep_report_markdown ?? null);
    setInternalDeepError(null);
  }, [dossier.dossier_id, dossier.dossier_sha256, dossier.deep_report_markdown]);

  const effectiveDeepReport = dossier.deep_report_markdown ?? localDeepReport;
  const isDeepLoading = deepReportLoading || internalDeepLoading;
  const effectiveDeepError = deepReportError ?? internalDeepError;

  const snap = dossier.snapshot;
  const sector = dossier.sector_applicability;
  const statements = snap.statements ?? [];
  const latestStmt = statements[0] ?? null;
  const diagnostics = snap.diagnostics ?? null;
  const flags = (diagnostics?.flags && diagnostics.flags.length > 0 ? diagnostics.flags : snap.flags) ?? [];
  const guidanceEvals = snap.guidance_nongaap?.guidance_evaluations ?? [];
  const nongaapBridges = snap.guidance_nongaap?.nongaap_verifications ?? [];
  const nongaapWarnings = snap.guidance_nongaap?.warnings ?? [];
  const valuation = snap.valuation ?? null;
  const reverseDcf = valuation?.reverse_dcf ?? null;
  const redlineSummary = snap.redline_summary ?? null;
  const evidenceCards = dossier.evidence_cards ?? [];
  const bearCases = dossier.bear_cases ?? [];
  const rejectedBearCases = dossier.rejected_bear_cases ?? [];
  const openGaps = dossier.open_gaps ?? [];
  const prefetchedSources = snap.prefetched_sources ?? [];

  const nonExportableSources = useMemo(() => collectNonExportableSources(dossier), [dossier]);
  const isExportRestricted = dossier.gate_report.exportable === false || nonExportableSources.length > 0;
  const redactionTooltipText = isExportRestricted
    ? `包含不可导出数据源（export=False）：${nonExportableSources.join("、") || "受限来源"}。导出 Markdown / CSV / Excel 时将自动脱敏剔除受限一致预期或电话会纪要，仅保留 SEC 官方披露与已授权内容。`
    : "所有关联数据源均允许导出（export=True）。";

  const cardsById = useMemo(() => {
    const map = new Map<string, ResearchV2.EvidenceCard>();
    for (const c of evidenceCards) map.set(c.card_id, c);
    return map;
  }, [evidenceCards]);

  const sourceLabelById = useMemo(() => {
    const map = new Map<string, string>();
    for (const s of prefetchedSources) {
      const title = (s.title || "").trim();
      if (title) {
        map.set(s.source_id, title);
      }
    }
    return map;
  }, [prefetchedSources]);

  const flagLabelById = useMemo(() => {
    const map = new Map<string, string>();
    for (const f of snap.flags ?? []) {
      const label = (f.metric_name || f.flag_id || "").trim();
      if (f.flag_id && label) map.set(f.flag_id, label);
    }
    return map;
  }, [snap.flags]);

  const formatSourceRefLabel = (sourceId: string, loc?: string | null): string => {
    const base = sourceLabelById.get(sourceId) ?? sourceId;
    return loc ? `${base} · ${loc}` : base;
  };

  const formatAnchorLabel = (anchorId: string): string => {
    const card = cardsById.get(anchorId);
    if (card) return card.topic_tag || anchorId;
    const flagLbl = flagLabelById.get(anchorId);
    if (flagLbl) return flagLbl;
    const srcLbl = sourceLabelById.get(anchorId);
    if (srcLbl) return srcLbl;
    return anchorId;
  };

  const defaultSecFilingSourceId = useMemo(() => {
    const filingSrc = prefetchedSources.find(
      (s) =>
        s.kind === "official"
        && /10-[kq]|filing|section|redline/i.test(`${s.source_id} ${s.title} ${s.retrieval_method}`),
    );
    return filingSrc?.source_id ?? prefetchedSources.find((s) => s.kind === "official")?.source_id ?? null;
  }, [prefetchedSources]);

  const defaultEx99SourceId = useMemo(() => {
    const exSrc = prefetchedSources.find((s) => /8k|99\.1|ex99|release/i.test(`${s.source_id} ${s.title} ${s.retrieval_method}`));
    return exSrc?.source_id ?? defaultSecFilingSourceId;
  }, [prefetchedSources, defaultSecFilingSourceId]);

  const handleModeSwitch = async (nextMode: DossierReportMode) => {
    if (reportMode === undefined) setInternalMode(nextMode);
    onReportModeChange?.(nextMode);
    if (nextMode === "deep_report" && !effectiveDeepReport && onRequestDeepReport && !isDeepLoading) {
      setInternalDeepLoading(true);
      setInternalDeepError(null);
      try {
        const result = await onRequestDeepReport();
        const extracted = extractDeepReportFromResponse(result);
        if (extracted) {
          setLocalDeepReport(extracted);
        }
      } catch (err) {
        setInternalDeepError(err instanceof Error ? err.message : "生成完整深研报告失败，请稍后重试。");
      } finally {
        setInternalDeepLoading(false);
      }
    }
  };

  const handleRetryDeepReport = () => {
    if (!onRequestDeepReport || isDeepLoading) return;
    setInternalDeepLoading(true);
    setInternalDeepError(null);
    Promise.resolve(onRequestDeepReport())
      .then((result) => {
        const extracted = extractDeepReportFromResponse(result);
        if (extracted) setLocalDeepReport(extracted);
      })
      .catch((err: unknown) => {
        setInternalDeepError(err instanceof Error ? err.message : "生成完整深研报告失败，请稍后重试。");
      })
      .finally(() => setInternalDeepLoading(false));
  };

  const handleExport = async (format: EarningsExportFormat) => {
    if (exportBusy) return;
    setExportBusy(format);
    setExportFeedback(null);
    const effectiveExportReportMode: DossierReportMode =
      activeMode === "deep_report" && effectiveDeepReport ? "deep_report" : "memo";
    try {
      if (onExport) {
        await onExport(format, effectiveExportReportMode);
        setExportFeedback({
          tone: "info",
          text: isExportRestricted
            ? `已请求导出 ${format.toUpperCase()}（已按来源权限自动脱敏 ${nonExportableSources.join("、")}）。`
            : `已导出 ${format.toUpperCase()} 底稿。`,
        });
      } else if (topicId) {
        const res = await downloadEarningsExport(topicId, format, {
          reportMode: effectiveExportReportMode,
        });
        setExportFeedback({
          tone: "info",
          text: res.redacted
            ? `已下载 ${res.filename}（已脱敏受限来源）`
            : `已下载 ${res.filename}`,
        });
      }
    } catch (err) {
      setExportFeedback({
        tone: "error",
        text: err instanceof Error ? err.message : `导出 ${format.toUpperCase()} 失败。`,
      });
    } finally {
      setExportBusy(null);
    }
  };

  const resolveAnchorInspection = (anchorId: string): { sourceId: string; location: string | null } | null => {
    const card = cardsById.get(anchorId);
    if (card?.citations && card.citations.length > 0) {
      const firstCit = card.citations[0]!;
      return { sourceId: firstCit.source_id, location: formatCitationLocation(firstCit) };
    }
    const directSrc = prefetchedSources.find((s) => s.source_id === anchorId);
    if (directSrc) {
      return { sourceId: directSrc.source_id, location: null };
    }
    if (anchorId === "transcript:qa" || anchorId.startsWith("transcript:")) {
      return {
        sourceId: snap.transcript_index?.source_id || "fmp_transcript",
        location: anchorId,
      };
    }
    if (anchorId === "consensus:gap" || anchorId.startsWith("consensus:") || /pe_forward|ev_to_ntm/i.test(anchorId)) {
      return {
        sourceId: snap.consensus_summary?.source_id || "fmp_consensus",
        location: anchorId,
      };
    }
    if (anchorId.startsWith("8k:") && defaultEx99SourceId) {
      return { sourceId: defaultEx99SourceId, location: anchorId };
    }
    if (anchorId.startsWith("redline:") && defaultSecFilingSourceId) {
      return { sourceId: defaultSecFilingSourceId, location: anchorId };
    }
    for (const c of evidenceCards) {
      if ((c.supporting_fact_ids ?? []).includes(anchorId) && c.citations && c.citations.length > 0) {
        const cit = c.citations[0]!;
        return { sourceId: cit.source_id, location: formatCitationLocation(cit) };
      }
    }
    if (defaultSecFilingSourceId && (flags.some((f) => f.flag_id === anchorId) || anchorId === "reverse_dcf" || anchorId.startsWith("valuation:"))) {
      return { sourceId: defaultSecFilingSourceId, location: anchorId };
    }
    return null;
  };

  const handleInspectAnchor = (anchorId: string) => {
    const target =
      document.getElementById(`ed-flag-${anchorId}`)
      ?? document.getElementById(`ed-card-${anchorId}`)
      ?? document.getElementById(`ed-redline-${anchorId.replace(/^redline:/, "")}`);
    target?.scrollIntoView?.({ block: "center" });

    const resolved = resolveAnchorInspection(anchorId);
    if (resolved && onInspectSource) {
      onInspectSource(resolved.sourceId, resolved.location);
    }
  };

  // Headline metrics extraction
  const revEval = guidanceEvals.find((g) => /revenue/i.test(g.metric_name)) ?? null;
  const epsEval = guidanceEvals.find((g) => /eps/i.test(g.metric_name)) ?? null;
  const epsBridge = nongaapBridges.find((b) => /eps/i.test(b.metric_name)) ?? null;
  const headlineRevenueText = revEval?.vs_prior_guidance.actual_value != null
    ? formatQuantityDisplay(revEval.vs_prior_guidance.actual_value, revEval.vs_prior_guidance.unit, revEval.vs_prior_guidance.currency)
    : revEval?.vs_consensus.actual_value != null
      ? formatQuantityDisplay(revEval.vs_consensus.actual_value, revEval.vs_consensus.unit, revEval.vs_consensus.currency)
      : latestStmt?.revenue
        ? formatQuantityDisplay(latestStmt.revenue.value, latestStmt.revenue.unit ?? latestStmt.monetary_unit, latestStmt.revenue.currency ?? latestStmt.currency)
        : "—";

  const gaapEpsValue =
    epsBridge?.gaap_value
    ?? (snap.derived_metrics?.["gaap_eps"] as string | number | null | undefined)
    ?? snap.consensus_summary?.records?.[0]?.eps_actual
    ?? null;
  const nonGaapEpsValue =
    epsBridge?.reported_non_gaap_value
    ?? (snap.derived_metrics?.["non_gaap_eps"] as string | number | null | undefined)
    ?? (epsEval && /non[-_ ]?gaap/i.test(epsEval.metric_name) ? epsEval.vs_consensus.actual_value ?? epsEval.vs_prior_guidance.actual_value : null);
  const headlineEpsUnit = epsBridge?.unit ?? epsEval?.vs_consensus.unit ?? "USD/share";

  const flagCounts = useMemo(() => {
    const counts = { red: 0, yellow: 0, green: 0 };
    for (const fl of flags) {
      if (fl.severity === "red") counts.red += 1;
      else if (fl.severity === "yellow") counts.yellow += 1;
      else if (fl.severity === "green") counts.green += 1;
    }
    return counts;
  }, [flags]);

  const activeStatementFields = useMemo(
    () => STATEMENT_FIELDS.filter(({ key }) => statements.some((s) => s[key] !== null && s[key] !== undefined)),
    [statements],
  );

  const segmentCards = useMemo(
    () =>
      evidenceCards.filter(
        (c) =>
          c.worker_role === "worker_segments"
          || /segment|kpi|rpo|nrr|arr|arpu|dio|sssg|pipeline/i.test(c.topic_tag)
          || (c.numeric_claims && c.numeric_claims.length > 0),
      ),
    [evidenceCards],
  );

  const otherCards = useMemo(() => {
    const shown = new Set(segmentCards.map((c) => c.card_id));
    return evidenceCards.filter((c) => !shown.has(c.card_id));
  }, [evidenceCards, segmentCards]);

  const ex99Tables = useMemo(() => {
    const out: Array<{ label: string; sourceId: string | null; table: ResearchV2.ParsedHtmlTable }> = [];
    const rel = snap.earnings_release;
    if (!rel) return out;
    const pushExhibit = (periodLabel: string, exhibit: ResearchV2.EarningsReleaseExhibit | null | undefined) => {
      if (!exhibit?.tables) return;
      for (const tbl of exhibit.tables) {
        out.push({ label: `${periodLabel} · ${exhibit.exhibit_type}`, sourceId: defaultEx99SourceId, table: tbl });
      }
    };
    pushExhibit("本期 8-K", rel.current?.ex99_1);
    pushExhibit("本期 8-K", rel.current?.ex99_2);
    pushExhibit("上期 8-K", rel.prior?.ex99_1);
    return out;
  }, [snap.earnings_release, defaultEx99SourceId]);

  const redlineSections = useMemo(
    () => Object.entries(redlineSummary?.section_diffs ?? {}),
    [redlineSummary],
  );

  const valuationMetrics = useMemo(() => {
    if (!valuation) return [];
    return [
      { key: "market_cap", label: "市值 (Market Cap)", metric: valuation.market_cap },
      { key: "enterprise_value", label: "企业价值 (EV)", metric: valuation.enterprise_value },
      { key: "pe_ttm", label: "P/E (TTM)", metric: valuation.pe_ttm },
      { key: "pe_forward", label: "Forward P/E", metric: valuation.pe_forward },
      { key: "ev_to_ebitda", label: "EV / EBITDA", metric: valuation.ev_to_ebitda },
      { key: "ev_to_sales", label: "EV / Sales", metric: valuation.ev_to_sales },
      { key: "fcf_yield_pct", label: "FCF Yield", metric: valuation.fcf_yield_pct },
      { key: "ev_to_fcf_ex_sbc", label: "EV / (FCF ex-SBC)", metric: valuation.ev_to_fcf_ex_sbc },
      { key: "fcf_ex_sbc_yield_pct", label: "FCF ex-SBC Yield", metric: valuation.fcf_ex_sbc_yield_pct },
    ];
  }, [valuation]);

  const slotEntries = useMemo(() => {
    const order: ReadonlyArray<ResearchV2.SlotUsageRecord["slot"]> = [
      "deep_reasoner",
      "fast_extractor",
      "bear_reviewer",
      "writer",
    ];
    return order.map((slotKey) => {
      const rec = dossier.slot_usage_summary?.slots?.[slotKey];
      const routingItem =
        slotRoutingSummary && typeof slotRoutingSummary[slotKey] === "object" && slotRoutingSummary[slotKey] !== null
          ? (slotRoutingSummary[slotKey] as Record<string, unknown>)
          : null;
      return { slotKey, rec, routingItem };
    });
  }, [dossier.slot_usage_summary, slotRoutingSummary]);

  const companyHeader = dossier.company_name || dossier.symbol || dossier.entity_id;
  const activeNarrative = activeMode === "deep_report" ? effectiveDeepReport : dossier.memo_markdown;

  return (
    <section
      className={`ed-dossier${className ? ` ${className}` : ""}`}
      aria-label="买方财报研究底稿视图"
      data-testid="earnings-dossier-view"
    >
      {/* Top Bar: Company/Period Header, Sector Gate Badge, Dual-Mode Switcher, Export Controls */}
      <header className="chart-card ed-topbar">
        <div className="chart-head ed-topbar-head">
          <div className="chart-title-wrap ed-identity">
            <div className="chart-title-row">
              <h2 className="chart-title ed-company-title">{companyHeader}</h2>
              {dossier.symbol ? <span className="chart-badge is-updated mono">{dossier.symbol}</span> : null}
              {dossier.period_label ? <span className="chart-badge is-updated">{dossier.period_label}</span> : null}
              <span
                className={`role light ${sector.is_gated ? "review" : "support"}`}
                data-testid="sector-applicability-badge"
              >
                {sector.is_gated
                  ? `行业门控 · ${SECTOR_LABEL[sector.sector_category]}`
                  : `${SECTOR_LABEL[sector.sector_category]} · 全指标适用`}
              </span>
              {dossier.locked !== false ? (
                <span className="role light neutral">已锁定底稿</span>
              ) : null}
            </div>
            <p className="chart-subtitle">
              {[
                dossier.cik ? `CIK ${dossier.cik}` : null,
                sector.sic_code ? `SIC ${sector.sic_code}${sector.sic_description ? ` (${sector.sic_description})` : ""}` : null,
                dossier.selected_l2_pack_id ? `行业包: ${L2_PACK_LABEL[dossier.selected_l2_pack_id] ?? dossier.selected_l2_pack_id}` : "L1 通用美股财报口径",
                dossier.dossier_id ? `Dossier ${dossier.dossier_id}` : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          </div>

          {/* Dual-Mode Switcher */}
          <div className="chart-tools ed-mode-controls">
            <div className="seg inline" role="tablist" aria-label="研究交付档位切换">
              <button
                type="button"
                role="tab"
                aria-selected={activeMode === "memo"}
                onClick={() => void handleModeSwitch("memo")}
              >
                买方备忘录 (Memo)
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={activeMode === "deep_report"}
                onClick={() => void handleModeSwitch("deep_report")}
              >
                完整深研报告 (Deep Report)
              </button>
            </div>
          </div>
        </div>

        {/* Multi-format Export Controls + Redaction Notice */}
        <div className="actions-row ed-export-bar">
          <button
            type="button"
            className="btn"
            disabled={exportBusy !== null || (!onExport && !topicId)}
            title={isExportRestricted ? redactionTooltipText : "导出结构化 Markdown 投研底稿"}
            aria-describedby={isExportRestricted ? tooltipId : undefined}
            onClick={() => void handleExport("markdown")}
          >
            <Icon name="download" size={15} />
            {exportBusy === "markdown" ? "正在导出 Markdown…" : "导出 Markdown"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={exportBusy !== null || (!onExport && !topicId)}
            title={isExportRestricted ? redactionTooltipText : "导出结构化 CSV 数据表"}
            aria-describedby={isExportRestricted ? tooltipId : undefined}
            onClick={() => void handleExport("csv")}
          >
            <Icon name="table" size={15} />
            {exportBusy === "csv" ? "正在导出 CSV…" : "导出 CSV"}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={exportBusy !== null || (!onExport && !topicId)}
            title={isExportRestricted ? redactionTooltipText : "导出 6-Sheet Excel 工作簿 (.xlsx)"}
            aria-describedby={isExportRestricted ? tooltipId : undefined}
            onClick={() => void handleExport("xlsx")}
          >
            <Icon name="file" size={15} />
            {exportBusy === "xlsx" ? "正在生成 Excel…" : "导出 Excel (.xlsx)"}
          </button>

          {isExportRestricted ? (
            <button
              type="button"
              className="btn plain ed-redaction-tip-btn"
              aria-expanded={redactionTipOpen}
              aria-describedby={tooltipId}
              title={redactionTooltipText}
              onClick={() => setRedactionTipOpen((v) => !v)}
            >
              <Icon name="alert" size={15} />
              导出权限脱敏说明 ({nonExportableSources.length})
            </button>
          ) : (
            <span className="meta">导出权限：全来源允许导出 (export=True)</span>
          )}
        </div>

        {isExportRestricted ? (
          <div
            id={tooltipId}
            role="tooltip"
            className="callout-lite warn ed-redaction-tooltip"
            data-testid="export-redaction-tooltip"
          >
            <Icon name="alert" size={15} />
            <div>
              <span>{redactionTooltipText}</span>
              {redactionTipOpen && (dossier.gate_report.notes ?? []).length > 0 ? (
                <div className="meta">{(dossier.gate_report.notes ?? []).join("；")}</div>
              ) : null}
            </div>
          </div>
        ) : null}

        {exportFeedback ? (
          <p className={exportFeedback.tone === "error" ? "inline-error" : "meta"} role="status">
            {exportFeedback.text}
          </p>
        ) : null}

        {sector.is_gated ? (
          <div className="callout ed-sector-callout" role="note" data-testid="sector-gate-notice">
            <div className="h">
              <Icon name="alert" size={16} />
              <span>
                行业口径门控已启用：{SECTOR_LABEL[sector.sector_category]}
                {sector.sic_code ? ` (SIC ${sector.sic_code})` : ""}
              </span>
            </div>
            <p>{sector.explanation}</p>
            {(sector.disabled_metrics ?? []).length > 0 ? (
              <p className="why">
                已关闭不适用指标：{(sector.disabled_metrics ?? []).slice(0, 10).join("、")}
                {(sector.disabled_metrics ?? []).length > 10 ? ` 等 ${(sector.disabled_metrics ?? []).length} 项` : ""}
              </p>
            ) : null}
            {(sector.recommended_alternatives ?? []).length > 0 ? (
              <p className="why">
                建议核心观察指标：{(sector.recommended_alternatives ?? []).join(" · ")}
              </p>
            ) : null}
          </div>
        ) : null}
      </header>

      {/* Section 1: 业绩速览、预期差与估值条 */}
      <section className="chart-card ed-section" aria-label="业绩速览、预期差与估值条">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">1. 业绩速览、预期差与估值条</h3>
            <p className="chart-subtitle">GAAP / Non-GAAP 核心表现、vs 指引与一致预期、估值倍数及 Reverse-DCF 隐含增速</p>
          </div>
          {snap.consensus_summary?.status === "unavailable" ? (
            <span className="chart-badge is-sim">一致预期暂缺（已回退至上期 8-K 指引）</span>
          ) : null}
        </div>

        {/* Headline Revenue / EPS & Profit */}
        <div className="stats ed-headline-strip">
          <div className="stat">
            <b>{headlineRevenueText}</b>
            <span>营业收入 (GAAP Revenue)</span>
          </div>
          <div className="stat">
            <b>{gaapEpsValue !== null && gaapEpsValue !== undefined ? formatQuantityDisplay(gaapEpsValue, headlineEpsUnit) : "—"}</b>
            <span>GAAP EPS</span>
          </div>
          <div className="stat">
            <b>{nonGaapEpsValue !== null && nonGaapEpsValue !== undefined ? formatQuantityDisplay(nonGaapEpsValue, headlineEpsUnit) : "—"}</b>
            <span>Non-GAAP EPS</span>
          </div>
          {latestStmt?.operating_income ? (
            <div className="stat">
              <b>{formatQuantityDisplay(latestStmt.operating_income.value, latestStmt.operating_income.unit ?? latestStmt.monetary_unit, latestStmt.operating_income.currency ?? latestStmt.currency)}</b>
              <span>GAAP 营业利润</span>
            </div>
          ) : null}
          {latestStmt?.net_income ? (
            <div className="stat">
              <b>{formatQuantityDisplay(latestStmt.net_income.value, latestStmt.net_income.unit ?? latestStmt.monetary_unit, latestStmt.net_income.currency ?? latestStmt.currency)}</b>
              <span>GAAP 净利润</span>
            </div>
          ) : null}
          {latestStmt?.free_cash_flow ? (
            <div className="stat">
              <b>{formatQuantityDisplay(latestStmt.free_cash_flow.value, latestStmt.free_cash_flow.unit ?? latestStmt.monetary_unit, latestStmt.free_cash_flow.currency ?? latestStmt.currency)}</b>
              <span>自由现金流 (FCF)</span>
            </div>
          ) : null}
        </div>

        {/* Guidance Beat/Miss & Direction Table */}
        {guidanceEvals.length > 0 ? (
          <div className="ev-block">
            <div className="gtitle">预期差对比与新指引方向 (vs Prior Guidance & Consensus)</div>
            <div className="md-table-scroll">
              <table className="ev-table" aria-label="指引与一致预期对比表">
                <thead>
                  <tr>
                    <th>指标</th>
                    <th className="md-num">本期实际</th>
                    <th>vs 上期指引</th>
                    <th>vs 一致预期</th>
                    <th>新指引方向</th>
                  </tr>
                </thead>
                <tbody>
                  {guidanceEvals.map((ev) => {
                    const actualVal = ev.vs_prior_guidance.actual_value ?? ev.vs_consensus.actual_value;
                    const actualUnit = ev.vs_prior_guidance.unit || ev.vs_consensus.unit;
                    const actualCurr = ev.vs_prior_guidance.currency ?? ev.vs_consensus.currency;
                    const ng = ev.new_guidance_direction;
                    const ngRange =
                      ng.new_low !== null && ng.new_low !== undefined && ng.new_high !== null && ng.new_high !== undefined
                        ? `${formatNumberString(ng.new_low)}–${formatNumberString(ng.new_high)}`
                        : ng.new_mid !== null && ng.new_mid !== undefined
                          ? formatNumberString(ng.new_mid)
                          : null;
                    return (
                      <tr key={`${ev.metric_name}-${ev.period_label ?? ""}`}>
                        <th scope="row">
                          <span className="ev-title">{formatMetricName(ev.metric_name)}</span>
                          {ev.period_label ? <span className="meta">{ev.period_label}</span> : null}
                        </th>
                        <td className="md-num">
                          <b>{formatQuantityDisplay(actualVal, actualUnit, actualCurr)}</b>
                        </td>
                        <td>
                          <div className="chips">
                            <span className={`role light ${surpriseTone(ev.vs_prior_guidance.outcome)}`}>
                              {surpriseLabel(ev.vs_prior_guidance.outcome)}
                            </span>
                            {ev.vs_prior_guidance.status === "computed" && ev.vs_prior_guidance.pct_diff != null ? (
                              <span className="mono">{formatQuantityDisplay(ev.vs_prior_guidance.pct_diff, "percent")}</span>
                            ) : (
                              <span className="meta">{ev.vs_prior_guidance.reason ?? "上期指引缺失"}</span>
                            )}
                          </div>
                          {ev.vs_prior_guidance.benchmark_value != null ? (
                            <div className="meta">中值 {formatNumberString(ev.vs_prior_guidance.benchmark_value)}</div>
                          ) : null}
                        </td>
                        <td>
                          <div className="chips">
                            <span className={`role light ${surpriseTone(ev.vs_consensus.outcome)}`}>
                              {surpriseLabel(ev.vs_consensus.outcome)}
                            </span>
                            {ev.vs_consensus.status === "computed" && ev.vs_consensus.pct_diff != null ? (
                              <span className="mono">{formatQuantityDisplay(ev.vs_consensus.pct_diff, "percent")}</span>
                            ) : (
                              <span className="meta">{ev.vs_consensus.reason ?? "一致预期不可用"}</span>
                            )}
                          </div>
                          {ev.vs_consensus.benchmark_value != null ? (
                            <div className="meta">预期 {formatNumberString(ev.vs_consensus.benchmark_value)}</div>
                          ) : null}
                        </td>
                        <td>
                          <div className="chips">
                            <span className={`role light ${guidanceDirectionTone(ng.direction)}`}>
                              {guidanceDirectionLabel(ng.direction)}
                            </span>
                            {ng.pct_change != null ? (
                              <span className="mono">{formatQuantityDisplay(ng.pct_change, "percent")}</span>
                            ) : null}
                          </div>
                          {ngRange ? <div className="meta">新指引 {ngRange}</div> : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <p className="empty-note">
            暂无结构化 Beat/Miss 对齐记录。
            {(snap.earnings_release?.current?.guidance_passages ?? []).length > 0
              ? ` 已提取 ${snap.earnings_release?.current?.guidance_passages?.length} 段 8-K 指引原文供参考。`
              : ""}
          </p>
        )}

        {/* GAAP vs Non-GAAP Reconciliation Summary */}
        {nongaapBridges.length > 0 ? (
          <div className="ev-block">
            <div className="gtitle">GAAP → Non-GAAP 调节表验算</div>
            <ul className="list">
              {nongaapBridges.map((br) => (
                <li className="item" key={`${br.metric_name}-${br.period_label}`}>
                  <div className="line">
                    <span className="title">{formatMetricName(br.metric_name)} ({br.period_label})</span>
                    <span className={`role light ${br.ties ? "support" : "error"}`}>
                      {br.tie_status === "exact_tie"
                        ? "精确勾稽"
                        : br.tie_status === "ties_within_rounding_tolerance"
                          ? "容差内勾稽"
                          : `勾稽差异 (${br.discrepancy})`}
                    </span>
                  </div>
                  <div className="chips">
                    <span>GAAP: {formatQuantityDisplay(br.gaap_value, br.unit, br.currency)}</span>
                    <span>Non-GAAP: {formatQuantityDisplay(br.reported_non_gaap_value, br.unit, br.currency)}</span>
                    <span>调节合计: {formatQuantityDisplay(br.adjustments_sum, br.unit, br.currency)}</span>
                    {br.uplift_pct.status === "computed" && br.uplift_pct.value != null ? (
                      <span>放大比例: {formatQuantityDisplay(br.uplift_pct.value, "percent")}</span>
                    ) : null}
                  </div>
                  {(br.classified_adjustments ?? []).some((adj) => adj.recurring_flag) ? (
                    <div className="meta">
                      经常性剔除项：
                      {br.classified_adjustments
                        .filter((adj) => adj.recurring_flag)
                        .map((adj) => `${adj.label} (${formatQuantityDisplay(adj.amount, adj.unit, adj.currency)})`)
                        .join("；")}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Valuation Multiples Strip */}
        <div className="ev-block">
          <div className="gtitle">估值倍数条 (Valuation Strip)</div>
          {valuation ? (
            <div className="grid2 ed-valuation-grid" data-testid="valuation-strip">
              {valuationMetrics.map(({ key, label, metric }) => {
                const formatted = formatDiagnosticMetricValue(metric);
                return (
                  <div className="item" key={key} data-metric-key={key}>
                    <div className="line">
                      <span className="meta grow">{label}</span>
                      <span className={`role light ${formatted.status === "computed" ? "neutral" : formatted.status === "disabled_by_sector" ? "review" : "neutral"}`}>
                        {formatted.status === "computed" ? "已计算" : formatted.status === "disabled_by_sector" ? "行业禁用" : "暂缺"}
                      </span>
                    </div>
                    <span className="value">{formatted.text}</span>
                    {formatted.sub ? <span className="meta">{formatted.sub}</span> : null}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="empty-note">Stage 0 快照未提供估值输入（可能缺少行情价格或财务序列）。</p>
          )}
        </div>

        {/* Reverse-DCF + Expandable 3x3 WACC x Terminal Growth Sensitivity Matrix */}
        {reverseDcf ? (
          <div className="item ed-reverse-dcf" data-testid="reverse-dcf-block">
            <div className="line">
              <div className="grow">
                <div className="title">Reverse-DCF 隐含 5 年 FCF 增速</div>
                <div className="meta">
                  模型参数：WACC {formatNumberString(reverseDcf.wacc_pct)}% · 永续增长率 g {formatNumberString(reverseDcf.terminal_growth_pct)}% · {reverseDcf.forecast_years} 年显性期
                </div>
              </div>
              {reverseDcf.status === "computed" && reverseDcf.implied_5y_fcf_cagr_pct != null ? (
                <span className="value lg" data-testid="reverse-dcf-cagr">
                  {formatQuantityDisplay(reverseDcf.implied_5y_fcf_cagr_pct, "percent")}
                </span>
              ) : (
                <span className="role light review">
                  {reverseDcf.status === "disabled_by_sector" ? "行业门控禁用" : "无法反推"}
                </span>
              )}
            </div>

            {reverseDcf.status === "computed" ? (
              <>
                <div className="chips">
                  {reverseDcf.target_ev != null ? <span>Target EV: {formatNumberString(reverseDcf.target_ev)}</span> : null}
                  {reverseDcf.base_ttm_fcf != null ? <span>Base TTM FCF: {formatNumberString(reverseDcf.base_ttm_fcf)}</span> : null}
                  {reverseDcf.terminal_value_share_pct != null ? <span>终值占比: {formatNumberString(reverseDcf.terminal_value_share_pct)}%</span> : null}
                  {reverseDcf.implied_exit_fcf_multiple != null ? <span>隐含退出倍数: {formatNumberString(reverseDcf.implied_exit_fcf_multiple)}x</span> : null}
                </div>
                {(reverseDcf.sensitivity_matrix ?? []).length > 0 ? (
                  <div>
                    <button
                      type="button"
                      className="btn plain"
                      aria-expanded={matrixOpen}
                      onClick={() => setMatrixOpen((open) => !open)}
                    >
                      <Icon name={matrixOpen ? "up" : "down"} size={14} />
                      {matrixOpen ? "收起 WACC × 永续增速 3×3 敏感性矩阵" : "展开 WACC × 永续增速 3×3 敏感性矩阵"}
                    </button>
                    {matrixOpen ? (
                      <div className="md-table-scroll" data-testid="reverse-dcf-matrix">
                        <table className="ev-table" aria-label="Reverse-DCF 3x3 WACC 与永续增长率敏感性矩阵">
                          <thead>
                            <tr>
                              <th>WACC \ 永续 g</th>
                              {(reverseDcf.terminal_growth_axis_pct ?? []).map((g) => (
                                <th key={g} className="md-num">g = {formatNumberString(g)}%</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {(reverseDcf.sensitivity_matrix ?? []).map((rowCells, rIdx) => {
                              const waccVal = reverseDcf.wacc_axis_pct?.[rIdx] ?? rowCells[0]?.wacc_pct ?? "";
                              return (
                                <tr key={`${waccVal}-${rIdx}`}>
                                  <th scope="row">WACC = {formatNumberString(waccVal)}%</th>
                                  {rowCells.map((cell, cIdx) => {
                                    const isCenter =
                                      cell.wacc_pct === reverseDcf.wacc_pct
                                      && cell.terminal_growth_pct === reverseDcf.terminal_growth_pct;
                                    return (
                                      <td key={`${rIdx}-${cIdx}`} className="md-num">
                                        {cell.status === "computed" && cell.implied_5y_fcf_cagr_pct != null ? (
                                          <span className={isCenter ? "value" : undefined}>
                                            {formatNumberString(cell.implied_5y_fcf_cagr_pct)}%
                                          </span>
                                        ) : (
                                          <span className="meta" title={cell.reason ?? undefined}>
                                            {cell.reason ?? "N/A"}
                                          </span>
                                        )}
                                      </td>
                                    );
                                  })}
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </>
            ) : (
              <p className="callout-lite warn" data-testid="reverse-dcf-unavailable">
                <Icon name="alert" size={14} />
                <span>{reverseDcf.reason ?? "TTM FCF <= 0 或缺少收盘价，Reverse-DCF 已显式降级停算。"}</span>
              </p>
            )}
          </div>
        ) : null}
      </section>

      {/* Section 2: 盈利质量排雷区 */}
      <section className="chart-card ed-section" aria-label="盈利质量排雷区">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">2. 盈利质量排雷区</h3>
            <p className="chart-subtitle">
              透明阈值逐项异常标注（零黑箱综合打分），每条红/黄灯均附带 MD&amp;A / 附注核查线索
            </p>
          </div>
          <div className="chips" data-testid="flag-counts">
            <span className="role light error">红灯 {flagCounts.red}</span>
            <span className="role light review">黄灯 {flagCounts.yellow}</span>
            <span className="role light support">绿灯 {flagCounts.green}</span>
          </div>
        </div>

        {flags.length > 0 ? (
          <ul className="list ed-flag-list">
            {flags.map((flag) => (
              <li className="item" key={flag.flag_id} id={`ed-flag-${flag.flag_id}`}>
                <div className="line">
                  <span className={`role ${severityTone(flag.severity)}`}>
                    {flagSeverityLabel(flag.severity)} · {FLAG_GROUP_LABEL[flag.group] ?? flag.group}
                  </span>
                  <span className="title grow">{flag.metric_name}</span>
                  <span className="value">{formatQuantityDisplay(flag.observed_value, flag.unit)}</span>
                </div>
                <p className="meta">触发规则：{flag.threshold_rule}</p>
                <p>{flag.summary}</p>
                <div className="callout-lite ok">
                  <Icon name="search" size={14} />
                  <span>
                    <strong>MD&amp;A / 附注核查指引：</strong>
                    {flag.investigation_prompt}
                  </span>
                </div>
                {(flag.input_ids ?? []).length > 0 ? (
                  <div className="chips">
                    <span className="meta">输入来源 ID:</span>
                    {(flag.input_ids ?? []).map((id) => (
                      <span className="mono meta" key={id}>{id}</span>
                    ))}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-note">
            {sector.is_gated
              ? `当前为 ${SECTOR_LABEL[sector.sector_category]}，工业类盈利质量排雷指标已按 SIC 门控关闭。`
              : "未触发盈利质量红/黄/绿灯异常旗标。"}
          </p>
        )}

        {nongaapWarnings.length > 0 ? (
          <div className="ev-block">
            <div className="gtitle">Non-GAAP 调节风险警示</div>
            <ul className="list">
              {nongaapWarnings.map((w) => (
                <li className="item" key={`${w.flag_id}-${w.adjustment_label ?? w.metric_name}`}>
                  <div className="line">
                    <span className={`role ${severityTone(w.severity)}`}>
                      {flagSeverityLabel(w.severity)} · Non-GAAP
                    </span>
                    <span className="title grow">{w.metric_name}{w.adjustment_label ? ` · ${w.adjustment_label}` : ""}</span>
                  </div>
                  <p>{w.summary}</p>
                  <p className="meta">附注核查：{w.investigation_prompt}</p>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {diagnostics ? (
          <Disclosure summary="查看全部 5 组确定性诊断指标（含利润率桥、现金含金量、营运资本 DSO/DIO/DPO、稀释与杠杆）">
            {diagnostics.margin_bridge.length > 0 ? (
              <div className="md-table-scroll">
                <table className="ev-table" aria-label="利润率变动桥明细">
                  <thead>
                    <tr>
                      <th>利润率桥</th>
                      <th className="md-num">本期</th>
                      <th className="md-num">QoQ (bps)</th>
                      <th className="md-num">YoY (bps)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diagnostics.margin_bridge.map((mb) => {
                      const cur = formatDiagnosticMetricValue(mb.current_pct);
                      const qoq = formatDiagnosticMetricValue(mb.qoq_change_bps);
                      const yoy = formatDiagnosticMetricValue(mb.yoy_change_bps);
                      return (
                        <tr key={mb.line}>
                          <th scope="row">{MARGIN_LINE_LABEL[mb.line] ?? mb.line}</th>
                          <td className="md-num">{cur.text}</td>
                          <td className="md-num">{qoq.text}</td>
                          <td className="md-num">{yoy.text}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : null}

            <div className="grid2">
              {[
                { label: "OCF / 净利润", m: diagnostics.earnings_quality.ocf_to_ni },
                { label: "FCF / 净利润", m: diagnostics.earnings_quality.fcf_to_ni },
                { label: "SBC / 营收", m: diagnostics.earnings_quality.sbc_to_revenue_pct },
                { label: "SBC / FCF", m: diagnostics.earnings_quality.sbc_to_fcf_pct },
                { label: "FCF ex-SBC", m: diagnostics.earnings_quality.fcf_ex_sbc },
                { label: "稀释股本 YoY", m: diagnostics.capital_allocation.diluted_shares_yoy_pct },
                { label: "DSO (应收天数)", m: diagnostics.working_capital.dso.current },
                { label: "DIO (存货天数)", m: diagnostics.working_capital.dio.current },
                { label: "CCC (现金周期)", m: diagnostics.working_capital.ccc.current },
                { label: "Net Debt / TTM EBITDA", m: diagnostics.balance_sheet.net_debt_to_ttm_ebitda },
                { label: "利息覆盖 (EBIT/Int)", m: diagnostics.balance_sheet.interest_coverage },
                { label: "流动比率 (Current Ratio)", m: diagnostics.balance_sheet.current_ratio },
              ].map(({ label, m }) => {
                const disp = formatDiagnosticMetricValue(m);
                return (
                  <div className="item" key={label}>
                    <span className="meta">{label}</span>
                    <span className="value">{disp.text}</span>
                    {disp.status !== "computed" && disp.sub ? <span className="meta">{disp.sub}</span> : null}
                  </div>
                );
              })}
            </div>
          </Disclosure>
        ) : null}
      </section>

      {/* Section 3: 多季财务趋势与分部/行业 KPI 表 */}
      <section className="chart-card ed-section" aria-label="多季财务趋势与分部行业 KPI 表">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">3. 多季财务趋势与分部 / 行业 KPI 表</h3>
            <p className="chart-subtitle">
              {dossier.selected_l2_pack_id
                ? `已激活 L2 行业包：${L2_PACK_LABEL[dossier.selected_l2_pack_id] ?? dossier.selected_l2_pack_id}`
                : "L1 通用美股财务趋势与 8-K / R*.htm 原始披露拆解"}
            </p>
          </div>
          {dossier.dispatch_plan?.selected_l2_pack_reason ? (
            <span className="meta">{dossier.dispatch_plan.selected_l2_pack_reason}</span>
          ) : null}
        </div>

        {/* Multi-Quarter Financial Trends */}
        {statements.length > 0 && activeStatementFields.length > 0 ? (
          <div className="ev-block">
            <div className="gtitle">多季财务趋势 (SEC XBRL / 财报三表)</div>
            <div className="md-table-scroll">
              <table className="ev-table" aria-label="多季财务趋势表">
                <thead>
                  <tr>
                    <th>科目</th>
                    <th>单位</th>
                    {statements.map((st) => (
                      <th key={`${st.period_label}-${st.period_end ?? ""}`} className="md-num">
                        {st.period_label}
                        <span className="meta"> · {st.period_type}</span>
                      </th>
                    ))}
                    <th>来源溯源</th>
                  </tr>
                </thead>
                <tbody>
                  {activeStatementFields.map(({ key, label }) => {
                    const rowMvs = statements.map((st) => st[key] as ResearchV2.MetricValue | null | undefined);
                    const firstMv = rowMvs.find((mv) => mv !== null && mv !== undefined);
                    const unitText = [
                      firstMv?.currency ?? latestStmt?.currency,
                      firstMv?.unit ?? latestStmt?.monetary_unit,
                    ]
                      .filter(Boolean)
                      .join(" / ");
                    const sourceId = firstMv?.source_id ?? defaultSecFilingSourceId;
                    const loc = firstMv?.xbrl_tag ?? firstMv?.fact_id ?? key;
                    return (
                      <tr key={key}>
                        <th scope="row">{label}</th>
                        <td className="meta">{unitText || "—"}</td>
                        {rowMvs.map((mv, idx) => (
                          <td key={`${key}-${idx}`} className="md-num">
                            {mv ? formatNumberString(mv.value) : "—"}
                          </td>
                        ))}
                        <td>
                          {sourceId && onInspectSource ? (
                            <button
                              type="button"
                              className="chart-src-btn"
                              onClick={() => onInspectSource(sourceId, loc)}
                            >
                              {firstMv?.fact_id ?? firstMv?.xbrl_tag ?? sourceId}
                            </button>
                          ) : (
                            <span className="meta mono">{firstMv?.fact_id ?? firstMv?.xbrl_tag ?? "—"}</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <p className="empty-note">Stage 0 快照暂无多季标准化三表行。</p>
        )}

        {/* Segment / 8-K Exhibit 99.1 Tables */}
        {ex99Tables.length > 0 ? (
          <div className="ev-block">
            <div className="gtitle">8-K Exhibit 99.1 业绩附表与分部表</div>
            {ex99Tables.map((entry, idx) => {
              const title = entry.table.title || `业绩附表 ${idx + 1}`;
              const headers = entry.table.headers ?? [];
              const rows = entry.table.rows ?? [];
              return (
                <div className="chart-card" key={`${title}-${idx}`}>
                  <div className="chart-head">
                    <div className="chart-title-row">
                      <span className="title">{title}</span>
                      <span className="meta">{entry.label}</span>
                      {entry.table.is_nongaap ? <span className="chart-badge is-sim">Non-GAAP</span> : null}
                    </div>
                    {entry.sourceId && onInspectSource ? (
                      <button
                        type="button"
                        className="chart-src-btn"
                        onClick={() => onInspectSource(entry.sourceId!, title)}
                      >
                        查看原文 ({entry.sourceId})
                      </button>
                    ) : null}
                  </div>
                  <div className="md-table-scroll">
                    <table className="ev-table" aria-label={title}>
                      {headers.length > 0 ? (
                        <thead>
                          <tr>
                            {headers.map((h, hIdx) => (
                              <th key={`${h}-${hIdx}`}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                      ) : null}
                      <tbody>
                        {rows.map((r, rIdx) => (
                          <tr key={rIdx}>
                            {r.map((cell, cIdx) => (
                              <td key={cIdx} className={cIdx > 0 && isFiniteSignedNumericString(cell.replace(/[,%$]/g, "")) ? "md-num" : undefined}>
                                {cell}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        {/* Segment & Industry KPI Evidence Cards with Clickable Citations */}
        {segmentCards.length > 0 ? (
          <div className="ev-block">
            <div className="gtitle">分部、行业 KPI 与取证卡片 (带可点引文)</div>
            <ul className="list">
              {segmentCards.map((card) => (
                <li className="item" key={card.card_id} id={`ed-card-${card.card_id}`}>
                  <div className="line">
                    <span className="role light counter">{card.topic_tag}</span>
                    <span className="meta">{card.worker_role}</span>
                    <span className="meta mono">{card.card_id}</span>
                  </div>
                  <p>{card.finding}</p>
                  {(card.numeric_claims ?? []).length > 0 ? (
                    <div className="md-table-scroll">
                      <table className="ev-table" aria-label={`${card.topic_tag} 指标明细`}>
                        <thead>
                          <tr>
                            <th>KPI / 指标</th>
                            <th className="md-num">数值</th>
                            <th>期间</th>
                            <th>同比 / 备注</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(card.numeric_claims ?? []).map((claim, idx) => {
                            const metricName = String(claim["metric"] ?? claim["label"] ?? claim["name"] ?? `KPI ${idx + 1}`);
                            const rawVal = claim["value"] !== undefined && claim["value"] !== null ? String(claim["value"]) : "—";
                            const unit = typeof claim["unit"] === "string" ? claim["unit"] : null;
                            const curr = typeof claim["currency"] === "string" ? claim["currency"] : null;
                            const period = typeof claim["period"] === "string" ? claim["period"] : "—";
                            const yoy = typeof claim["yoy"] === "string" ? claim["yoy"] : typeof claim["note"] === "string" ? claim["note"] : "—";
                            return (
                              <tr key={`${card.card_id}-claim-${idx}`}>
                                <th scope="row">{metricName}</th>
                                <td className="md-num">{formatQuantityDisplay(rawVal, unit, curr)}</td>
                                <td>{period}</td>
                                <td>{yoy}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                  {(card.citations ?? []).length > 0 ? (
                    <div className="chips" aria-label="来源引文">
                      <span className="meta">引文溯源:</span>
                      {(card.citations ?? []).map((cit, cIdx) => {
                        const loc = formatCitationLocation(cit);
                        const label = formatSourceRefLabel(cit.source_id, loc);
                        return (
                          <button
                            key={`${card.card_id}-cit-${cIdx}`}
                            type="button"
                            className="fact-ref"
                            title={cit.quotation}
                            onClick={() => onInspectSource?.(cit.source_id, loc)}
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                  {card.caveat ? <p className="meta">注意：{card.caveat}</p> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* FilingSummary Table Catalog */}
        {snap.filing_table_catalog && (snap.filing_table_catalog.all_reports ?? []).length > 0 ? (
          <Disclosure summary={`FilingSummary 官方附注表索引（共 ${snap.filing_table_catalog.report_count} 张 R*.htm）`}>
            <div className="chips">
              {(snap.filing_table_catalog.all_reports ?? []).slice(0, 24).map((rep) => (
                <button
                  key={rep.r_file}
                  type="button"
                  className="fact-ref"
                  title={rep.long_name}
                  onClick={() => onInspectSource?.(defaultSecFilingSourceId ?? rep.r_file, `${rep.r_file}: ${rep.short_name}`)}
                >
                  {rep.r_file} · {rep.short_name}
                </button>
              ))}
            </div>
          </Disclosure>
        ) : null}

        {otherCards.length > 0 ? (
          <Disclosure summary={`其他已核验取证卡片（${otherCards.length} 条）`}>
            <ul className="list">
              {otherCards.map((card) => (
                <li className="item" key={card.card_id} id={`ed-card-${card.card_id}`}>
                  <div className="line">
                    <span className="role light counter">{card.topic_tag}</span>
                    <span className="meta">{card.worker_role}</span>
                    <span className="meta mono">{card.card_id}</span>
                  </div>
                  <p>{card.finding}</p>
                  {(card.citations ?? []).length > 0 ? (
                    <div className="chips">
                      {(card.citations ?? []).map((cit, cIdx) => {
                        const loc = formatCitationLocation(cit);
                        return (
                          <button
                            key={`${card.card_id}-ocit-${cIdx}`}
                            type="button"
                            className="fact-ref"
                            title={cit.quotation}
                            onClick={() => onInspectSource?.(cit.source_id, loc)}
                          >
                            {formatSourceRefLabel(cit.source_id, loc)}
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </Disclosure>
        ) : null}
      </section>

      {/* Section 4: 跨期披露 Redline 变动区 */}
      <section className="chart-card ed-section" aria-label="跨期披露 Redline 变动区">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">4. 跨期披露 Redline 变动区</h3>
            <p className="chart-subtitle">10-Q / 10-K Risk Factors、会计政策与关键附注的实质性增删改对比</p>
          </div>
          {redlineSummary ? (
            <div className="chips" data-testid="redline-counts">
              <span className="role light support">Added +{formatInteger(redlineSummary.total_added ?? 0)}</span>
              <span className="role light error">Removed -{formatInteger(redlineSummary.total_removed ?? 0)}</span>
              <span className="role light review">Revised ~{formatInteger(redlineSummary.total_revised ?? 0)}</span>
              {(redlineSummary.total_filtered_noise ?? 0) > 0 ? (
                <span className="meta">已过滤噪音 {formatInteger(redlineSummary.total_filtered_noise ?? 0)} 处</span>
              ) : null}
            </div>
          ) : null}
        </div>

        {redlineSections.length > 0 ? (
          <div className="list">
            {redlineSections.map(([sectionKey, sec]) => {
              const added = sec.added ?? [];
              const removed = sec.removed ?? [];
              const revised = sec.revised ?? [];
              const hasItems = added.length > 0 || removed.length > 0 || revised.length > 0;
              return (
                <div className="item" key={sectionKey} id={`ed-redline-${sectionKey}`}>
                  <div className="line">
                    <span className="title grow">{REDLINE_SECTION_LABEL[sectionKey] ?? sectionKey}</span>
                    <span className={`role light ${sec.has_substantive_changes ? "review" : "neutral"}`}>
                      {sec.has_substantive_changes ? "存在实质性披露变化" : "无实质性变化"}
                    </span>
                    {defaultSecFilingSourceId && onInspectSource ? (
                      <button
                        type="button"
                        className="chart-src-btn"
                        onClick={() => onInspectSource(defaultSecFilingSourceId, `redline:${sectionKey}`)}
                      >
                        对照原文
                      </button>
                    ) : null}
                  </div>

                  {hasItems ? (
                    <ul className="diff">
                      {added.map((d, idx) => (
                        <li className="add" key={`add-${idx}`}>
                          <span className="sign" aria-hidden="true">+</span>
                          <div>
                            <span className="role light support">Added · 新增</span>{" "}
                            <span>{d.current_text ?? d.redline_markup ?? ""}</span>
                          </div>
                        </li>
                      ))}
                      {removed.map((d, idx) => (
                        <li className="remove" key={`rem-${idx}`}>
                          <span className="sign" aria-hidden="true">-</span>
                          <div>
                            <span className="role light error">Removed · 删除</span>{" "}
                            <del>{d.prior_text ?? d.redline_markup ?? ""}</del>
                          </div>
                        </li>
                      ))}
                      {revised.map((d, idx) => (
                        <li className="change" key={`rev-${idx}`}>
                          <span className="sign" aria-hidden="true">~</span>
                          <div>
                            <span className="role light review">Revised · 修订</span>
                            {d.similarity !== null && d.similarity !== undefined ? (
                              <span className="meta"> (相似度 {Math.round(d.similarity * 100)}%)</span>
                            ) : null}
                            {d.redline_markup ? (
                              <div>{d.redline_markup}</div>
                            ) : (
                              <>
                                {d.prior_text ? <div><del>{d.prior_text}</del></div> : null}
                                {d.current_text ? <div><b>{d.current_text}</b></div> : null}
                              </>
                            )}
                          </div>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="meta">
                      本章节未检测到实质性段落增删（未改动 {sec.unchanged_count ?? 0} 段，过滤滚动噪音 {sec.filtered_noise_count ?? 0} 处）。
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="empty-note">暂无跨期 10-Q / 10-K Redline 变动数据。</p>
        )}
      </section>

      {/* Section 5: 证据锚定空头反证（Bear Case） */}
      <section className="chart-card ed-section" aria-label="证据锚定空头反证">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">5. 证据锚定空头反证 (Bear Case)</h3>
            <p className="chart-subtitle">物理隔离多头叙事的独立反方质询：每条必须绑定硬证据锚点与下季可观测证伪指标</p>
          </div>
          <span className="role light counter">已通过门控 {bearCases.length} 条</span>
        </div>

        {bearCases.length > 0 ? (
          <ul className="list ed-bear-list">
            {bearCases.map((bear) => (
              <li className="item" key={bear.bear_id ?? bear.thesis_challenge} data-testid="bear-case-card">
                <div className="line">
                  <span className={`role ${severityTone(bear.severity ?? "medium")}`}>
                    {bearSeverityLabel(bear.severity ?? "medium")}
                  </span>
                  <span className="chart-badge is-updated" data-testid="bear-anchor-kind">
                    {ANCHOR_KIND_LABEL[bear.anchor_kind] ?? bear.anchor_kind}
                  </span>
                  {bear.bear_id ? <span className="meta mono">{bear.bear_id}</span> : null}
                </div>

                <p className="title">{bear.thesis_challenge}</p>

                {(bear.supporting_numbers ?? []).length > 0 ? (
                  <div className="chips">
                    <span className="meta">支撑数字:</span>
                    {(bear.supporting_numbers ?? []).map((num, idx) => (
                      <span className="chart-badge is-sim" key={`${num}-${idx}`}>{num}</span>
                    ))}
                  </div>
                ) : null}

                <div className="chips" aria-label="锚定证据">
                  <span className="meta">锚定证据:</span>
                  {bear.anchored_evidence_ids.map((anchorId) => (
                    <button
                      key={anchorId}
                      type="button"
                      className="fact-ref"
                      title={anchorId}
                      onClick={() => handleInspectAnchor(anchorId)}
                    >
                      {formatAnchorLabel(anchorId)}
                    </button>
                  ))}
                </div>

                <div className="callout ed-invalidation-trigger" data-testid="bear-invalidation-trigger">
                  <div className="h">
                    <Icon name="alert" size={14} />
                    <span>下季证伪指标 (Next-Quarter Invalidation Trigger)</span>
                  </div>
                  <p>{bear.next_quarter_invalidation_trigger}</p>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-note">当前底稿暂无通过证据锚定门控的空头反证条目。</p>
        )}

        {rejectedBearCases.length > 0 ? (
          <Disclosure summary={`服务端门控已拒绝 ${rejectedBearCases.length} 条缺乏硬证据锚点的空头陈词`}>
            <ul className="list">
              {rejectedBearCases.map((rej, idx) => (
                <li className="item" key={`${rej.bear_id ?? "rej"}-${idx}`}>
                  <div className="line">
                    <span className="role light error">已拒绝</span>
                    <span className="meta mono">{rej.bear_id || `rej_${idx + 1}`}</span>
                  </div>
                  {rej.thesis_challenge ? <p>{rej.thesis_challenge}</p> : null}
                  <p className="meta">拒绝原因：{rej.rejection_reason}</p>
                </li>
              ))}
            </ul>
          </Disclosure>
        ) : null}
      </section>

      {/* Section 6: 未获取缺口与模型槽位摘要 */}
      <section className="chart-card ed-section" aria-label="未获取缺口与模型槽位摘要">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">6. 未获取缺口与模型槽位摘要</h3>
            <p className="chart-subtitle">局部失败存活记录（open_gaps）与 4 逻辑模型槽位执行账本</p>
          </div>
        </div>

        {/* Open Gaps */}
        {openGaps.length > 0 ? (
          <div className="callout ed-open-gaps" role="region" aria-label="未获取缺口" data-testid="open-gaps-callout">
            <div className="h">
              <Icon name="alert" size={16} />
              <span>未获取缺口与降级说明 ({openGaps.length})</span>
            </div>
            <ul>
              {openGaps.map((gap, idx) => (
                <li key={gap.gap_id ?? `${gap.code}-${idx}`}>
                  <strong>[{gap.stage}{gap.worker_role ? ` · ${gap.worker_role}` : ""}] {gap.code}</strong>: {gap.message}
                  {gap.fallback ? <span className="why">（回退方案：{gap.fallback}）</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="callout-lite ok" data-testid="open-gaps-clear">
            <Icon name="ok" size={15} />
            <span>无未获取缺口（0 open gaps），所有计划支线均已完成。</span>
          </div>
        )}

        {/* Slot Usage Summary */}
        <div className="ev-block" data-testid="slot-usage-summary">
          <div className="gtitle">模型槽位与调用摘要</div>
          <div className="chips">
            <span className="chart-badge is-updated">总模型调用 {formatInteger(dossier.slot_usage_summary.total_calls ?? 0)} 次</span>
            <span className="chart-badge is-updated">总 Token {formatInteger(dossier.slot_usage_summary.total_tokens ?? 0)}</span>
            <span className="chart-badge is-updated">Stage 0 模型调用 {formatInteger(dossier.slot_usage_summary.stage_0_model_calls ?? 0)} 次</span>
            <span className="chart-badge is-updated">Deep Report 检索调用 {formatInteger(dossier.slot_usage_summary.deep_report_tool_calls ?? 0)} 次</span>
          </div>
          <div className="grid2">
            {slotEntries.map(({ slotKey, rec, routingItem }) => {
              const modelName = rec?.model ?? (typeof routingItem?.["model"] === "string" ? routingItem["model"] : null) ?? "默认主模型";
              const protocol = rec?.protocol ?? (typeof routingItem?.["protocol"] === "string" ? routingItem["protocol"] : null);
              const fallbackUsed = rec?.fallback_used ?? Boolean(routingItem?.["fallback_used"]);
              return (
                <div className="item" key={slotKey}>
                  <div className="line">
                    <span className="title grow">{SLOT_LABEL[slotKey]}</span>
                    {fallbackUsed ? (
                      <span className="role light neutral">回落主研究模型</span>
                    ) : (
                      <span className="role light counter">独立槽位</span>
                    )}
                  </div>
                  <div className="meta">
                    模型：<span className="mono">{modelName}</span>
                    {protocol ? <> · 协议：<span className="mono">{protocol}</span></> : null}
                  </div>
                  <div className="chips">
                    <span>调用 {formatInteger(rec?.calls ?? 0)} 次</span>
                    <span>Token {formatInteger(rec?.total_tokens ?? 0)}</span>
                    {rec?.elapsed_ms ? <span>耗时 {formatInteger(rec.elapsed_ms)} ms</span> : null}
                    {rec?.failed_calls ? <span className="role light error">失败 {rec.failed_calls}</span> : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* Section 7: 正文报告渲染区 */}
      <section className="chart-card ed-section ed-report-body" aria-label="正文报告渲染区" data-testid="dossier-report-body">
        <div className="chart-head">
          <div className="chart-title-wrap">
            <h3 className="chart-title">
              7. {activeMode === "deep_report" ? "完整深研报告 (Deep Report)" : "买方备忘录 (Memo)"}
            </h3>
            <p className="chart-subtitle">
              {activeMode === "deep_report"
                ? "基于同一份已锁定 EarningsDossier 按需合成并缓存，零新增取证工具调用"
                : "默认首跑买方备忘录（Tear Sheet），与 Deep Report 共享完全一致的事实口径"}
            </p>
          </div>
          <span className="role light neutral">
            {activeMode === "deep_report" ? (effectiveDeepReport ? "已缓存" : "按需生成中") : "Memo 档"}
          </span>
        </div>

        {activeMode === "deep_report" && isDeepLoading ? (
          <div role="status" aria-live="polite" data-testid="deep-report-loading">
            <p className="meta">正在基于已锁定底稿生成完整深研报告（0 检索调用）…</p>
            <div className="skeleton line" />
            <div className="skeleton line" />
            <div className="skeleton line short" />
          </div>
        ) : activeMode === "deep_report" && effectiveDeepError ? (
          <div className="callout error" role="alert">
            <div className="h">
              <Icon name="alert" size={16} />
              <span>生成完整深研报告失败</span>
            </div>
            <p>{effectiveDeepError}</p>
            {onRequestDeepReport ? (
              <div className="actions">
                <button type="button" className="btn primary" onClick={handleRetryDeepReport}>
                  重试生成完整深研报告
                </button>
              </div>
            ) : null}
          </div>
        ) : activeNarrative ? (
          <MarkdownContent content={activeNarrative} variant="report" />
        ) : (
          <p className="empty-note">
            {activeMode === "deep_report"
              ? "尚未生成完整深研报告，点击上方“完整深研报告 (Deep Report)”或配置生成回调。"
              : "暂无 Memo 正文内容。"}
          </p>
        )}
      </section>
    </section>
  );
}
