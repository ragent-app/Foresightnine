import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { AppShell, Disclosure } from "./components/AppShell";
import { Button, StatusDot, Tag } from "./components/Controls";
import { ThesisInput } from "./components/ThesisInput";
import { ChallengePanel } from "./components/ChallengePanel";
import { WorldlinePanel } from "./components/WorldlinePanel";
import { FmPanel } from "./components/FmPanel";
import { PortfolioPanel } from "./components/PortfolioPanel";
import { NavPanel } from "./components/NavPanel";
import { MethodsPanel } from "./components/MethodsPanel";
import { evidenceHash, fmInput, isEvidenceIndependentlyReviewable, makeBranches } from "./fixtures";
import { allocationRows, initialState, reducer } from "./state";
import { downloadText, csvExport, navExport, researchExport } from "./exports";
import { currencySymbol, parseCapital, validateSearchEvidence } from "./validation";
import { runFmInWorker } from "./fm-client";
import type { Currency, EvidenceDraft, EvidenceView, Stage } from "./types";

const DRAFT_KEY = "foresightnine.web.draft.v1";

export function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const challengeTimer = useRef<number | null>(null);
  const fmAbort = useRef<AbortController | null>(null);
  const capital = parseCapital(state.capital);
  const rows = useMemo(() => allocationRows(state.fmResult, capital, state.currency, state.portfolioMode), [state.fmResult, capital, state.currency, state.portfolioMode]);
  const viewpointBranches = useMemo(() => makeBranches(state.viewpoint), [state.viewpoint]);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(DRAFT_KEY);
      if (saved) {
        const draft = JSON.parse(saved) as { viewpoint?: unknown; capital?: unknown; currency?: unknown; updatedAt?: unknown };
        if (typeof draft.viewpoint === "string" && typeof draft.capital === "string" && (draft.currency === "CNY" || draft.currency === "USD" || draft.currency === "EUR")) dispatch({ type: "restore_draft", draft: { viewpoint: draft.viewpoint, capital: draft.capital, currency: draft.currency as Currency, updatedAt: typeof draft.updatedAt === "string" ? draft.updatedAt : new Date().toISOString() } });
      }
    } catch { /* local draft is optional */ }
  }, []);

  useEffect(() => {
    try { window.localStorage.setItem(DRAFT_KEY, JSON.stringify({ viewpoint: state.viewpoint, capital: state.capital, currency: state.currency, updatedAt: new Date().toISOString() })); } catch { /* offline storage is optional */ }
  }, [state.viewpoint, state.capital, state.currency]);

  useEffect(() => () => { if (challengeTimer.current !== null) window.clearTimeout(challengeTimer.current); fmAbort.current?.abort(); }, []);

  const startChallenge = useCallback(() => {
    dispatch({ type: "start_challenge" });
    challengeTimer.current = window.setTimeout(() => dispatch({ type: "challenge_complete" }), 900);
  }, []);
  const cancelChallenge = useCallback(() => { if (challengeTimer.current !== null) window.clearTimeout(challengeTimer.current); dispatch({ type: "challenge_cancel" }); }, []);
  const retryChallenge = useCallback(() => { dispatch({ type: "challenge_recover" }); challengeTimer.current = window.setTimeout(() => dispatch({ type: "challenge_complete" }), 700); }, []);
  const recoverChallenge = useCallback(() => { retryChallenge(); }, [retryChallenge]);
  const submitReply = useCallback((body: string) => { dispatch({ type: "submit_reply", body }); }, []);
  const addEvidence = useCallback((draft: EvidenceDraft) => {
    if (Object.keys(validateSearchEvidence(draft)).length > 0) return;
    const retrievedAt = "2026-08-10T00:00:00Z";
    const sourceType: EvidenceView["source_type"] = draft.grade === "A" ? "primary" : draft.grade === "B" ? "reliable_secondary" : draft.grade === "C" ? "commentary" : draft.grade === "D" ? "user_private" : "model_inference";
    const evidenceId = `evidence-user-${state.evidence.length + 1}`;
    const challengeId = "challenge-web-demo";
    const url = draft.url.trim();
    const title = draft.title.trim();
    const publisher = draft.publisher.trim();
    const publishedAt = draft.published_at.trim();
    const excerpt = draft.excerpt.trim();
    const userNote = draft.user_note.trim();
    const independentlyReviewable = isEvidenceIndependentlyReviewable(draft.grade, sourceType, url);
    const evidence: EvidenceView = {
      content_hash: evidenceHash({ evidence_id: evidenceId, challenge_id: challengeId, source_type: sourceType, url, title, publisher, published_at: publishedAt, retrieved_at: retrievedAt, excerpt, user_note: userNote, grade: draft.grade, supports_or_challenges: draft.supports_or_challenges, independently_reviewable: independentlyReviewable }),
      evidence_id: evidenceId,
      challenge_id: challengeId,
      source_type: sourceType,
      url,
      title,
      publisher,
      published_at: publishedAt,
      retrieved_at: retrievedAt,
      excerpt,
      user_note: userNote,
      grade: draft.grade,
      supports_or_challenges: draft.supports_or_challenges,
      independently_reviewable: independentlyReviewable,
      sourceLabel: publisher,
    };
    dispatch({ type: "add_evidence", evidence });
  }, [state.evidence.length]);

  const computeFm = useCallback(() => {
    fmAbort.current?.abort();
    const controller = new AbortController(); fmAbort.current = controller;
    dispatch({ type: "fm_start" });
    void runFmInWorker(fmInput(), { signal: controller.signal, onProgress: (progress, step) => dispatch({ type: "fm_progress", progress, step }) }).then((result) => { if (!controller.signal.aborted) dispatch({ type: "fm_complete", result }); }).catch(() => { if (!controller.signal.aborted) dispatch({ type: "fm_fail" }); });
  }, []);
  const cancelFm = useCallback(() => { fmAbort.current?.abort(); dispatch({ type: "fm_cancel" }); }, []);

  const exportJson = useCallback(() => { downloadText("foresightnine-research.json", JSON.stringify(researchExport(state, viewpointBranches, rows), null, 2), "application/json;charset=utf-8"); dispatch({ type: "export_notice", notice: "JSON 已导出；当前观点、挑战回复和证据均保留。" }); }, [rows, state, viewpointBranches]);
  const exportCsv = useCallback(() => { downloadText("foresightnine-allocation.csv", csvExport(rows), "text/csv;charset=utf-8"); dispatch({ type: "export_notice", notice: "CSV 已导出；这是 allocation-only 快照，完整审计字段请使用 JSON。" }); }, [rows]);
  const exportPath = useCallback(() => { downloadText("foresightnine-worldline-nav.json", JSON.stringify(navExport(state, viewpointBranches), null, 2), "application/json;charset=utf-8"); dispatch({ type: "export_notice", notice: "路径数据已导出，并标记了当前 premise、完整 NAV 数组与历史 / 模拟边界。" }); }, [state, viewpointBranches]);
  const methodsExport = useCallback(() => { downloadText("foresightnine-methods.txt", "ForesightNine 方法说明\n\n概率是可修订判断，不是事实。\n情景模拟，不是预测或收益承诺。\n", "text/plain;charset=utf-8"); dispatch({ type: "export_notice", notice: "方法说明已导出。" }); }, []);

  const content = renderStage(state.stage, state, dispatch, { startChallenge, cancelChallenge, retryChallenge, recoverChallenge, addEvidence, submitReply, computeFm, cancelFm, exportJson, exportCsv, exportPath, methodsExport, rows, viewpointBranches });
  const inspector = renderInspector(state.stage, state, rows, () => dispatch({ type: "toggle_drawdown" }));
  return <AppShell stage={state.stage} onStage={(stage: Stage) => dispatch({ type: "set_stage", stage })} inspector={inspector}><div className="top-status"><span><StatusDot tone="sage" /> 本地草稿已启用</span><span>数据截止 2026-08-09</span>{state.exportNotice ? <span className="export-notice" role="status">{state.exportNotice}</span> : null}</div>{content}</AppShell>;
}

