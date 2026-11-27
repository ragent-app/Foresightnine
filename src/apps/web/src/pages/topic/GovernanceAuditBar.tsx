import { useEffect, useState } from "react";
import {
  useGovernanceAudit,
  useGovernanceCdaBrief,
  useGuidanceBackfill,
  type GovernanceAuditReport,
  type GovernanceLight,
} from "../../api/governance";
import { showToast } from "../../ui/toast";

interface GovernanceAuditBarProps {
  symbol?: string | null;
  cik?: string | null;
}

const LIGHT_LABEL: Record<GovernanceLight, { text: string; cls: string }> = {
  green: { text: "良好", cls: "peer-light peer-light-green" },
  yellow: { text: "留意", cls: "peer-light peer-light-yellow" },
  red: { text: "警示", cls: "peer-light peer-light-red" },
  not_applicable: { text: "金融豁免", cls: "peer-light peer-light-na" },
  unavailable: { text: "待补齐", cls: "peer-light peer-light-na" },
};

const PROFILE_LABEL: Record<string, string> = {
  chronic_sandbagger: "保守超预期（沙袋型）",
  high_credibility: "高兑现信用",
  mixed: "表现分化",
  guidance_miss_prone: "指引易不及预期",
  insufficient_history: "样本不足（<3季）",
};

function fmtUsdB(raw?: string | null): string {
  if (!raw) return "—";
  const n = Number(raw);
  if (!Number.isFinite(n)) return raw;
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  return `$${n.toLocaleString("en-US")}`;
}

/**
 * V2.5 Governance, Capital Allocation & Guidance Credibility Bar mounted in TopicPage.
 * Defaults strictly to `cache_only: true` with `cik` (0 network / 0 LLM on mount);
 * expands into the 3-pillar deterministic GovernanceAuditPanel with on-demand Layer 2 triggers.
 */
