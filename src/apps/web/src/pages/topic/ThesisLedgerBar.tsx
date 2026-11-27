import { useState } from "react";
import { Link } from "react-router-dom";
import {
  useRadarMutations,
  useRadarSubscriptions,
  useTopicThesisLedger,
  type ThesisClaim,
} from "../../api/radar";
import { describeError } from "../research/errors";
import { showToast } from "../../ui/toast";

export function ThesisLedgerBar({
  topicId,
  symbol,
  cik,
  companyName,
  hasLockedDossier,
}: {
  topicId: string;
  symbol?: string | null;
  cik?: string | null;
  companyName?: string | null;
  hasLockedDossier: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [draftEdits, setDraftEdits] = useState<ThesisClaim[] | null>(null);
  const ledger = useTopicThesisLedger(topicId);
  const subs = useRadarSubscriptions(Boolean(cik || symbol));
  const mutations = useRadarMutations();

  const confirmed = ledger.data?.latest_confirmed ?? null;
  const draft = ledger.data?.pending_draft ?? null;
  const activeClaims = draftEdits ?? draft?.claims ?? confirmed?.claims ?? [];

  const matchedSub = subs.data?.find(
    (s) => s.topic_id === topicId || (cik && s.cik === cik) || (symbol && s.symbol === symbol),
  );

  if (!hasLockedDossier && !confirmed && !draft) return null;

  const handleGenerateDraft = () => {
    mutations.generateLedgerDraft.mutate(topicId, {
      onSuccess: (d) => {
        setDraftEdits(d.claims);
        setExpanded(true);
        showToast(`已从锁定底稿提炼 ${d.claims.length} 条核心假设草案（0 Token）`);
      },
      onError: (err) => showToast(describeError(err), { tone: "error" }),
    });
  };

  const handleConfirm = () => {
    if (activeClaims.length === 0) return;
    mutations.confirmLedger.mutate(
      { topicId, claims: activeClaims, decision_summary: "用户确认核心假设台账" },
      {
        onSuccess: (saved) => {
          setDraftEdits(null);
          showToast(`已确认假设台账 v${saved.version}`);
        },
        onError: (err) => showToast(describeError(err), { tone: "error" }),
      },
    );
  };

  const handleToggleWatch = () => {
    if (matchedSub) {
      mutations.patchSubscription.mutate(
        { id: matchedSub.subscription_id, patch: { enabled: !matchedSub.enabled } },
        {
          onSuccess: (u) => showToast(u.enabled ? "已恢复 SEC 变化雷达监控" : "已暂停 SEC 变化雷达监控"),
          onError: (err) => showToast(describeError(err), { tone: "error" }),
        },
      );
      return;
    }
    if (!cik && !symbol) return;
    mutations.createSubscription.mutate(
      {
        topic_id: topicId,
        ...(cik != null && { cik }),
        ...(symbol != null && { symbol }),
        ...(companyName != null && { company_name: companyName }),
      },
      {
        onSuccess: () => showToast(`已为 ${symbol ?? cik} 开启 SEC 变化雷达监控`),
        onError: (err) => showToast(describeError(err), { tone: "error" }),
      },
    );
  };

  return (
    <section className="ledger-bar" aria-label="假设台账与持续跟踪">
      <div className="ledger-bar-row">
        <div className="ledger-bar-title">
          <strong>假设台账 & SEC 变化雷达</strong>
          {confirmed ? (
            <span className="ledger-ver-pill">已确认 v{confirmed.version} · {confirmed.claims.length} 条支柱</span>
          ) : draft ? (
            <span className="ledger-ver-pill draft">草案待确认</span>
          ) : (
            <span className="ledger-muted">将本轮核心假设与证伪阈值沉淀为版本化台账</span>
          )}
        </div>
        <div className="ledger-bar-actions">
          {!confirmed && !draft ? (
            <button
              type="button"
              className="inbox-btn primary"
              disabled={mutations.generateLedgerDraft.isPending}
              onClick={handleGenerateDraft}
            >
              {mutations.generateLedgerDraft.isPending ? "提炼中…" : "一键生成假设草案"}
            </button>
          ) : (
            <button type="button" className="inbox-btn secondary" onClick={() => setExpanded((v) => !v)}>
              {expanded ? "收起台账" : "查看 / 编辑假设"}
            </button>
          )}
          {cik || symbol ? (
            <button
              type="button"
              className={`inbox-btn ${matchedSub?.enabled ? "secondary" : "primary"}`}
              disabled={mutations.createSubscription.isPending || mutations.patchSubscription.isPending}
              onClick={handleToggleWatch}
            >
              {matchedSub?.enabled ? "雷达监控中" : "开启 SEC 雷达"}
            </button>
          ) : null}
          <Link className="inbox-link-btn" to="/inbox">
            变化收件箱 →
          </Link>
        </div>
      </div>

      {expanded && activeClaims.length > 0 ? (
        <div className="ledger-drawer">
          {activeClaims.map((claim, idx) => (
            <div key={claim.claim_id} className="ledger-claim-card">
              <div className="ledger-claim-meta">
                <span className="inbox-pillar-chip">{claim.pillar}</span>
                <span className="ledger-claim-status">{claim.stance} · {claim.status}</span>
              </div>
              <textarea
                className="ledger-textarea"
                aria-label={`假设 ${idx + 1}`}
                rows={2}
                value={claim.statement}
                onChange={(e) => {
                  const next = activeClaims.map((c, i) => (i === idx ? { ...c, statement: e.target.value } : c));
                  setDraftEdits(next);
                }}
              />
              <div className="ledger-invalidation">
                <strong>证伪条件：</strong>
                <span>{claim.invalidation_condition}</span>
              </div>
            </div>
          ))}
          <div className="ledger-drawer-foot">
            <button
              type="button"
              className="inbox-btn primary"
              disabled={mutations.confirmLedger.isPending}
              onClick={handleConfirm}
            >
              {mutations.confirmLedger.isPending
                ? "保存中…"
                : confirmed
                  ? `确认并升级至 v${confirmed.version + 1}`
                  : "确认假设台账 v1"}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