interface Handlers { readonly startChallenge: () => void; readonly cancelChallenge: () => void; readonly retryChallenge: () => void; readonly recoverChallenge: () => void; readonly addEvidence: (draft: EvidenceDraft) => void; readonly submitReply: (body: string) => void; readonly computeFm: () => void; readonly cancelFm: () => void; readonly exportJson: () => void; readonly exportCsv: () => void; readonly exportPath: () => void; readonly methodsExport: () => void; readonly rows: ReturnType<typeof allocationRows>; readonly viewpointBranches: ReturnType<typeof makeBranches>; }

function renderStage(stage: Stage, state: ReturnType<typeof reducer>, dispatch: React.Dispatch<Parameters<typeof reducer>[1]>, handlers: Handlers) {
  switch (stage) {
    case "thesis": return <ThesisInput viewpoint={state.viewpoint} capital={state.capital} currency={state.currency} draftRestored={state.draftRestored} onViewpoint={(viewpoint) => dispatch({ type: "set_viewpoint", viewpoint })} onCapital={(capital) => dispatch({ type: "set_capital", capital })} onCurrency={(currency) => dispatch({ type: "set_currency", currency: currency as Currency })} onStart={handlers.startChallenge} onReset={() => dispatch({ type: "reset" })} />;
    case "challenge": return <ChallengePanel status={state.challengeStatus} turns={state.challengeTurns} replyDraft={state.replyDraft} evidence={state.evidence} rubric={state.rubric} onStart={handlers.startChallenge} onCancel={handlers.cancelChallenge} onRetry={handlers.retryChallenge} onRecover={handlers.recoverChallenge} onReplyDraft={(draft) => dispatch({ type: "set_reply_draft", draft })} onSubmitReply={handlers.submitReply} onEvidence={handlers.addEvidence} onConfirm={(confirmation) => dispatch({ type: "confirm", confirmation })} />;
    case "worldline": return <WorldlinePanel branches={handlers.viewpointBranches} selected={state.selectedBranch} onSelect={(branch) => dispatch({ type: "select_branch", branch })} onCompute={handlers.computeFm} />;
    case "fm": return <FmPanel status={state.fmStatus} progress={state.fmProgress} step={state.fmStep} resultReady={Boolean(state.fmResult)} onCompute={handlers.computeFm} onCancel={handlers.cancelFm} onPortfolio={() => dispatch({ type: "set_stage", stage: "portfolio" })} />;
    case "portfolio": return <PortfolioPanel mode={state.portfolioMode} currency={state.currency} rows={handlers.rows} capital={parseCapital(state.capital)} selectedBranch={state.selectedBranch} branches={handlers.viewpointBranches} stress={state.stressScenario} onMode={(mode) => dispatch({ type: "set_mode", mode })} onStress={(scenario) => dispatch({ type: "set_stress", scenario })} onCompute={handlers.computeFm} onNav={() => dispatch({ type: "set_stage", stage: "nav" })} onExport={(kind) => kind === "json" ? handlers.exportJson() : handlers.exportCsv()} fmReady={Boolean(state.fmResult)} />;
    case "nav": return <NavPanel branches={handlers.viewpointBranches} selected={state.selectedBranch} onSelect={(branch) => dispatch({ type: "select_branch", branch })} stress={state.stressScenario} onExport={handlers.exportPath} drawdownVisible={state.drawdownVisible} onToggleDrawdown={() => dispatch({ type: "toggle_drawdown" })} />;
    case "methods": return <MethodsPanel onExport={handlers.methodsExport} />;
  }
}