export function GovernanceAuditBar({ symbol, cik }: GovernanceAuditBarProps) {
  const sym = (symbol || "").trim().toUpperCase();
  const [report, setReport] = useState<GovernanceAuditReport | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [checkedCache, setCheckedCache] = useState(false);

  const auditMut = useGovernanceAudit();
  const cdaMut = useGovernanceCdaBrief();
  const backfillMut = useGuidanceBackfill();

  useEffect(() => {
    if (!sym || checkedCache) return;
    setCheckedCache(true);
    auditMut.mutate(
      { symbol: sym, ...(cik ? { cik } : {}), cache_only: true },
      {
        onSuccess: (data) => {
          if (data) setReport(data);
        },
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sym, cik]);

  if (!sym) return null;

  const cap = report?.capital_allocation;
  const inc = report?.incentive_governance;
  const gui = report?.guidance_credibility;

  return (
    <section className="gov-wrap" aria-label="治理与资本配置审计">
      <div className="peer-coord-bar gov-bar">
        <div className="peer-coord-left">
          <span className="peer-coord-tag">治理与资本</span>
          {report ? (
            <>
              <span className={LIGHT_LABEL[report.overall_light].cls}>
                {LIGHT_LABEL[report.overall_light].text}
              </span>
              <span className="peer-coord-summary">{report.headline_anchor_summary}</span>
            </>
          ) : (
            <span className="peer-coord-muted">
              {auditMut.isPending ? "正在拉取 SEC 资本配置与 DEF 14A 体检…" : `${sym} 资本配置 / 高管激励 / 指引信用尚未加载`}
            </span>
          )}
        </div>
        <div className="peer-coord-actions">
          {!report ? (
            <button
              type="button"
              className="btn peer-coord-btn"
              disabled={auditMut.isPending}
              onClick={() =>
                auditMut.mutate(
                  { symbol: sym, ...(cik ? { cik } : {}), cache_only: false },
                  {
                    onSuccess: (data) => {
                      if (data) {
                        setReport(data);
                        setExpanded(true);
                      }
                    },
                    onError: (err) => showToast(`治理体检加载失败：${err.message}`, { tone: "error" }),
                  },
                )
              }
            >
              {auditMut.isPending ? "加载中…" : "加载治理体检 (0 LLM)"}
            </button>
          ) : (
            <button
              type="button"
              className="btn peer-coord-btn"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "收起体检面板" : "展开 3 支柱详情"}
            </button>
          )}
        </div>
      </div>

      {report && expanded && cap && inc && gui ? (
        <div className="gov-panel">
          <div className="gov-grid">
            <article className="gov-card">
              <header className="gov-card-head">
                <h3>1. 真实资本配置与 SBC 净稀释</h3>
                <span className={LIGHT_LABEL[cap.quality_light].cls}>{LIGHT_LABEL[cap.quality_light].text}</span>
              </header>
              <dl className="gov-kv">
                <div><dt>3Y 复权净股本变化</dt><dd>{cap.net_diluted_share_change_3y_pct ? `${cap.net_diluted_share_change_3y_pct}%` : "—"}</dd></div>
                <div><dt>SBC / FCF</dt><dd>{cap.sbc_pct_of_fcf ? `${cap.sbc_pct_of_fcf}%` : (cap.is_financial_gated ? "金融豁免" : "—")}</dd></div>
                <div><dt>回购 / SBC 倍数</dt><dd>{cap.buyback_to_sbc_ratio ? `${cap.buyback_to_sbc_ratio}x` : "—"}</dd></div>
                <div><dt>真实净股东回报率</dt><dd>{cap.true_net_shareholder_yield_pct ? `${cap.true_net_shareholder_yield_pct}%` : "—"}</dd></div>
                <div><dt>最新年回购 / SBC</dt><dd>{fmtUsdB(cap.buybacks_ttm_usd)} / {fmtUsdB(cap.sbc_ttm_usd)}</dd></div>
                <div><dt>未确认 SBC 悬顶</dt><dd>{fmtUsdB(cap.unrecognized_sbc_usd)}{cap.unrecognized_sbc_pct_of_mcap ? ` (${cap.unrecognized_sbc_pct_of_mcap}%)` : ""}</dd></div>
              </dl>
              {cap.fake_buyback_flag ? <div className="peer-warning peer-warning-error">触发「假回购真稀释」红灯</div> : null}
              {cap.notes.length > 0 ? <p className="gov-note">{cap.notes.slice(0, 2).join(" ")}</p> : null}
            </article>

            <article className="gov-card">
              <header className="gov-card-head">
                <h3>2. DEF 14A 高管激励考核锚</h3>
                <span className={LIGHT_LABEL[inc.quality_light].cls}>{LIGHT_LABEL[inc.quality_light].text}</span>
              </header>
              <dl className="gov-kv">
                <div><dt>公司选定核心指标</dt><dd>{inc.company_selected_measure || "—"}</dd></div>
                <div><dt>最高考核档位</dt><dd>{inc.highest_anchor_label}</dd></div>
                <div><dt>年度考核降级</dt><dd>{inc.anchor_downgraded_yoy ? "是（红灯）" : "否"}</dd></div>
                <div><dt>MNPI 时点重叠</dt><dd>{inc.mnpi_red_flag ? "是（红灯）" : "未触发"}</dd></div>
                <div><dt>CEO 账面 / 实得薪酬</dt><dd>{fmtUsdB(inc.latest_peo_total_comp_usd)} / {fmtUsdB(inc.latest_peo_cap_usd)}</dd></div>
                <div><dt>披露口径 / 归档日</dt><dd>{inc.disclosure_regime} {inc.latest_filed_at ? `(${inc.latest_filed_at})` : ""}</dd></div>
              </dl>
              {inc.notes.length > 0 ? <p className="gov-note">{inc.notes.slice(0, 2).join(" ")}</p> : null}
            </article>

            <article className="gov-card">
              <header className="gov-card-head">
                <h3>3. 跨季指引兑现信用台账</h3>
                <span className={LIGHT_LABEL[gui.quality_light].cls}>{LIGHT_LABEL[gui.quality_light].text}</span>
              </header>
              <dl className="gov-kv">
                <div><dt>信用画像</dt><dd>{PROFILE_LABEL[gui.profile] ?? gui.profile}</dd></div>
                <div><dt>已评分季数</dt><dd>{gui.scored_quarters}（共 {gui.total_records} 条）</dd></div>
                <div><dt>超预期 / 符合 / 不及</dt><dd>{gui.beat_count} / {gui.inline_count} / {gui.miss_count}</dd></div>
                <div><dt>平均相对中值偏差</dt><dd>{gui.avg_surprise_vs_mid_pct ? `${gui.avg_surprise_vs_mid_pct}%` : "—"}</dd></div>
              </dl>
              <div className="gov-card-actions">
                <button
                  type="button"
                  className="btn peer-coord-btn"
                  disabled={backfillMut.isPending}
                  onClick={() =>
                    backfillMut.mutate(
                      { symbol: sym, cik: report.cik, max_quarters: 6 },
                      {
                        onSuccess: (res) => {
                          setReport((prev) => (prev ? { ...prev, guidance_credibility: res.guidance_credibility } : prev));
                          showToast(`已从 8-K 回溯提取 ${res.extracted_records} 条指引记录`);
                        },
                        onError: (err) => showToast(`8-K 回溯失败：${err.message}`, { tone: "error" }),
                      },
                    )
                  }
                >
                  {backfillMut.isPending ? "回溯 8-K 中…" : "回溯近 6 季 8-K 指引 (1 LLM)"}
                </button>
              </div>
            </article>
          </div>

          <div className="gov-l2-bar">
            <span className="peer-coord-muted">
              规则版本 {report.rules_version} ({report.rules_sha256}) · 耗时 {report.elapsed_ms}ms
            </span>
            <button
              type="button"
              className="btn primary peer-coord-btn"
              disabled={cdaMut.isPending}
              onClick={() =>
                cdaMut.mutate(
                  { symbol: sym, cik: report.cik },
                  { onError: (err) => showToast(`CD&A 简评失败：${err.message}`, { tone: "error" }) },
                )
              }
            >
              {cdaMut.isPending ? "生成 CD&A 深度归因中…" : "生成 CD&A 深度归因简评 (1 LLM)"}
            </button>
          </div>

          {cdaMut.data ? (
            <div className="peer-brief-card">
              <div className="peer-brief-head">
                <h2>DEF 14A CD&amp;A 深度归因简评</h2>
                <span className="peer-brief-badge">
                  数值核验 {cdaMut.data.verified_metric_count}/{cdaMut.data.total_cited_metric_count} ·
                  原文引文核验 {cdaMut.data.verified_quote_count}/{cdaMut.data.total_cited_quote_count}
                  {cdaMut.data.model_used ? ` · ${cdaMut.data.model_used}` : ""}
                  {cdaMut.data.tokens_used > 0 ? ` · ${cdaMut.data.tokens_used} tok` : ""}
                </span>
              </div>
              <p className="peer-brief-text">{cdaMut.data.summary}</p>
              <p className="peer-brief-text"><strong>激励对齐：</strong>{cdaMut.data.incentive_alignment_assessment}</p>
              <p className="peer-brief-text"><strong>资本配置：</strong>{cdaMut.data.capital_allocation_verdict}</p>
              <div className="skeptic-callout">
                <strong>反方视角（Skeptic）：</strong>
                <span>{cdaMut.data.skeptic_counterview}</span>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
