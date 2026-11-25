import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  useCreatePeerGroup,
  useDeletePeerGroup,
  usePeerAttributionBrief,
  usePeerCompare,
  usePeerSuggest,
  type PeerCompanyMetrics,
  type PeerComparisonMatrix,
  type QualityLight,
} from "../api/peers";
import { showToast } from "../ui/toast";

interface MetricRowSpec {
  key: keyof PeerCompanyMetrics;
  label: string;
  unit: "%" | "x" | "pp" | "$B" | "raw";
  group: "scale" | "growth" | "profit" | "cash" | "valuation";
}

const METRIC_ROWS: MetricRowSpec[] = [
  { key: "market_cap_usd", label: "市值 (USD)", unit: "$B", group: "scale" },
  { key: "enterprise_value_usd", label: "企业价值 EV (USD)", unit: "$B", group: "scale" },
  { key: "ttm_revenue_usd", label: "TTM 营收 (USD)", unit: "$B", group: "scale" },
  { key: "ttm_fcf_usd", label: "TTM 自由现金流 (USD)", unit: "$B", group: "scale" },

  { key: "ttm_revenue_yoy_pct", label: "TTM 营收同比", unit: "%", group: "growth" },
  { key: "latest_q_revenue_yoy_pct", label: "最新单季营收同比", unit: "%", group: "growth" },
  { key: "revenue_accel_pp", label: "增速边际变化 (单季 - TTM)", unit: "pp", group: "growth" },

  { key: "ttm_gross_margin_pct", label: "TTM 毛利率", unit: "%", group: "profit" },
  { key: "ttm_operating_margin_pct", label: "TTM 营业利润率", unit: "%", group: "profit" },
  { key: "ttm_net_margin_pct", label: "TTM 净利率", unit: "%", group: "profit" },
  { key: "ttm_roic_pct", label: "TTM ROIC", unit: "%", group: "profit" },

  { key: "ttm_fcf_margin_pct", label: "TTM FCF 利润率", unit: "%", group: "cash" },
  { key: "ocf_to_ni_ratio", label: "OCF / 净利润", unit: "x", group: "cash" },
  { key: "sbc_pct_of_fcf", label: "SBC / FCF", unit: "%", group: "cash" },

  { key: "pe_ttm", label: "P/E (TTM)", unit: "x", group: "valuation" },
  { key: "ev_to_sales_ttm", label: "EV / Sales", unit: "x", group: "valuation" },
  { key: "ev_to_ebit_ttm", label: "EV / EBIT", unit: "x", group: "valuation" },
  { key: "ev_to_fcf_ttm", label: "EV / FCF", unit: "x", group: "valuation" },
  { key: "ev_to_fcf_ex_sbc_ttm", label: "EV / FCF (扣 SBC)", unit: "x", group: "valuation" },
  { key: "reverse_dcf_implied_5y_fcf_cagr_pct", label: "Reverse-DCF 隐含 5Y CAGR", unit: "%", group: "valuation" },
];

const GROUP_TITLES: Record<MetricRowSpec["group"], string> = {
  scale: "规模与企业价值",
  growth: "增长动能",
  profit: "盈利能力与资本回报",
  cash: "现金流质量与稀释",
  valuation: "相对估值与隐含预期",
};

const QUALITY_BADGE: Record<QualityLight, { label: string; cls: string }> = {
  green: { label: "良好", cls: "peer-light peer-light-green" },
  yellow: { label: "留意", cls: "peer-light peer-light-yellow" },
  red: { label: "警示", cls: "peer-light peer-light-red" },
  not_applicable: { label: "金融豁免", cls: "peer-light peer-light-na" },
};

function formatMetricCell(raw: unknown, unit: MetricRowSpec["unit"]): string {
  if (raw === null || raw === undefined || raw === "") return "—";
  const num = Number(raw);
  if (!Number.isFinite(num)) return String(raw);
  if (unit === "$B") {
    const b = num / 1e9;
    return `$${b.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}B`;
  }
  if (unit === "%") return `${num.toFixed(2)}%`;
  if (unit === "pp") return `${num >= 0 ? "+" : ""}${num.toFixed(2)}pp`;
  if (unit === "x") return `${num.toFixed(2)}x`;
  return String(raw);
}