function renderInspector(stage: Stage, state: ReturnType<typeof reducer>, rows: ReturnType<typeof allocationRows>, onToggleDrawdown: () => void) {
  if (stage === "challenge") return <div className="inspector-content"><h2>证据工作台</h2><p>来源等级表示来源与可核验性，不代表结论正确。</p>{state.evidence.map((item) => <div className="inspector-row" key={item.evidence_id}><Tag tone={item.grade === "E" ? "warning" : "strong"}>{item.grade}</Tag><span>{item.title}</span></div>)}<Disclosure compact /></div>;
  if (stage === "worldline") return <div className="inspector-content"><h2>概率依据</h2>{["基准率 · 中性起点", "支持证据 · 向上", "反对证据 · 向下", "专家约束 · 排除不可能路径", "模型不确定性 · 区间加宽"].map((item) => <p className="inspector-line" key={item}>{item}</p>)}<div className="inspector-callout">方向表示判断影响，不是可加总百分点。</div></div>;
  if (stage === "fm") return <div className="inspector-content"><h2>可复现性</h2>{["FM-v1", "决策时点 2026-08-09", "Python golden parity", "无未来数据", "Web Worker"].map((item) => <p className="check-line" key={item}><StatusDot tone="sage" />{item}</p>)}<hr /><h3>数据快照</h3><p>签名哈希由本地计算生成，原始市场分片不会上传。</p></div>;
  if (stage === "portfolio") return <div className="inspector-content"><h2>配置说明</h2>{state.fmResult ? <><p>当前组合基于 FM 方法论，在世界线框架下平衡收益贡献与风险暴露。</p><hr /><h3>世界线暴露</h3><p>当前选择：{state.selectedBranch === "main" ? "主情景 W₀" : state.selectedBranch === "weak" ? "弱情景 W−" : "强情景 W+"}</p><p>残余现金：{currencySymbol(state.currency)}{Math.max(0, parseCapital(state.capital) - rows.reduce((sum, row) => sum + row.amount, 0)).toLocaleString(state.currency === "USD" ? "en-US" : state.currency === "EUR" ? "de-DE" : "zh-CN")}</p></> : <p role="status">FM 尚未计算；暂无配置权重、金额、单位或残余现金结果。</p>}<Disclosure /></div>;
  if (stage === "nav") return <div className="inspector-content"><h2>路径假设</h2>{["世界线版本 WL-v1", "波动率窗口 252 日", "噪声种子 已固定", "价格口径 复权收盘", "数据截止 2026-08-09"].map((item) => <p className="inspector-line" key={item}>{item}</p>)}<Button variant="secondary" onClick={onToggleDrawdown}>{state.drawdownVisible ? "隐藏回撤" : "显示回撤"}</Button><Disclosure compact /></div>;
  return <div className="inspector-content"><h2>研究档案</h2><p>公开研究模式保留从观点、证据到组合路径的审计线索。</p><div className="archive-status"><StatusDot tone="sage" /> 本地保存</div><div className="archive-status"><StatusDot tone="blue" /> {state.evidence.length} 条证据</div><Disclosure /></div>;
}
