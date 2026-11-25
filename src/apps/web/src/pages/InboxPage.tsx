import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  pollRadarSweepOperation,
  useChangeInbox,
  useRadarHealth,
  useRadarMutations,
  useRadarSubscriptions,
  type ChangeInboxItem,
} from "../api/radar";
import { describeError } from "./research/errors";
import { showToast } from "../ui/toast";

const IMPACT_LABELS: Record<string, string> = {
  critical: "重大风险 / 关键",
  high: "高影响",
  medium: "中等影响",
  low: "低影响",
};

const PILLAR_LABELS: Record<string, string> = {
  growth: "增长",
  margin: "利润率",
  cash_quality: "现金流质量",
  capital_allocation: "资本配置",
  valuation: "估值",
  risk: "风险治理",
};

const STATE_LABELS: Record<string, string> = {
  unread: "待复核",
  read: "已读",
  accepted: "已确认升级",
  dismissed_with_note: "已保留原判断",
};

function InboxCard({
  item,
  onDecide,
  busy,
}: {
  item: ChangeInboxItem;
  onDecide: (action: "accept" | "keep_original" | "mark_read", note?: string) => void;
  busy: boolean;
}) {
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState("");
  const [quotesOpen, setQuotesOpen] = useState(false);
  const quotes = item.signal_envelope?.verbatim_quotes ?? [];

  return (
    <article className={`inbox-card${item.review_state === "unread" ? " unread" : ""}`}>
      <div className="inbox-card-head">
        <span className={`inbox-impact impact-${item.impact_level}`}>
          {IMPACT_LABELS[item.impact_level] ?? item.impact_level}
        </span>
        <span className="inbox-channel">
          {item.source_channel === "v1_events"
            ? "SEC 重大事件"
            : item.source_channel === "peer_relative"
              ? "同业估值偏离"
              : "财报增量复核"}
        </span>
        {item.disclosed_at ? <span className="inbox-date">{item.disclosed_at}</span> : null}
        <span className="grow" />
        <span className={`inbox-state state-${item.review_state}`}>
          {STATE_LABELS[item.review_state] ?? item.review_state}
          {item.resulting_ledger_version ? ` · v${item.resulting_ledger_version}` : ""}
        </span>
      </div>

      <h3 className="inbox-headline">{item.headline}</h3>
      <p className="inbox-summary">{item.summary}</p>
      {item.source_channel === "peer_relative" && typeof item.scorecard_diff?.compare_url === "string" ? (
        <div className="inbox-quotes-wrap">
          <Link className="peer-coord-link" to={String(item.scorecard_diff.compare_url)}>
            打开同业估值矩阵 →
          </Link>
        </div>
      ) : null}

      {item.affected_pillars.length > 0 ? (
        <div className="inbox-pillars">
          {item.affected_pillars.map((p) => (
            <span key={p} className="inbox-pillar-chip">
              {PILLAR_LABELS[p] ?? p}
            </span>
          ))}
        </div>
      ) : null}

      {item.skeptic_dissent ? (
        <div className="skeptic-callout">
          <strong>反方保留意见（Skeptic）：</strong>
          <span>{item.skeptic_dissent}</span>
        </div>
      ) : null}

      {quotes.length > 0 ? (
        <div className="inbox-quotes-wrap">
          <button
            type="button"
            className="inbox-link-btn"
            onClick={() => setQuotesOpen((v) => !v)}
          >
            {quotesOpen ? "收起原文引文" : `查看原文引文（${quotes.length} 条）`}
          </button>
          {quotesOpen ? (
            <ul className="inbox-quotes-list">
              {quotes.map((q, idx) => (
                <li key={`${q.source_id}-${idx}`} className="inbox-quote-item">
                  <span className={`quote-verify ${q.verified ? "ok" : "warn"}`}>
                    {q.verified ? "原文已核验" : "待核验"}
                  </span>
                  <blockquote>“{q.quotation}”</blockquote>
                  {q.quote_context_warning ? (
                    <div className="quote-warn-note">语境提示：{q.quote_context_warning}</div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {item.user_decision_note ? (
        <div className="inbox-decision-note">我的复核备注：{item.user_decision_note}</div>
      ) : null}

      {noteOpen ? (
        <div className="inbox-note-form">
          <input
            type="text"
            className="inbox-input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="填写保留原判断的原因或修订备注（可选）"
          />
          <button
            type="button"
            className="inbox-btn secondary"
            disabled={busy}
            onClick={() => {
              onDecide("keep_original", note);
              setNoteOpen(false);
              setNote("");
            }}
          >
            确认保留原判断
          </button>
          <button type="button" className="inbox-btn ghost" onClick={() => setNoteOpen(false)}>
            取消
          </button>
        </div>
      ) : null}

      <div className="inbox-actions">
        {item.review_state !== "accepted" ? (
          <button
            type="button"
            className="inbox-btn primary"
            disabled={busy}
            onClick={() => onDecide("accept")}
          >
            {item.topic_id ? "接受并升级假设台账" : "确认知晓"}
          </button>
        ) : null}
        {item.review_state !== "dismissed_with_note" ? (
          <button
            type="button"
            className="inbox-btn secondary"
            disabled={busy}
            onClick={() => setNoteOpen((v) => !v)}
          >
            保留原判断（记异议）
          </button>
        ) : null}
        {item.review_state === "unread" ? (
          <button
            type="button"
            className="inbox-btn ghost"
            disabled={busy}
            onClick={() => onDecide("mark_read")}
          >
            标为已读
          </button>
        ) : null}
        <span className="grow" />
        {item.topic_id ? (
          <Link className="inbox-link-btn" to={`/research/${encodeURIComponent(item.topic_id)}`}>
            查看完整底稿 →
          </Link>
        ) : null}
      </div>
    </article>
  );
}

export function InboxPage() {
  const [stateFilter, setStateFilter] = useState("all");
  const [impactFilter, setImpactFilter] = useState("all");
  const [symbolFilter, setSymbolFilter] = useState("");
  const [newSymbol, setNewSymbol] = useState("");
  const [activeSweepOp, setActiveSweepOp] = useState<string | null>(null);

  const health = useRadarHealth();
  const subs = useRadarSubscriptions();
  const inbox = useChangeInbox({
    state: stateFilter,
    impact: impactFilter,
    symbol: symbolFilter,
  });
  const mutations = useRadarMutations();

  useEffect(() => {
    if (!activeSweepOp) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const res = await pollRadarSweepOperation(activeSweepOp);
        if (cancelled) return;
        if (res.operation.status === "completed") {
          setActiveSweepOp(null);
          const created = res.sweep?.inbox_items_created ?? 0;
          const zero = res.sweep?.l1_zero_change_count ?? 0;
          showToast(`巡检完成：新增 ${created} 条实质变化，${zero} 家公司无变化（0 Token）。`);
          void health.refetch();
          void subs.refetch();
          void inbox.refetch();
        } else if (res.operation.status === "failed") {
          setActiveSweepOp(null);
          showToast(`巡检失败：${res.error?.message ?? "请稍后重试"}`, { tone: "error" });
        }
      } catch {
        // transient poll error ignored until next tick
      }
    }, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeSweepOp, health, subs, inbox]);

  const handleAddSub = () => {
    const trimmed = newSymbol.trim().toUpperCase();
    if (!trimmed) return;
    const isCik = /^\d+$/.test(trimmed);
    mutations.createSubscription.mutate(
      isCik ? { cik: trimmed } : { symbol: trimmed },
      {
        onSuccess: (created) => {
          setNewSymbol("");
          showToast(`已开启 ${created.symbol ?? created.cik} 的 SEC 变化雷达监控`);
        },
        onError: (err) => showToast(describeError(err), { tone: "error" }),
      },
    );
  };

  const handleCheckNow = () => {
    mutations.checkNow.mutate(
      {},
      {
        onSuccess: (op) => {
          setActiveSweepOp(op.operation_id);
          showToast("已启动 P1-B 错峰巡检（先零成本快扫 L1，仅实质变化进 L2 优先队列）");
        },
        onError: (err) => showToast(describeError(err), { tone: "error" }),
      },
    );
  };

  const hasErrors = (health.data?.error_count ?? 0) > 0 || (health.data?.degraded_count ?? 0) > 0;

  return (
    <main className="inbox-page">
      <header className="inbox-header">
        <div>
          <h1 className="inbox-title">变化收件箱 & SEC 雷达</h1>
          <p className="inbox-subtitle">
            30 秒审阅实质 SEC 事件与财报增量变化；无变化保持安静，监控故障独立告警。
          </p>
        </div>
        <div className="inbox-header-actions">
          <button
            type="button"
            className="inbox-btn primary"
            disabled={mutations.checkNow.isPending || Boolean(activeSweepOp)}
            onClick={handleCheckNow}
          >
            {activeSweepOp ? "正在错峰巡检…" : "立即巡检 SEC"}
          </button>
        </div>
      </header>

      <section className={`radar-health-bar${hasErrors ? " has-warning" : ""}`} aria-label="监控健康状态">
        <div className="radar-health-stats">
          <span>正在跟踪 <strong>{health.data?.enabled_subscriptions ?? 0}</strong> 家公司</span>
          <span>正常 {health.data?.healthy_count ?? 0}</span>
          {(health.data?.degraded_count ?? 0) > 0 ? (
            <span className="health-warn">部分降级 {health.data?.degraded_count}</span>
          ) : null}
          {(health.data?.error_count ?? 0) > 0 ? (
            <span className="health-err">监控故障 {health.data?.error_count}</span>
          ) : null}
          <span>待复核 <strong>{health.data?.unread_inbox_count ?? 0}</strong></span>
        </div>
        <div className="radar-add-form">
          <input
            type="text"
            className="inbox-input"
            value={newSymbol}
            onChange={(e) => setNewSymbol(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") handleAddSub(); }}
            placeholder="输入美股代码或 CIK（如 NVDA）"
            aria-label="添加监控公司"
          />
          <button
            type="button"
            className="inbox-btn secondary"
            disabled={mutations.createSubscription.isPending || !newSymbol.trim()}
            onClick={handleAddSub}
          >
            添加监控
          </button>
        </div>
      </section>

      {(subs.data?.length ?? 0) > 0 ? (
        <section className="radar-subs-strip" aria-label="已订阅公司">
          {subs.data!.map((s) => (
            <div key={s.subscription_id} className={`radar-sub-pill health-${s.health_status}`}>
              <span>{s.symbol ?? s.cik}</span>
              {s.health_status !== "healthy" ? <span className="pill-err" title={s.last_error ?? ""}>!</span> : null}
              <button
                type="button"
                className="pill-toggle"
                onClick={() =>
                  mutations.patchSubscription.mutate({ id: s.subscription_id, patch: { enabled: !s.enabled } })
                }
                title={s.enabled ? "暂停监控" : "恢复监控"}
              >
                {s.enabled ? "监控中" : "已暂停"}
              </button>
              <button
                type="button"
                className="pill-del"
                aria-label={`移除 ${s.symbol ?? s.cik}`}
                onClick={() => mutations.deleteSubscription.mutate(s.subscription_id)}
              >
                ×
              </button>
            </div>
          ))}
        </section>
      ) : null}

      <section className="inbox-filters" aria-label="筛选条件">
        <select
          className="inbox-select"
          aria-label="复核状态"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
        >
          <option value="all">全部状态</option>
          <option value="unread">仅待复核</option>
          <option value="accepted">已确认升级</option>
          <option value="dismissed_with_note">已保留原判断</option>
          <option value="read">已读</option>
        </select>
        <select
          className="inbox-select"
          aria-label="影响级别"
          value={impactFilter}
          onChange={(e) => setImpactFilter(e.target.value)}
        >
          <option value="all">全部影响级别</option>
          <option value="critical">关键 / 重大风险</option>
          <option value="high">高影响</option>
          <option value="medium">中等影响</option>
          <option value="low">低影响</option>
        </select>
        <input
          type="search"
          className="inbox-input"
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
          placeholder="按代码筛选（如 SMCI）"
          aria-label="按代码筛选"
        />
      </section>

      <section className="inbox-list" aria-label="变化条目">
        {inbox.isLoading ? <div className="inbox-empty">正在加载变化收件箱…</div> : null}
        {inbox.isError ? <div className="inbox-empty">收件箱加载失败，请稍后重试。</div> : null}
        {inbox.data && inbox.data.items.length === 0 ? (
          <div className="inbox-empty">
            暂无符合条件的实质变化。开启公司雷达监控后，仅重大 SEC 事件与财报增量变化会进入此处。
          </div>
        ) : null}
        {inbox.data?.items.map((item) => (
          <InboxCard
            key={item.inbox_item_id}
            item={item}
            busy={mutations.decideInboxItem.isPending}
            onDecide={(action, user_note) =>
              mutations.decideInboxItem.mutate(
                { inboxItemId: item.inbox_item_id, action, user_note },
                {
                  onSuccess: (res) => {
                    showToast(
                      res.new_ledger
                        ? `已确认并将假设台账升级至 v${res.new_ledger.version}`
                        : "已更新复核状态",
                    );
                  },
                  onError: (err) => showToast(describeError(err), { tone: "error" }),
                },
              )
            }
          />
        ))}
      </section>
    </main>
  );
}