export function ComparePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialPrimary = (searchParams.get("primary") || "NVDA").trim().toUpperCase();
  const initialPeers = (searchParams.get("peers") || "")
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);

  const [primary, setPrimary] = useState<string>(initialPrimary);
  const [symbols, setSymbols] = useState<string[]>(
    initialPeers.length > 0 ? Array.from(new Set([initialPrimary, ...initialPeers])).slice(0, 6) : [initialPrimary, "AMD", "AVGO", "INTC", "TSM"],
  );
  const [tickerInput, setTickerInput] = useState("");
  const [groupNameInput, setGroupNameInput] = useState("");
  const [excludeNonComparable, setExcludeNonComparable] = useState(true);
  const [matrix, setMatrix] = useState<PeerComparisonMatrix | null>(null);

  const suggest = usePeerSuggest(primary);
  const compareMutation = usePeerCompare();
  const briefMutation = usePeerAttributionBrief();
  const createGroupMutation = useCreatePeerGroup();
  const deleteGroupMutation = useDeletePeerGroup();

  useEffect(() => {
    if (initialPeers.length > 0 && !matrix && !compareMutation.isPending) {
      compareMutation.mutate(
        {
          primary_symbol: initialPrimary,
          symbols: Array.from(new Set([initialPrimary, ...initialPeers])).slice(0, 6),
          exclude_non_comparable_from_median: excludeNonComparable,
        },
        { onSuccess: (data) => setMatrix(data) },
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const groupedRows = useMemo(() => {
    const groups: Array<{ group: MetricRowSpec["group"]; rows: MetricRowSpec[] }> = [];
    for (const g of ["scale", "growth", "profit", "cash", "valuation"] as const) {
      groups.push({ group: g, rows: METRIC_ROWS.filter((r) => r.group === g) });
    }
    return groups;
  }, []);

  function runCompare(nextPrimary = primary, nextSymbols = symbols, nextExclude = excludeNonComparable) {
    const clean = Array.from(new Set([nextPrimary, ...nextSymbols].map((s) => s.trim().toUpperCase()).filter(Boolean))).slice(0, 6);
    if (clean.length < 2) {
      showToast("请至少选择 2 家公司进行同业对比。", { tone: "error" });
      return;
    }
    setPrimary(clean[0]);
    setSymbols(clean);
    setSearchParams({ primary: clean[0], peers: clean.slice(1).join(",") }, { replace: true });
    briefMutation.reset();
    compareMutation.mutate(
      {
        primary_symbol: clean[0],
        symbols: clean,
        exclude_non_comparable_from_median: nextExclude,
      },
      {
        onSuccess: (data) => setMatrix(data),
        onError: (err) => showToast(`对比失败：${err.message}`, { tone: "error" }),
      },
    );
  }

  function handleAddTicker() {
    const clean = tickerInput.trim().toUpperCase();
    if (!clean) return;
    if (symbols.includes(clean)) {
      setTickerInput("");
      return;
    }
    if (symbols.length >= 6) {
      showToast("单次横评最多支持 6 家标的。", { tone: "error" });
      return;
    }
    setSymbols([...symbols, clean]);
    setTickerInput("");
  }

  function handleRemoveTicker(sym: string) {
    const next = symbols.filter((s) => s !== sym);
    setSymbols(next);
    if (sym === primary && next.length > 0) {
      setPrimary(next[0]);
    }
  }

  function handleSaveGroup() {
    const name = groupNameInput.trim();
    if (!name || symbols.length < 2) {
      showToast("请输入组名并确保至少有 2 个标的。", { tone: "error" });
      return;
    }
    createGroupMutation.mutate(
      { name, symbols },
      {
        onSuccess: () => {
          setGroupNameInput("");
          showToast(`已保存同业组「${name}」`);
        },
        onError: (err) => showToast(`保存失败：${err.message}`, { tone: "error" }),
      },
    );
  }

  return (
    <main className="peer-page">
      <header className="peer-header">
        <div>
          <h1 className="peer-title">同业对齐与相对估值</h1>
          <p className="peer-subtitle">
            0-LLM SEC XBRL 确定性财务矩阵 · 统一口径 Reverse-DCF（10% WACC / 3% 永续）· 可按需生成 AI 归因简评
          </p>
        </div>
        <div className="peer-header-actions">
          <button
            type="button"
            className="btn primary"
            disabled={compareMutation.isPending || symbols.length < 2}
            onClick={() => runCompare()}
          >
            {compareMutation.isPending ? "拉取并计算中…" : "开始同业对比 (0 LLM)"}
          </button>
        </div>
      </header>

      <section className="peer-controls-card">
        <div className="peer-chip-row">
          <span className="peer-label">当前标的（点击设为主标的）：</span>
          {symbols.map((sym) => (
            <span key={sym} className={`peer-chip${sym === primary ? " peer-chip-primary" : ""}`}>
              <button type="button" className="peer-chip-btn" onClick={() => setPrimary(sym)}>
                {sym === primary ? `★ ${sym}` : sym}
              </button>
              <button
                type="button"
                className="peer-chip-remove"
                aria-label={`移除 ${sym}`}
                onClick={() => handleRemoveTicker(sym)}
              >
                ×
              </button>
            </span>
          ))}
          <form
            className="peer-add-form"
            onSubmit={(e) => {
              e.preventDefault();
              handleAddTicker();
            }}
          >
            <input
              className="peer-input"
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value)}
              placeholder="添加美股代码如 MSFT"
              maxLength={12}
              aria-label="添加美股代码"
            />
            <button type="submit" className="btn">添加</button>
          </form>
        </div>

        <div className="peer-presets-row">
          <span className="peer-label">行业预设：</span>
          {(suggest.data?.curated_groups ?? []).slice(0, 8).map((g) => (
            <button
              key={g.group_id}
              type="button"
              className="peer-preset-btn"
              onClick={() => runCompare(g.symbols[0], g.symbols)}
            >
              {g.name}
            </button>
          ))}
        </div>

        {(suggest.data?.user_groups ?? []).length > 0 ? (
          <div className="peer-presets-row">
            <span className="peer-label">我的分组：</span>
            {suggest.data!.user_groups.map((ug) => (
              <span key={ug.group_id} className="peer-user-group">
                <button
                  type="button"
                  className="peer-preset-btn"
                  onClick={() => runCompare(ug.symbols[0], ug.symbols)}
                >
                  {ug.name} ({ug.symbols.join("/")})
                </button>
                <button
                  type="button"
                  className="peer-chip-remove"
                  aria-label={`删除 ${ug.name}`}
                  onClick={() => deleteGroupMutation.mutate(ug.group_id)}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : null}

        <div className="peer-options-row">
          <label className="peer-toggle">
            <input
              type="checkbox"
              checked={excludeNonComparable}
              onChange={(e) => {
                const nextVal = e.target.checked;
                setExcludeNonComparable(nextVal);
                if (matrix) runCompare(primary, symbols, nextVal);
              }}
            />
            <span>仅按可比标的（同 US-GAAP/USD 且非金融豁免）计算同业中位数</span>
          </label>
          <div className="peer-save-group">
            <input
              className="peer-input"
              value={groupNameInput}
              onChange={(e) => setGroupNameInput(e.target.value)}
              placeholder="保存为自定义同业组…"
              maxLength={60}
            />
            <button
              type="button"
              className="btn"
              disabled={createGroupMutation.isPending || !groupNameInput.trim()}
              onClick={handleSaveGroup}
            >
              保存分组
            </button>
          </div>
        </div>
      </section>

      {matrix ? (
        <>
          <section className="peer-summary-strip">
            <div className="peer-summary-main">
              <strong>{matrix.primary_symbol} 相对坐标：</strong>
              <span>{matrix.primary_coordinate_summary}</span>
            </div>
            <div className="peer-summary-meta">
              <span>耗时 {matrix.elapsed_ms}ms</span>
              <span>缓存命中 {matrix.cache_hits}/{matrix.cache_hits + matrix.cache_misses}</span>
              <button
                type="button"
                className="btn primary"
                disabled={briefMutation.isPending}
                onClick={() => briefMutation.mutate({ comparison_id: matrix.comparison_id })}
              >
                {briefMutation.isPending ? "生成 AI 归因中…" : "生成 AI 同业归因简评"}
              </button>
            </div>
          </section>

          {matrix.comparability_warnings.length > 0 ? (
            <section className="peer-warnings-box" aria-label="可比性告警">
              {matrix.comparability_warnings.map((w, idx) => (
                <div key={`${w.code}-${idx}`} className={`peer-warning peer-warning-${w.severity}`}>
                  <strong>[{w.code}]</strong> {w.message}
                </div>
              ))}
            </section>
          ) : null}

          {briefMutation.data ? (
            <section className="peer-brief-card">
              <div className="peer-brief-head">
                <h2>AI 同业归因简评</h2>
                <span className="peer-brief-badge">
                  引用数字已核验 {briefMutation.data.verified_metric_count}/{briefMutation.data.total_cited_metric_count}
                  {briefMutation.data.model_used ? ` · ${briefMutation.data.model_used}` : ""}
                  {briefMutation.data.tokens_used > 0 ? ` · ${briefMutation.data.tokens_used} tok` : ""}
                </span>
              </div>
              <p className="peer-brief-text">{briefMutation.data.summary}</p>
              <p className="peer-brief-text">{briefMutation.data.valuation_vs_fundamentals}</p>
              {briefMutation.data.relative_risks.length > 0 ? (
                <ul className="peer-brief-risks">
                  {briefMutation.data.relative_risks.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              ) : null}
            </section>
          ) : null}

          <section className="peer-table-wrap">
            <table className="peer-table">
              <thead>
                <tr>
                  <th className="peer-col-metric">指标 / 口径</th>
                  {matrix.companies.map((c) => (
                    <th key={c.symbol} className={c.symbol === matrix.primary_symbol ? "peer-col-primary" : ""}>
                      <div className="peer-th-sym">
                        <span>{c.symbol}</span>
                        <span className={QUALITY_BADGE[c.quality_light].cls}>
                          {QUALITY_BADGE[c.quality_light].label}
                        </span>
                      </div>
                      <div className="peer-th-sub">
                        {c.fiscal_period_label || c.period_basis} · {c.accounting_standard}/{c.currency}
                      </div>
                      <div className="peer-th-sub">
                        {c.price_usd ? `$${c.price_usd} (${c.price_as_of_date ?? ""})` : "无行情"}
                      </div>
                    </th>
                  ))}
                  <th className="peer-col-median">同业中位数</th>
                </tr>
              </thead>
              <tbody>
                {groupedRows.map(({ group, rows }) => (
                  <>
                    <tr key={`grp-${group}`} className="peer-group-row">
                      <td colSpan={matrix.companies.length + 2}>{GROUP_TITLES[group]}</td>
                    </tr>
                    {rows.map((spec) => {
                      const kStr = String(spec.key);
                      const medVal = matrix.peer_medians[kStr];
                      return (
                        <tr key={kStr}>
                          <td className="peer-col-metric">{spec.label}</td>
                          {matrix.companies.map((c) => {
                            const rawVal = c[spec.key];
                            const rk = c.ranks[kStr];
                            const vsMed = c.vs_median[kStr];
                            return (
                              <td
                                key={`${c.symbol}-${kStr}`}
                                className={c.symbol === matrix.primary_symbol ? "peer-col-primary" : ""}
                              >
                                <div className="peer-cell-val">{formatMetricCell(rawVal, spec.unit)}</div>
                                {rk !== undefined ? (
                                  <div className="peer-cell-meta">
                                    #{rk} {vsMed ? `(${vsMed})` : ""}
                                  </div>
                                ) : null}
                              </td>
                            );
                          })}
                          <td className="peer-col-median">{formatMetricCell(medVal, spec.unit)}</td>
                        </tr>
                      );
                    })}
                  </>
                ))}
              </tbody>
            </table>
          </section>
          <p className="peer-footnote">{matrix.reverse_dcf_assumptions}</p>
        </>
      ) : (
        <section className="peer-empty">
          <p>选择行业预设或输入 2–6 个美股代码，点击「开始同业对比 (0 LLM)」即可生成确定性财务与相对估值矩阵。</p>
        </section>
      )}
    </main>
  );
}
