import { useEffect, useMemo, useRef, useState } from "react";
import { MAX_RESEARCH_PDF_BYTES, researchClient, sourceDiscoveryQuery, searchCandidateDate, ResearchRequestError, researchPollingDelay, type DiscoveryCandidate, type ResearchClaim, type ResearchFact, type ResearchOperation, type ResearchTopic, type SourceMetadata, type RunBudget, type RunInputSnapshot, type IssuerCandidate } from "../general-research-client";
import type { Language } from "../types";
import { Button, Field, Input, Select, Textarea } from "./Controls";
import { Header } from "./AppShell";
import { SourceContext } from "./SourceContext";
import { formatResearchTextReport } from "../research-text-report";
import { CitationEditor } from "./CitationEditor";
import { canUseCitationSource } from "../source-text-search";

interface Props { baseUrl: string | null; accountId: string | null; language: Language }

function localizeLimitation(value: string, en: boolean): string {
  if (value === "Search candidates are unverified leads, not evidence; only an explicitly authorized retrieved source can be analyzed.")
    return en ? value : "搜索结果只是待核验线索，只有经授权获取的资料才能用于分析。";
  if (value === "The official page was retrieved with user-granted model permission; its content and dates remain unverified.")
    return en ? value : "已按授权获取官方网页供模型处理；内容与日期仍未经核验。";
  if (value === "The official source could not be retrieved; the search result remains an unverified lead.")
    return en ? value : "无法获取官方资料，搜索结果仍只是待核验线索。";
  if (value === "No counterarguments were provided; this does not establish that none exist.")
    return en ? "The last model run provided no counterarguments; this does not establish that none exist." : "上次模型运行未提供反方材料；这不代表不存在反方意见。";
  if (value === "The research is partial: supported claims or counterarguments are incomplete.")
    return en ? "The last research run was partial: supported findings or counterarguments were incomplete." : "上次研究运行尚未完成：有依据的结论或反方材料不足。";
  if (en) return value;
  const modelPrefix = "Model-reported limitation (not independently verified): ";
  if (value.startsWith(modelPrefix)) return `模型报告（尚未独立核实）：${value.slice(modelPrefix.length)}`;
  const fixed: Record<string, string> = {
    "No permitted, time-eligible source was available for model analysis.": "没有可供模型分析且符合时间要求的授权资料。",
    "The requested focus exceeds the input budget; no model call was made.": "所选引文超出输入预算，未调用模型。",
    "The requested focus is unavailable; no model call was made.": "所选引文不可用，未调用模型。",
    "Matching a quotation does not independently establish that it supports a claim.": "原文匹配不代表已独立核实该资料支持结论。",
    "A joined quotation was split into separately located passages; support requires review.": "拼接的引文已拆为分别定位的原文；是否支持结论仍需核查。",
    "A second model assessment is not independent proof that evidence supports a claim.": "模型二次判断不能独立证明证据支持结论。",
    "A claim percentage may differ from a deterministic calculation; review the wording and calculation.": "结论中的百分比可能与确定性计算不一致，请核对表述和计算。",
    "A claim amount may not match the cited numeric facts after unit conversion; review the source and wording.": "结论金额换算后可能与所引数值不一致，请核对来源与表述。",
    "A claimed direction or contradiction may require review; check revision versus change, definitions, signs and periods.": "变化方向或反证关系需要核查，请区分修订与实际变化，并核对定义、正负号和期间。",
    "A claimed component direction may conflict with an accounting identity; check definitions, signs and periods.": "结论所述分项变化方向可能与会计关系冲突，请核对定义、正负号及期间。",
    "A claimed trade-deficit direction may conflict with the goods-deficit minus services-surplus identity; check definitions, signs and periods.": "结论所述贸易逆差变化方向可能与商品逆差减服务顺差的关系冲突，请核对定义、正负号及期间。",
    "An explicit quarter and year could not be matched in the attached quotations; check the period and source context.": "所附引文未匹配结论中的季度与年份，请核对期间和原文上下文。",
    "A dollar-symbol amount could not be matched in the attached quotations; check currency, units, source and any calculation.": "所附引文未匹配带 $ 符号的金额，请核对币种、单位、来源及可能的计算。",
    "An explicit USD amount could not be confirmed from the attached quotations; check currency, units, source and any calculation.": "无法从所附引文确认结论中的美元金额，请核对币种、单位、来源与可能的计算。",
    "A matched amount fact passage is not fully covered by attached quotations; check evidence and context.": "所附引文未完整覆盖匹配的金额事实片段，请核对证据和上下文。",
    "A cited source contains a matching fact outside the attached quotations; review the candidate and its meaning.": "已引用的来源中有匹配事实，但所附引文未覆盖其原文；请核对候选资料及其含义。",
    "A quotation may omit a relevant subject or table header; review the source context and claim.": "引文可能遗漏相关指标或表头，请核对原文上下文及结论。",
    "A quotation may omit relevant context; review the source and claim.": "引文可能遗漏相关上下文，请核对原文及结论。",
    "A table fact's declared year conflicts with its apparent column; it was excluded from automatic calculations pending review.": "事实声明的年度与表格中的对应列可能不一致，核查前未用于自动计算。",
    "Table column alignment is not established; verify each value, period and unit against the source before relying on calculations.": "表格列对齐尚未核实；使用计算结果前请对照原文核对数值、期间和单位。",
    "A table fact's currency identifier is absent from its quotation; verify the source and currency before relying on the calculation.": "表格事实的引文未标明币种；使用计算结果前请核对原文及币种。",
    "A fact's currency conflicts with an explicit currency cue on the same quoted value; it was excluded from automatic calculations pending review.": "事实的币种与引文中同一数值旁的明确币种不一致；核查前不参与自动计算。",
    "A model-reported evidence gap is an unverified limitation, not counterevidence.": "模型声称资料存在缺口仅是未经核实的局限说明，不属于反方证据。",
    "A model-reported absence of counterarguments is an unverified limitation, not counterevidence.": "模型声称未发现反方仅是未经核实的局限说明，不属于反方证据。",
    "A claimed publication date may differ from the source's declared date; review both.": "结论中的发布日期可能与来源声明的日期不一致，请核对两者。",
    "At most 50 amount review candidates are shown; additional candidates may exist.": "最多展示 50 条待核查金额，可能还有其他差异。",
    "A claim percentage-point figure is absent from its quotations; check the source and any calculation.": "结论中的百分点数值未出现在引文中，请核对来源与可能的计算。",
    "A claim's basis-point figure is absent from its quotations; check the source and any calculation.": "结论中的基点数值未出现在引文中，请核对来源与可能的计算。",
    "A model quotation omitted table markup or empty cells; the full original row was restored and requires column review.": "模型引文省略了表格标记或空单元格；已恢复完整原始行，需核对列对齐。",
    "A table-normalized fact was omitted until column alignment is verified.": "表格事实在列对齐核实前已省略。",
    "Model-generated date or unit observations were retained as unverified claims, not numeric facts.": "模型生成的日期或单位声明作为待核验结论保留，不作为数值事实。",
    "Some model-generated text observations were omitted because the claim limit was reached.": "部分模型文字观察因结论数量上限被省略。",
    "A model-generated quotation did not match the supplied source and was omitted.": "模型生成的一条引文未匹配提供的资料，已省略。",
    "A model-added JSON closing brace was removed from a quotation; check the original context.": "已从引文中移除模型添加的 JSON 结束括号，请核对原文上下文。",
    "A model-generated claim without a valid quotation was omitted.": "一条引用无效的模型结论已省略。",
    "Period comparability is not established: different year, quarter or month labels may refer to stock or flow figures.": "期间是否可比尚未确认：年、季度或月份标签不同，需核对指标是存量还是期间流量。",
    "A model-generated fact had an invalid source or ambiguous identifier and was omitted.": "模型生成的一项事实来源无效或标识不唯一，已省略。",
    "A model-generated fact had an unlocatable quotation and was omitted.": "模型生成的一项事实引文无法定位，已省略。",
    "A model-generated fact value was absent from its quotation and was omitted.": "模型生成的一项事实数值未出现在引文中，已省略。",
    "A model-generated fact quotation cuts through a word or number and was omitted.": "模型事实的引文截断了单词或数字，已省略。",
    "A numeric fact has an ambiguous quotation location and was excluded from automatic calculations.": "数值的引文存在多个可能位置，未用于自动计算。",
    "A model-generated quotation cuts through a word or number; review the surrounding source.": "引文截断了单词或数字，请查看原文上下文。",
    "During generation, a percentage fact included a model-declared currency; it was excluded from automatic calculations pending review.": "生成时，一项百分比事实带有模型声明的币种，核查前未用于自动计算。",
    "The requested model support review did not cover all claims.": "所请求的模型证据复查未覆盖全部结论。",
    "Generation is saved; support review is pending or was interrupted.": "生成结果已保存；证据复查尚未完成或已中断。",
    "Cross-source numeric differences require entity and accounting-basis review; they are not confirmed conflicts.": "跨来源数值差异需要核对研究对象和统计口径，不能直接认定为冲突。",
    "At most 50 candidate differences are shown.": "最多展示 50 组待核查差异。",
    "Source discovery is unavailable; attach source material manually.": "来源搜索暂不可用，请手动添加资料。",
    "Source discovery failed or was rate limited; attach source material manually.": "来源搜索失败或受到限流，请手动添加资料。",
    "Search candidates are unverified leads, not evidence. Attach and authorize material before analysis.": "搜索结果是待核验线索，添加并授权资料后才能分析。",
    "Some search candidates could not be validated and were omitted.": "部分搜索线索无法验证，已省略。",
    "No eligible search candidates remained after validation.": "校验后没有可用的搜索线索。",
    "Historical search publication dates are not independently verified.": "历史搜索结果的公开日期尚未独立核验。",
  };
  if (fixed[value]) return fixed[value];
  const omitted = value.match(/^(\d+) model-generated numeric fact\(s\) were omitted because their format was invalid\.$/);
  if (omitted) return `模型生成的 ${omitted[1]} 条数值事实因格式无效而被省略。`;
  const spacing = value.match(/^Source (.+): PDF text may have missing word spacing; check the original and consider another extraction order\.$/);
  if (spacing) return `资料 ${spacing[1]} 的 PDF 提取文本可能缺少词间空格，请核对原文件并考虑使用其他读取方式。`;
  const simulated = value.match(/^Source (.+): simulated material is excluded from real research\.$/);
  if (simulated) return `资料 ${simulated[1]} 标为模拟，未用于真实研究。`;
  const prefixes: Record<string, string> = {
    "Research analysis could not be completed: provider_budget_unknown": "无法完成分析：尚未配置供应商价格，无法保证不超过费用上限。",
    "Research analysis could not be completed: provider_budget_exhausted": "无法完成分析：调用预算已用完。",
    "Research analysis could not be completed: provider_unavailable": "无法完成分析：尚无可用的研究模型。",
  };
  return prefixes[value] ?? value;
}

function reviewOmissionHint(run: Record<string, unknown>, index: number, en: boolean): string | null {
  const entries = run.support_review_omissions;
  if (!Array.isArray(entries)) return null;
  const item = entries.find((value): value is { claim_index: number; reason: string; required_subject_context?: boolean } =>
    typeof value === "object" && value !== null && value.claim_index === index && typeof value.reason === "string");
  if (!item) return null;
  if (item.reason === "model_omitted") return en ? "The model did not return an assessment for this submitted finding." : "该结论已提交复查，但模型未返回判断。";
  if (item.reason === "not_reviewable") return en ? "The citations or evidence did not meet the review input requirements." : "引文或证据未满足模型复查的输入要求。";
  if (item.reason === "input_budget") return item.required_subject_context
    ? (en ? "The quotation and required context did not fit the review input budget." : "完整引文及所需上下文超出复查输入预算。")
    : (en ? "The complete quotation did not fit the review input budget." : "完整引文超出复查输入预算。");
  if (item.reason === "call_budget") return en ? "The model-call budget did not allow the review." : "模型调用次数预算不足，未进行复查。";
  if (item.reason === "token_budget") return en ? "The token budget did not allow the review." : "Token 预算不足，未进行复查。";
  if (item.reason === "time_budget") return en ? "The time budget did not allow the review." : "时间预算不足，未进行复查。";
  if (item.reason === "review_failed") return en ? "The review for this finding failed." : "该结论的复查失败。";
  if (item.reason === "review_failure") return en ? "This review was not started after an earlier review failed." : "之前的复查失败，此项未开始。";
  if (item.reason === "not_started") return en ? "This review has not started." : "此项复查尚未开始。";
  if (item.reason === "in_progress") return en ? "This review was in progress; its outcome is unknown." : "此项复查进行中断，结果未知。";
  return null;
}

function statusLabel(status: string, en: boolean): string {
  if (en) return status;
  return ({ pending: "等待中", running: "进行中", completed: "已结束", failed: "失败", cancelled: "已取消", partial: "部分完成" } as Record<string, string>)[status] ?? status;
}

function factSourceWarning(value: string, en: boolean): string {
  const messages: Record<string, [string, string]> = {
    source_unavailable: ["Source is unavailable for this cutoff or display.", "资料不可展示或不符合截止时间。"],
    source_missing: ["The quotation has no source attached.", "引文未关联来源。"],
    quotation_missing: ["No source quotation is saved.", "未保存来源引文。"],
    quotation_unlocated: ["The quotation was not found in the source.", "无法在资料中定位该引文。"],
    quotation_ambiguous: ["The quotation occurs in multiple locations.", "该引文有多个可能位置。"],
    quotation_partial: ["The quotation cuts through a word or number.", "该引文截断了单词或数字。"],
    location_mismatch: ["The saved location does not match the quotation.", "保存的位置与引文不一致。"],
    value_missing: ["The edited value is not present in the quotation.", "编辑后的数值未出现在引文中。"],
    not_checked: ["The quotation was not checked because the review limit was reached.", "达到检查数量上限，未核对该引文。"],
  };
  return (messages[value] ?? ["Source details require review.", "来源详情需要核查。"])[en ? 0 : 1];
}

function safeCitationUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : null;
  } catch { return null; }
}

function SourceTimes({ source, en }: { source: { event_at?: string | null; published_at?: string | null; obtained_at?: string | null; revised_at?: string | null }; en: boolean }) {
  const missing = en ? "unknown" : "未知";
  return <small>{en ? "Event" : "事件时间"}: {source.event_at || missing} · {en ? "Published" : "公开时间"}: {source.published_at || missing} · {en ? "Retrieved" : "获取时间"}: {source.obtained_at || missing} · {en ? "Revised" : "修订时间"}: {source.revised_at || missing}</small>;
}

function CandidateDate({ publishedAt, cutoff, en }: { publishedAt: string | null | undefined; cutoff: string | null | undefined; en: boolean }) {
  const { date, relation } = searchCandidateDate(publishedAt, cutoff);
  return <p><small>{en ? "Search provider date: " : "搜索服务日期："}{date ?? (en ? "unknown" : "未知")} · {en ? "date only, not independently verified" : "仅日期，未经独立核实"}.
    {cutoff ? <> {relation === "before" ? (en ? "The date precedes the UTC cutoff date; availability and revision history still need checking." : "该日期早于截止日（UTC），仍需核查公开与修订历史。") : relation === "same_or_after" ? (en ? "This date is on or after the UTC cutoff date; availability before the cutoff is not established." : "该日期在截止日（UTC）当天或之后，不能据此认定截止前可用。") : (en ? "Availability before the cutoff is not established." : "无法据此认定截止前可用。")}</> : null}</small></p>;
}

function reviewedAncestor(topic: ResearchTopic | null, parentId: string | null | undefined) {
  const versions = new Map((topic?.versions ?? []).map((version) => [version.version_id, version]));
  const visited = new Set<string>();
  let id = parentId;
  while (id && !visited.has(id)) {
    visited.add(id);
    const version = versions.get(id);
    if (!version) return undefined;
    if (version.review !== "draft") return version;
    id = version.parent_version_id;
  }
  return undefined;
}

export function GeneralResearch({ baseUrl, accountId, language }: Props) {
  const en = language === "en";
  const api = useMemo(() => baseUrl ? researchClient(baseUrl) : null, [baseUrl]);
  const [topics, setTopics] = useState<ResearchTopic[]>([]);
  const [topic, setTopic] = useState<ResearchTopic | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState("");
  const [discoverScope, setDiscoverScope] = useState(false);
  const [runSearchScope, setRunSearchScope] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [publishedAt, setPublishedAt] = useState("");
  const [eventAt, setEventAt] = useState("");
  const [revisedAt, setRevisedAt] = useState("");
  const [sourceMode, setSourceMode] = useState<"text" | "sec" | "file">("text");
  const [file, setFile] = useState<File | null>(null);
  const [pdfTextOrder, setPdfTextOrder] = useState<"layout" | "reading">("layout");
  const [fileInputKey, setFileInputKey] = useState(0);
  const [cik, setCik] = useState("");
  const [issuerQuery, setIssuerQuery] = useState("");
  const [issuerCandidates, setIssuerCandidates] = useState<IssuerCandidate[]>([]);
  const [issuerRetrievedAt, setIssuerRetrievedAt] = useState("");
  const [taxonomy, setTaxonomy] = useState("us-gaap");
  const [concept, setConcept] = useState("RevenueFromContractWithCustomerExcludingAssessedTax");
  const [unit, setUnit] = useState("USD");
  const [filingBefore, setFilingBefore] = useState("");
  const [modelInput, setModelInput] = useState(false);
  const [allowExport, setAllowExport] = useState(false);
  const [isSimulated, setIsSimulated] = useState(false);
  const [confirmRights, setConfirmRights] = useState(false);
  const [candidates, setCandidates] = useState<DiscoveryCandidate[]>([]);
  const [summary, setSummary] = useState("");
  const [note, setNote] = useState("");
  const [claims, setClaims] = useState<ResearchClaim[]>([]);
  const [facts, setFacts] = useState<ResearchFact[]>([]);
  const [calculationFormula, setCalculationFormula] = useState<"growth_percent" | "margin_percent" | "difference">("difference");
  const [calculationFirst, setCalculationFirst] = useState("");
  const [calculationSecond, setCalculationSecond] = useState("");
  const [editingFacts, setEditingFacts] = useState(false);
  const [editingClaims, setEditingClaims] = useState(false);
  const [operation, setOperation] = useState<ResearchOperation | null>(null);
  const [runInputs, setRunInputs] = useState<RunInputSnapshot | null>(null);
  const [runQuestion, setRunQuestion] = useState("");
  const [allowSearch, setAllowSearch] = useState(true);
  const [autoFetchOfficial, setAutoFetchOfficial] = useState(false);
  const [supportReview, setSupportReview] = useState(false);
  const [reviewMode, setReviewMode] = useState<"batch" | "isolated">("batch");
  const [maxModelCalls, setMaxModelCalls] = useState("4");
  const [requireCounterarguments, setRequireCounterarguments] = useState(true);
  const [focusChoice, setFocusChoice] = useState("");
  const [maxElapsed, setMaxElapsed] = useState("60");
  const [maxTokens, setMaxTokens] = useState("10000");
  const [maxInputTokens, setMaxInputTokens] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [sourceMetadata, setSourceMetadata] = useState<SourceMetadata[]>([]);
  const [excludedSourceIds, setExcludedSourceIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [pollNotice, setPollNotice] = useState("");
  const pendingRun = useRef<{ topicId: string; versionId: string; key: string; budget: RunBudget } | null>(null);
  const pendingSource = useRef<{ fingerprint: string; sourceId: string; file: File | null } | null>(null);
  const pendingReextract = useRef<{ accountId: string; topicId: string; sourceId: string; textOrder: "layout" | "reading"; key: string; newSourceId?: string } | null>(null);
  const sourceDetails = useRef<HTMLDetailsElement>(null);
  const latest = topic?.versions[topic.versions.length - 1];
  const current = topic?.versions.find((item) => item.version_id === selectedVersionId) ?? latest;
  const isLatest = current?.version_id === latest?.version_id;
  useEffect(() => { setFocusChoice(""); }, [current?.version_id]);
  useEffect(() => { setRunSearchScope(false); setRunQuestion(""); setCalculationFirst(""); setCalculationSecond(""); setExcludedSourceIds([]); }, [accountId, topic?.topic_id]);
  const focusOptions = (current?.content.claims ?? []).flatMap((claim, claimIndex) => (claim.citations ?? []).flatMap((citation, citationIndex) => {
    const source = sourceMetadata.find((item) => item.source_id === citation.source_id);
    if (!source || excludedSourceIds.includes(source.source_id) || !source.permissions?.model_input || !canUseCitationSource(source, topic?.request.as_of) || !citation.quotation || citation.partial_token || citation.location?.status === "ambiguous" || citation.location?.character_start === undefined) return [];
    return [{ value: `${claimIndex}:${citationIndex}`, label: `${source.title || source.source_id}: ${citation.quotation.slice(0, 100)}`, claim_index: claimIndex, citation_index: citationIndex }];
  }));
  const parent = reviewedAncestor(topic, current?.parent_version_id);
  const originalClaims = useMemo(() => new Set((current?.content.claims ?? []).map((item) => JSON.stringify(item))), [current]);
  const originalFacts = useMemo(() => new Set((current?.content.facts ?? []).map((item) => JSON.stringify(item))), [current]);
  const hasUnsavedFacts = !!current && JSON.stringify(facts) !== JSON.stringify(current.content.facts);
  const hasUnsavedReview = !!current && (summary !== current.content.summary || note !== current.note ||
    JSON.stringify(claims) !== JSON.stringify(current.content.claims) || hasUnsavedFacts);

  useEffect(() => {
    let active = true;
    if (api && accountId) void api.list().then((items) => { if (active) setTopics(items); })
      .catch(() => { if (active) setNotice(en ? "Could not load research." : "无法加载研究。") });
    return () => { active = false; };
  }, [api, accountId, en]);

  useEffect(() => {
    setSummary(current?.content.summary ?? "");
    setNote(current?.note ?? "");
    setClaims(current?.content.claims ?? []);
    setFacts(current?.content.facts ?? []);
    setEditingClaims(false);
    setEditingFacts(false);
  }, [current?.version_id]);

  useEffect(() => { setAutoFetchOfficial(false); }, [topic?.topic_id]);

  useEffect(() => {
    let active = true;
    setSourceMetadata([]);
    if (api && topic && current) void api.sources(topic.topic_id, current.version_id)
      .then((items) => { if (active) setSourceMetadata(items); })
      .catch(() => { if (active) setNotice(en ? "Could not load source details." : "无法加载来源信息。"); });
    return () => { active = false; };
  }, [api, topic?.topic_id, current?.version_id, en]);

  useEffect(() => {
    let active = true;
    setRunInputs(null);
    if (api && topic && operation) void api.runInputs(topic.topic_id, operation.operation_id)
      .then((inputs) => { if (active) setRunInputs(inputs); })
      .catch(() => { if (active) setRunInputs(null); });
    return () => { active = false; };
  }, [api, topic?.topic_id, operation?.operation_id]);

  useEffect(() => {
    if (!api || !topic || !operation || !["pending", "running"].includes(operation.status)) { setPollNotice(""); return; }
    let active = true;
    let timer: number | undefined;
    let delay = 1500;
    const poll = async () => {
      try {
        const { operation: next } = await api.operation(topic.topic_id, operation.operation_id);
        if (!active) return;
        if (!["pending", "running"].includes(next.status)) {
          const updated = await api.get(topic.topic_id);
          if (active) { setTopic(updated); setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item)); setPollNotice(""); setOperation(next); }
          return;
        }
        setPollNotice(""); setOperation(next); delay = 1500;
      } catch (error) {
        if (!active) return;
        if (error instanceof ResearchRequestError && error.status >= 400 && error.status < 500 && error.status !== 429) {
          setPollNotice(en ? "Cannot check this run. Reopen the research to refresh its status." : "无法查询此运行，请重新打开研究以刷新状态。不要重复启动。 ");
          return;
        }
        delay = researchPollingDelay(error, delay);
        setPollNotice(en ? "Could not check progress; the same run will be checked again." : "暂时无法获取运行状态，将继续查询同一次运行。不要重复启动。 ");
      }
      if (active) timer = window.setTimeout(() => void poll(), delay);
    };
    timer = window.setTimeout(() => void poll(), delay);
    return () => { active = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [api, topic?.topic_id, operation?.operation_id, operation?.status, en]);

  if (!baseUrl) return <p role="status">{en ? "Connect the API to use the research workspace." : "连接 API 后即可使用研究工作区。"}</p>;
  if (!accountId || !api) return <p role="status">{en ? "Sign in to create private research." : "登录后即可创建私人研究。"}</p>;
  const running = !!operation && ["pending", "running"].includes(operation.status);
  const hasMaterial = sourceMode === "sec" ? Boolean(cik.trim()) : sourceMode === "file" ? Boolean(file) : Boolean(text.trim() || url.trim());
  const hasMaterialDraft = Boolean(title.trim() || text.trim() || url.trim() || publishedAt || eventAt || revisedAt || file || cik.trim() || filingBefore || issuerQuery.trim());

  function updateFact(index: number, patch: Partial<ResearchFact>) {
    setFacts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  }

  async function uploadMaterial(): Promise<string> {
    if (!api || !hasMaterial || !confirmRights) throw new Error("rights_required");
    const fingerprint = JSON.stringify([sourceMode, title, text, url, publishedAt, eventAt, revisedAt, cik, taxonomy, concept, unit, filingBefore, modelInput, allowExport, isSimulated, pdfTextOrder]);
    if (pendingSource.current?.fingerprint === fingerprint && pendingSource.current.file === file) return pendingSource.current.sourceId;
    const sourceTitle = title.trim() || (en ? "Provided material" : "用户提供的资料");
    const source = sourceMode === "sec" ? await api.fetchSecConcept(cik.trim(), taxonomy, concept.trim(), unit.trim(), filingBefore, modelInput, allowExport)
      : sourceMode === "file" && file ? await (async () => {
          if (file.size > MAX_RESEARCH_PDF_BYTES) throw new Error("file_too_large");
          const htmlFile = /\.html?$/i.test(file.name);
          if (!htmlFile && !file.name.toLowerCase().endsWith(".pdf")) throw new Error("invalid_file");
          const bytes = new Uint8Array(await file.arrayBuffer());
          let binary = "";
          for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
          return htmlFile ? api.uploadHtml(title.trim() || file.name, btoa(binary), modelInput, allowExport, publishedAt, eventAt, revisedAt, isSimulated)
            : api.uploadPdf(title.trim() || file.name, btoa(binary), modelInput, allowExport, publishedAt, eventAt, revisedAt, isSimulated, pdfTextOrder);
        })()
      : text.trim() ? await api.createSource(sourceTitle, text, url.trim(), modelInput, allowExport, publishedAt, eventAt, revisedAt, isSimulated)
      : await api.fetchSource(sourceTitle, url.trim(), modelInput, allowExport, publishedAt, eventAt, revisedAt, isSimulated);
    pendingSource.current = { fingerprint, sourceId: source.source_id, file };
    return source.source_id;
  }
  function clearMaterial() {
    pendingSource.current = null;
    setTitle(""); setText(""); setUrl(""); setPublishedAt(""); setEventAt(""); setRevisedAt(""); setCik(""); setFilingBefore(""); setFile(null); setFileInputKey((value) => value + 1);
    setConfirmRights(false); setModelInput(false); setAllowExport(false); setIsSimulated(false); setPdfTextOrder("layout");
    setIssuerQuery(""); setIssuerCandidates([]); setIssuerRetrievedAt("");
  }

  function selectCandidate(candidate: { title: string; url: string }) {
    if (hasMaterialDraft) {
      setNotice(en ? "Finish adding the current material, or clear it before choosing another source." : "请先添加当前资料，或清空当前资料后再选择其他来源。");
      if (sourceDetails.current) {
        sourceDetails.current.open = true;
        sourceDetails.current.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      return;
    }
    pendingSource.current = null;
    setConfirmRights(false); setModelInput(false); setAllowExport(false); setIsSimulated(false);
    setTitle(candidate.title); setUrl(candidate.url); setSourceMode("text"); setCandidates([]); setNotice("");
    if (sourceDetails.current) {
      sourceDetails.current.open = true;
      sourceDetails.current.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  async function searchIssuers() {
    if (!api || !issuerQuery.trim()) return;
    setBusy(true); setNotice(""); setIssuerCandidates([]); setIssuerRetrievedAt("");
    try {
      const found = await api.searchSecIssuers(issuerQuery.trim());
      setIssuerCandidates(found.candidates);
      setIssuerRetrievedAt(found.retrieved_at);
      if (!found.candidates.length) setNotice(en ? "No SEC issuer candidates found." : "未找到 SEC 公司候选项。");
    } catch (error) {
      setNotice(error instanceof Error && error.message === "request_429"
        ? (en ? "SEC search is rate limited. Try again shortly." : "SEC 查询受到限流，请稍后重试。")
        : (en ? "SEC issuer search is unavailable. You can enter a CIK directly." : "SEC 公司查询不可用，可以直接输入 CIK。"));
    } finally { setBusy(false); }
  }

  async function create() {
    if (!api || (!question.trim() && !hasMaterial)) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before creating another research topic." : "请先保存当前修改，再创建其他研究。"); return; }
    setBusy(true); setNotice("");
    try {
      let sourceIds: string[] = [];
      if (hasMaterial) {
        if (!confirmRights) { setNotice(en ? "Confirm you can store this material." : "请先确认你有权保存这份资料。"); return; }
        sourceIds = [await uploadMaterial()];
      }
      const created = await api.create(question.trim(), sourceIds, asOf, scope.trim());
      setTopic(created); setSelectedVersionId(""); setTopics((items) => [created, ...items]); setOperation(null);
      setQuestion(""); setScope(""); setDiscoverScope(false); setRunSearchScope(false); setAsOf(""); clearMaterial();
    } catch (error) {
      if (error instanceof Error && error.message === "file_too_large") {
        setNotice(en ? "Files must be no larger than 5 MB." : "文件不能超过 5 MB。");
      } else if ((sourceMode === "sec" || /^https:\/\/www\.sec\.gov\//i.test(url.trim())) && error instanceof Error && error.message === "request_503") {
        setNotice(en ? "SEC access is unavailable. Ask the service administrator to check its configuration." : "SEC 访问暂不可用，请联系服务管理员检查配置。");
      } else if (error instanceof Error && error.message === "request_429") {
        setNotice(en ? "The source is rate limited. Wait a moment and retry." : "来源请求过于频繁，请稍后重试。");
      } else setNotice(en ? "Research could not be created. Check the material and try again." : "创建失败，请检查资料后重试。");
    }
    finally { setBusy(false); }
  }
  async function reextractSource(source: SourceMetadata) {
    if (!api || !accountId || !topic || !current || !isLatest || running || busy) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before changing material." : "请先保存当前修改，再调整资料。"); return; }
    const textOrder = source.pdf_text_order === "layout" ? "reading" : "layout";
    const storageKey = "foresightnine.research.reextract-pending.v1";
    function remember(value: typeof pendingReextract.current) {
      pendingReextract.current = value;
      try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* retry remains available in this tab's memory */ }
    }
    setBusy(true); setNotice("");
    try {
      let pending = pendingReextract.current;
      if (!pending) {
        try {
          const stored: unknown = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
          if (stored && typeof stored === "object") {
            const item = stored as Record<string, unknown>;
            if (item.accountId === accountId && item.topicId === topic.topic_id && item.sourceId === source.source_id &&
                item.textOrder === textOrder && typeof item.key === "string" && item.key.length >= 8 && item.key.length <= 128 &&
                (item.newSourceId === undefined || typeof item.newSourceId === "string")) {
              pending = item as typeof pendingReextract.current;
            }
          }
        } catch { /* unavailable or malformed local state; create a fresh request */ }
      }
      if (!pending || pending.accountId !== accountId || pending.topicId !== topic.topic_id || pending.sourceId !== source.source_id || pending.textOrder !== textOrder) {
        pending = { accountId, topicId: topic.topic_id, sourceId: source.source_id, textOrder, key: crypto.randomUUID() };
        remember(pending);
      }
      else if (pendingReextract.current !== pending) remember(pending);
      if (!pending.newSourceId) {
        const result = await api.reextractPdf(source.source_id, textOrder, pending.key);
        pending.newSourceId = result.source_id;
        remember(pending);
      }
      const newSourceId = pending.newSourceId;
      let updated = topic;
      if (!current.source_ids?.includes(newSourceId)) {
        try { updated = await api.attach(topic.topic_id, current.version_id, [newSourceId]); }
        catch (error) {
          // An attach response may be lost after commit. Inspect the latest
          // version before retrying, to avoid creating a duplicate version.
          const fresh = await api.get(topic.topic_id);
          if (!fresh.versions[fresh.versions.length - 1]?.source_ids?.includes(newSourceId)) throw error;
          updated = fresh;
        }
      }
      remember(null);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      setExcludedSourceIds((items) => Array.from(new Set([...items, source.source_id])).filter((id) => id !== newSourceId));
      setFocusChoice("");
      setNotice(en ? "Alternative extraction added and selected for the next run. The original remains in history." : "已添加另一种提取结果并用于下次研究，原资料仍保留在历史中。");
    } catch (error) {
      const pending = pendingReextract.current;
      if (pending?.sourceId === source.source_id && pending.newSourceId) {
        setNotice(en ? "Extraction was created but could not be attached. Reload the research and retry." : "提取结果已创建但未能添加，请刷新研究后重试。");
      } else {
        if (error instanceof ResearchRequestError && error.status === 409) remember(null);
        setNotice(en ? "Could not confirm extraction. Retry to reuse the same request, or check the original PDF." : "无法确认提取是否完成；可重试同一请求，或核对原 PDF。 ");
      }
    } finally { setBusy(false); }
  }

  async function attachMaterial() {
    if (!api || !topic || !current || !isLatest || !hasMaterial) return;
    if (hasUnsavedReview) {
      setNotice(en ? "Save your edits before attaching material." : "请先保存当前修改，再添加资料。"); return;
    }
    if (!confirmRights) { setNotice(en ? "Confirm you can store this material." : "请先确认你有权保存这份资料。"); return; }
    setBusy(true); setNotice("");
    try {
      const sourceId = await uploadMaterial();
      const updated = await api.attach(topic.topic_id, current.version_id, [sourceId]);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      clearMaterial();
      setNotice(en ? "Material added. Run research to analyze it." : "资料已添加，可运行研究进行分析。");
    } catch (error) {
      if ((sourceMode === "sec" || /^https:\/\/www\.sec\.gov\//i.test(url.trim())) && error instanceof Error && error.message === "request_503") {
        setNotice(en ? "SEC access is unavailable. Ask the service administrator to check its configuration, or upload a saved filing." : "SEC 访问暂不可用，请联系服务管理员检查配置，或上传已保存的财报。");
      } else setNotice(en ? "Could not add material. Check permissions and retry." : "无法添加资料，请检查权限后重试。");
    }
    finally { setBusy(false); }
  }
  async function recoverOfficialSource() {
    const sourceId = runInputs?.planned_official_source_id;
    if (!api || !topic || !current || !sourceId || !isLatest || running || busy) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before recovering material." : "请先保存修改，再恢复资料。"); return; }
    setBusy(true); setNotice("");
    try {
      const updated = await api.attach(topic.topic_id, current.version_id, [sourceId]);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      setNotice(en ? "The saved source was attached. Run research to analyze it." : "已恢复保存的资料，可运行研究进行分析。");
    } catch { setNotice(en ? "No saved source is available from this run." : "此运行没有可恢复的已保存资料。"); }
    finally { setBusy(false); }
  }
  async function openTopic(topicId: string) {
    if (!api) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before reopening research." : "请先保存当前修改，再打开研究。"); return; }
    setBusy(true); setNotice("");
    try {
      const [updated, runs] = await Promise.all([api.get(topicId), api.runs(topicId)]);
      setTopic(updated); setSelectedVersionId(""); setRunSearchScope(false); setOperation(runs[0]?.operation ?? null);
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
    } catch { setNotice(en ? "Could not open research." : "无法打开研究。"); }
    finally { setBusy(false); }
  }
  async function discover() {
    if (!api || !question.trim()) return;
    setBusy(true); setNotice(""); setCandidates([]);
    try {
      const found = await api.search(sourceDiscoveryQuery(question, scope, discoverScope));
      if (!found.project_search_available) setNotice(en ? "Search is unavailable. Add a source directly." : "搜索暂不可用，可以直接添加来源资料。");
      else if (!found.candidates.length) setNotice(en ? "No source candidates found." : "未找到候选来源。");
      else setCandidates(found.candidates);
    } catch { setNotice(en ? "Search is unavailable. Add a source directly." : "搜索暂不可用，可以直接添加来源资料。"); }
    finally { setBusy(false); }
  }
  async function run() {
    if (!api || !topic || !current) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before running research." : "请先保存当前修改，再运行研究。"); return; }
    const focus = focusChoice ? focusOptions.find((item) => item.value === focusChoice) : undefined;
    if (focusChoice && !focus) { setNotice(en ? "The selected quotation is no longer available." : "所选引文已不可用，请重新选择。"); return; }
    if (!/^\d+$/.test(maxElapsed) || Number(maxElapsed) < 1 || Number(maxElapsed) > 180 ||
        !/^\d+$/.test(maxTokens) || Number(maxTokens) > 20000 ||
        (supportReview && reviewMode === "isolated" && (!/^\d+$/.test(maxModelCalls) || Number(maxModelCalls) < 2 || Number(maxModelCalls) > 10)) ||
        (maxInputTokens !== "" && (!/^\d+$/.test(maxInputTokens) || Number(maxInputTokens) < 1 || Number(maxInputTokens) > 12000)) ||
        (maxCost !== "" && !/^\d+(?:\.\d{1,6})?$/.test(maxCost))) {
      setNotice(en ? "Check the research budget values." : "请检查研究预算设置。"); return;
    }
    setBusy(true); setNotice("");
    if (!pendingRun.current || pendingRun.current.topicId !== topic.topic_id || pendingRun.current.versionId !== current.version_id) {
      pendingRun.current = { topicId: topic.topic_id, versionId: current.version_id, key: crypto.randomUUID(),
        budget: { ...(excludedSourceIds.length ? { selectedSourceIds: Array.from(new Set([...topic.request.source_ids, ...(current.source_ids ?? [])])).filter((id) => !excludedSourceIds.includes(id)) } : {}), ...(runQuestion.trim() ? { questionOverride: runQuestion.trim() } : {}), search: allowSearch, ...(allowSearch && autoFetchOfficial && !topic.request.as_of ? { autoFetchOfficial: true } : {}), supportReview, ...(supportReview && reviewMode === "isolated" ? { supportReviewMode: "isolated", maxModelCalls: Number(maxModelCalls) } : {}), requireCounterarguments, maxElapsedSeconds: Number(maxElapsed), maxTotalTokens: Number(maxTokens), ...(maxInputTokens ? { maxInputTokensPerCall: Number(maxInputTokens) } : {}), ...(allowSearch && runSearchScope && topic.request.scope ? { includeScopeInSearch: true } : {}), ...(focus ? { focusCitation: { claim_index: focus.claim_index, citation_index: focus.citation_index } } : {}), ...(maxCost ? { maxCostUsd: maxCost } : {}) } };
    }
    try { const receipt = await api.run(topic.topic_id, current.version_id, pendingRun.current.key, pendingRun.current.budget);
      pendingRun.current = null;
      // Follow the latest version after this run, even if the latest version
      // had been explicitly selected before starting it.
      setSelectedVersionId("");
      if (!["pending", "running"].includes(receipt.operation.status)) {
        const updated = await api.get(topic.topic_id); setTopic(updated);
        setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      }
      setOperation(receipt.operation); }
    catch { setNotice(en ? "Could not start research." : "无法启动研究。"); }
    finally { setBusy(false); }
  }
  async function save(review: "draft" | "confirmed" | "reservation") {
    if (!api || !topic || !current) return;
    if (claims.some((claim) => !claim.text.trim() || claim.text.length > 3000)) {
      setNotice(en ? "Each finding must contain between 1 and 3,000 characters." : "每条结论或反方意见须填写 1 至 3000 个字符。"); return;
    }
    if (facts.some((fact) => !originalFacts.has(JSON.stringify(fact)) && (!String(fact.value).trim() || !String(fact.unit ?? "").trim() || !String(fact.period ?? "").trim() ||
        !String(fact.label ?? "").trim() || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(String(fact.value).trim())))) {
      setNotice(en ? "Each edited figure needs a valid number, label, unit and period." : "每项修改后的事实都需要填写指标、有效数值、单位和期间。"); return;
    }
    setBusy(true); setNotice("");
    try { const updated = await api.save(topic.topic_id, current, summary, review, note, claims, facts); setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item)); }
    catch (error) {
      setNotice(error instanceof Error && error.message === "request_422"
        ? (en ? "Check findings, figure values, units, periods and quotations. Added quotations must exactly match attached, displayable material that was available by the research cutoff." : "请检查结论、数值、单位和期间；新增引文必须与已关联、允许展示且符合研究截止时间的资料原文完全一致。")
        : (en ? "Save failed; reload before trying again." : "保存失败，请重新打开研究后再试。"));
    }
    finally { setBusy(false); }
  }
  async function confirmDirection(factId: string) {
    if (!api || !topic || !current || !isLatest || running) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before confirming this figure." : "请先保存当前修改，再确认此数值。"); return; }
    setBusy(true); setNotice("");
    try {
      const updated = await api.confirmDirection(topic.topic_id, current.version_id, factId);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      setNotice(en ? "Your confirmation was saved in a new version; the source is not independently verified." : "已在新版本记录你的确认；来源尚未独立核验。");
    } catch { setNotice(en ? "Could not confirm this figure. Check its source, location and other warnings." : "无法确认此数值，请检查来源、原文位置及其他提示。"); }
    finally { setBusy(false); }
  }
  async function confirmTable(factId: string) {
    if (!api || !topic || !current || !isLatest || running) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before confirming this figure." : "请先保存当前修改，再确认此数值。"); return; }
    setBusy(true); setNotice("");
    try {
      const updated = await api.confirmTable(topic.topic_id, current.version_id, factId);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      setNotice(en ? "Your table review was saved in a new version; the source is not independently verified." : "已在新版本记录你的表格核对；来源尚未独立核验。");
    } catch { setNotice(en ? "Could not confirm this figure. Check its source, location and other warnings." : "无法确认此数值，请检查来源、原文位置及其他提示。"); }
    finally { setBusy(false); }
  }
  async function createCalculation() {
    if (!api || !topic || !current || !isLatest || running) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before calculating." : "请先保存当前修改，再计算。"); return; }
    if (!calculationFirst || !calculationSecond || calculationFirst === calculationSecond) {
      setNotice(en ? "Choose two different figures." : "请选择两个不同的数值。"); return;
    }
    setBusy(true); setNotice("");
    try {
      const updated = await api.createCalculation(topic.topic_id, current.version_id, calculationFormula, calculationFirst, calculationSecond);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      setNotice(en ? "Calculation saved in a new version. Check the source and meaning of each input." : "计算已保存到新版本，请核对各输入的来源和含义。");
    } catch { setNotice(en ? "Could not calculate. Check the selected figures, units, periods and review warnings." : "无法计算，请检查所选数值、单位、期间及核对提示。"); }
    finally { setBusy(false); }
  }
  async function recalculate(index: number) {
    if (!api || !topic || !current || !isLatest || running) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before recalculating." : "请先保存当前修改，再重新计算。"); return; }
    setBusy(true); setNotice("");
    try {
      const updated = await api.recalculate(topic.topic_id, current.version_id, index);
      setTopic(updated); setSelectedVersionId("");
      setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      setNotice(en ? "Calculation updated in a new draft version." : "已重新计算，并保存为新的草稿版本。");
    } catch { setNotice(en ? "Could not recalculate. Check that the input facts and units are valid." : "无法重新计算，请检查输入事实和单位。"); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (!api || !operation || !topic) return;
    setBusy(true);
    try {
      const stopped = await api.cancel(operation, crypto.randomUUID());
      setOperation(stopped.operation);
      try {
        const updated = await api.get(topic.topic_id);
        setTopic(updated); setSelectedVersionId("");
        setTopics((items) => items.map((item) => item.topic_id === updated.topic_id ? updated : item));
      } catch { setNotice(en ? "Cancellation was confirmed, but saved research could not be refreshed. Reopen it to check." : "取消已确认，但无法刷新已保存的研究。请重新打开研究查看。"); }
    }
    catch { setNotice(en ? "Cancellation could not be confirmed." : "无法确认取消结果。"); }
    finally { setBusy(false); }
  }
  async function exportResearch(metadataOnly: boolean, textReport = false) {
    if (!api || !topic || !current) return;
    if (hasUnsavedReview) { setNotice(en ? "Save your edits before exporting." : "请先保存当前修改，再导出。"); return; }
    setBusy(true); setNotice("");
    try {
      const result = metadataOnly ? await api.exportMetadata(topic.topic_id, current.version_id) : await api.exportVersion(topic.topic_id, current.version_id);
      const link = document.createElement("a");
      const content = textReport && !metadataOnly ? formatResearchTextReport(result as Awaited<ReturnType<typeof api.exportVersion>>, en) : JSON.stringify(result, null, 2);
      const objectUrl = URL.createObjectURL(new Blob([content], { type: textReport ? "text/plain;charset=utf-8" : "application/json" }));
      link.href = objectUrl; link.download = textReport ? "research-report.txt" : metadataOnly ? "research-metadata.json" : "research.json"; link.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch { setNotice(en ? "Export is unavailable; check source permissions. A metadata-only record is available separately." : "无法导出完整研究，请检查资料权限；也可以单独导出不含研究内容的记录摘要。"); }
    finally { setBusy(false); }
  }
  async function downloadOriginal(sourceId: string, html = false) {
    if (!api) return;
    setBusy(true); setNotice("");
    try {
      const blob = html ? await api.originalHtml(sourceId) : await api.originalPdf(sourceId);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = objectUrl; link.download = html ? "source.html" : "source.pdf"; link.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch { setNotice(en ? "The original document is unavailable for download." : "原文件无法下载，请检查资料权限。"); }
    finally { setBusy(false); }
  }
  return <section className="general-research">
    <Header eyebrow={en ? "RESEARCH WORKSPACE" : "研究工作区"} title={en ? "Start with a question" : "从问题开始研究"} />
    <p className="lead">{en ? "Collect sources, examine evidence and keep your research together." : "整理资料、检查证据，并保存你的研究。"}</p>
    <div className="general-grid">
      <div>
        <Field label={en ? "Research question" : "研究问题"}><Textarea value={question} disabled={busy} onChange={(event) => { setQuestion(event.target.value); setCandidates([]); }} placeholder={en ? "What would you like to investigate?" : "你想研究什么？"} /></Field>
        <Field label={en ? "Scope and desired output (optional)" : "研究范围与期望成果（可选）"} helper={en ? "For example, companies, periods or the comparison you need. The scope is sent to the model during research." : "例如需要研究的公司、期间或比较方式。运行研究时会将范围发送给模型。"}><Textarea value={scope} maxLength={10000} disabled={busy} onChange={(event) => { setScope(event.target.value); setCandidates([]); }} /></Field>
        <Field label={en ? "Research cutoff (optional)" : "研究截止时间（可选）"} helper={en ? "Uses your device's local time." : "使用设备所在时区。"}><Input type="datetime-local" value={asOf} disabled={busy} onChange={(event) => { setAsOf(event.target.value); setCandidates([]); }} /></Field>
        <Button disabled={busy || !question.trim()} onClick={() => void discover()}>{en ? "Find sources" : "查找资料"}</Button>
        {scope.trim() ? <label><input type="checkbox" checked={discoverScope} disabled={busy} onChange={(event) => { setDiscoverScope(event.target.checked); setCandidates([]); }} /> {en ? "Include part of the scope in source search (sent to the search provider)." : "查找资料时包含部分研究范围（将发送给搜索服务）。"}</label> : null}
        {question.trim() ? <p><small>{en ? "Search query: " : "搜索内容："}{sourceDiscoveryQuery(question, scope, discoverScope)}</small></p> : null}
        {candidates.length ? <section className="general-candidates"><p>{en ? "Search results are unverified leads. Check publication and revision times before attaching a source." : "搜索结果是待核验线索，添加资料前请核查公开和修订时间。"}</p>{candidates.map((candidate, index) => <article key={index}><strong>{candidate.title}</strong><CandidateDate publishedAt={candidate.published_at} cutoff={asOf} en={en} /><p>{candidate.excerpt}</p><Button disabled={busy || running} onClick={() => selectCandidate(candidate)}>{en ? "Use link" : "使用链接"}</Button></article>)}</section> : null}
        <details className="general-source" ref={sourceDetails}><summary>{en ? "Add material (optional)" : "添加资料（可选）"}</summary>
          <Field label={en ? "Source type" : "资料类型"}><select className="text-input select-input" value={sourceMode} onChange={(event) => { const mode = event.target.value as "text" | "sec" | "file"; setSourceMode(mode); if (mode === "sec") setIsSimulated(false); }}><option value="text">{en ? "Text or official page" : "正文或官方网页"}</option><option value="file">{en ? "PDF or HTML document" : "PDF 或 HTML 文档"}</option><option value="sec">{en ? "SEC company concept" : "SEC 公司财务指标"}</option></select></Field>
          {sourceMode === "sec" ? <>
            <Field label={en ? "Company name or ticker" : "公司名称或股票代码"}><Input value={issuerQuery} onChange={(event) => { setIssuerQuery(event.target.value); setCik(""); setIssuerCandidates([]); setIssuerRetrievedAt(""); }} /></Field>
            <Button disabled={busy || !issuerQuery.trim()} onClick={() => void searchIssuers()}>{en ? "Find SEC issuers" : "查找 SEC 公司"}</Button>
            {issuerCandidates.length ? <div className="general-candidates"><p>{en ? "Choose an issuer candidate. Exchange, security class and historical validity are not established." : "请选择公司候选项；交易所、证券类别和历史有效性尚未确认。"}</p><p><a href="https://www.sec.gov/files/company_tickers.json" target="_blank" rel="noreferrer">SEC</a> · {en ? "retrieved" : "获取时间"}: {issuerRetrievedAt ? new Date(issuerRetrievedAt).toLocaleString() : (en ? "unknown" : "未知")}</p>{issuerCandidates.map((candidate) => <article key={`${candidate.cik}:${candidate.ticker}`}><strong>{candidate.name}</strong><p>{candidate.ticker} · CIK {candidate.cik}</p><Button onClick={() => { setCik(candidate.cik); setIssuerCandidates([]); }}>{en ? "Use this CIK" : "使用此 CIK"}</Button></article>)}</div> : null}
            <Field label={en ? "Company CIK" : "公司 CIK"}><Input inputMode="numeric" value={cik} onChange={(event) => setCik(event.target.value)} placeholder="0000789019" /></Field>
            <Field label={en ? "XBRL taxonomy" : "XBRL 分类"}><select className="text-input select-input" value={taxonomy} onChange={(event) => setTaxonomy(event.target.value)}><option value="us-gaap">US GAAP</option><option value="ifrs-full">IFRS</option><option value="dei">DEI</option></select></Field>
            <Field label={en ? "XBRL concept" : "XBRL 指标"}><Input value={concept} onChange={(event) => setConcept(event.target.value)} /></Field>
            <Field label={en ? "Unit (optional)" : "单位（可选）"}><Input value={unit} onChange={(event) => setUnit(event.target.value)} /></Field>
            <Field label={en ? "Filed strictly before (optional)" : "提交日期早于（可选）"}><Input type="date" value={filingBefore} onChange={(event) => setFilingBefore(event.target.value)} /></Field>
            <p>{en ? "SEC access requires a server-configured contact. Filing dates do not prove the exact public availability time." : "SEC 访问需要服务端配置联系信息。提交日期不能证明资料的精确公开时刻。"}</p>
          </> : sourceMode === "file" ? <>
            <Field label={en ? "PDF or UTF-8 HTML file (maximum 5 MB)" : "PDF 或 UTF-8 HTML 文件（最大 5 MB）"}><Input key={fileInputKey} type="file" accept="application/pdf,text/html,.pdf,.html,.htm" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></Field>
            {!file || !/\.html?$/i.test(file.name) ? <>
              <Field label={en ? "PDF text order" : "PDF 文字读取方式"}><select className="text-input select-input" value={pdfTextOrder} onChange={(event) => setPdfTextOrder(event.target.value as "layout" | "reading")}><option value="layout">{en ? "Preserve layout (tables)" : "保留版式（表格）"}</option><option value="reading">{en ? "Reading order (prose)" : "阅读顺序（正文）"}</option></select></Field>
              <p>{en ? "Extraction order can change table or paragraph meaning. Check the original PDF; choosing another order creates a separate source." : "读取方式可能改变表格或段落顺序，请对照原 PDF；更换方式会创建一份独立资料。"}</p>
            </> : null}
            <Field label={en ? "Title (optional)" : "资料标题（可选）"}><Input value={title} onChange={(event) => setTitle(event.target.value)} /></Field>
            <Field label={en ? "Publication time (if known)" : "公开时间（如已知）"}><Input type="datetime-local" value={publishedAt} onChange={(event) => setPublishedAt(event.target.value)} /></Field>
            <p>{en ? "Text-based PDFs and UTF-8 HTML are supported. Scanned PDFs require text recognition before upload." : "支持含文本的 PDF 和 UTF-8 HTML；扫描 PDF 需要先识别文字。"}</p>
          </> : <>
          <Field label={en ? "Title" : "资料标题"}><Input value={title} onChange={(event) => setTitle(event.target.value)} /></Field>
          <Field label={en ? "Source URL (optional)" : "来源链接（可选）"}><Input type="url" value={url} onChange={(event) => setUrl(event.target.value)} /></Field>
          <p>{en ? "Direct SEC filing HTML links require the service's SEC contact configuration. You can also upload a saved HTML filing." : "直接读取 SEC 财报 HTML 链接需要服务端配置 SEC 联系信息；也可上传已保存的 HTML 财报。"}</p>
          <Field label={en ? "Publication time (if known)" : "公开时间（如已知）"} helper={en ? "Uses your device's local time; the date remains unverified." : "使用设备所在时区；此时间尚未独立核验。"}><Input type="datetime-local" value={publishedAt} onChange={(event) => setPublishedAt(event.target.value)} /></Field>
          <Field label={en ? "Text" : "资料正文"}><Textarea value={text} onChange={(event) => setText(event.target.value)} maxLength={200000} /></Field>
          <p>{en ? "If text is empty, the app can fetch supported BEA or Federal Reserve HTML pages. Other sources require pasted text." : "正文为空时，可读取支持的美国经济分析局或美联储 HTML 页面；其他来源请粘贴正文。"}</p>
          </>}
          {sourceMode !== "sec" ? <>
            <Field label={en ? "Event time (if known)" : "事件时间（如已知）"} helper={en ? "Uses your device's local time; these dates are not independently verified." : "使用设备所在时区；这些时间尚未独立核验。"}><Input type="datetime-local" value={eventAt} onChange={(event) => setEventAt(event.target.value)} /></Field>
            <Field label={en ? "Revision time (if known)" : "修订时间（如已知）"}><Input type="datetime-local" value={revisedAt} onChange={(event) => setRevisedAt(event.target.value)} /></Field>
            <label><input type="checkbox" checked={isSimulated} onChange={(event) => setIsSimulated(event.target.checked)} /> {en ? "This material is simulated; exclude it from real research." : "这是模拟资料，不用于真实研究。"}</label>
          </> : null}
          <label><input type="checkbox" checked={confirmRights} onChange={(event) => setConfirmRights(event.target.checked)} /> {en ? "I may store this material." : "我有权保存这份资料。"}</label>
          <label><input type="checkbox" checked={modelInput} onChange={(event) => setModelInput(event.target.checked)} /> {en ? "Allow model processing." : "允许模型处理这份资料。"}</label>
          <label><input type="checkbox" checked={allowExport} onChange={(event) => setAllowExport(event.target.checked)} /> {en ? "Allow export of this material." : "允许导出这份资料。"}</label>
          {hasMaterialDraft ? <Button disabled={busy || running} onClick={() => { clearMaterial(); setNotice(""); }}>{en ? "Clear current material" : "清空当前资料"}</Button> : null}
          {topic ? <Button disabled={busy || running || !isLatest || !hasMaterial} onClick={() => void attachMaterial()}>{en ? "Add to current research" : "添加到当前研究"}</Button> : null}
        </details>
        <Button variant="primary" disabled={busy || (!question.trim() && !hasMaterial)} onClick={() => void create()}>{en ? "Create research" : "创建研究"}</Button>
      </div>
      <aside className="general-library"><h2>{en ? "Your research" : "我的研究"}</h2>{topics.length === 0 ? <p>{en ? "No saved research yet." : "还没有保存的研究。"}</p> : topics.map((item) => <button type="button" disabled={busy} className="general-topic" key={item.topic_id} onClick={() => void openTopic(item.topic_id)}>{item.request.question || (en ? "Material-based research" : "资料研究")}</button>)}</aside>
    </div>
    {notice ? <p role="alert">{notice}</p> : null}
    {topic && current ? <section className="general-result">
      <div className="general-result-header"><div><p className="eyebrow">{en ? "CURRENT RESEARCH" : "当前研究"}</p><h2>{topic.request.question || (en ? "Material-based research" : "资料研究")}</h2></div>
        <Button variant="primary" disabled={busy || running || !isLatest} onClick={() => void run()}>{en ? "Run research" : "开始研究"}</Button></div>
      {topic.request.scope ? <p><strong>{en ? "Scope: " : "研究范围："}</strong>{topic.request.scope}</p> : null}
      {topic.request.as_of ? <p><strong>{en ? "Research cutoff: " : "研究截止时间："}</strong><time dateTime={topic.request.as_of}>{topic.request.as_of}</time>. {en ? "Source dates are declared, and model training may include later information." : "来源时间为声明值，模型训练可能包含之后的信息。"}</p> : null}
      <Field label={en ? "Research version" : "研究版本"}><select className="text-input select-input" value={current.version_id} onChange={(event) => { if (hasUnsavedReview && event.target.value !== current.version_id) { setNotice(en ? "Save your edits before changing versions." : "请先保存当前修改，再切换版本。"); return; } setSelectedVersionId(event.target.value); }}>{topic.versions.map((version, index) => <option key={version.version_id} value={version.version_id}>{en ? "Version" : "版本"} {index + 1} · {version.review === "confirmed" ? (en ? "confirmed" : "已确认") : version.review === "reservation" ? (en ? "reservation" : "保留异议") : (en ? "draft" : "草稿")} · {new Date(version.created_at).toLocaleString()}</option>)}</select></Field>
      {!isLatest ? <p role="status">{en ? "Historical version is read-only. Select the latest version to edit or run." : "历史版本为只读；选择最新版本后可编辑或运行研究。"}</p> : null}
      <p>{en ? "Version created by: " : "版本创建者："}{current.created_by === "user" ? (en ? "user" : "用户") : current.created_by === "agent" ? (en ? "research agent" : "研究 Agent") : (en ? "unknown" : "未知")}</p>
      {parent ? <details className="general-source"><summary>{en ? "Compare with the last reviewed version" : "对比此前复核的版本"}</summary>
        <p><strong>{en ? "Previous summary" : "原总结"}</strong></p><p>{parent.content.summary || (en ? "No summary" : "无总结")}</p>
        <p><strong>{en ? "Current summary" : "当前总结"}</strong></p><p>{summary || (en ? "No summary" : "无总结")}</p>
        <p><strong>{en ? "Previous findings" : "原结论"}</strong></p><ul>{parent.content.claims.map((claim, index) => <li key={index}>{claim.text}</li>)}</ul>
        <p><strong>{en ? "Current findings" : "当前结论"}</strong></p><ul>{claims.map((claim, index) => <li key={index}>{claim.text}</li>)}</ul>
        <p><strong>{en ? "Previous figures" : "原数值"}</strong></p><ul>{parent.content.facts.map((fact, index) => <li key={index}>{fact.label}: {fact.value} {fact.unit} · {fact.period}{fact.currency ? ` · ${fact.currency}` : ""}</li>)}</ul>
        <p><strong>{en ? "Current figures" : "当前数值"}</strong></p><ul>{facts.map((fact, index) => <li key={index}>{fact.label}: {fact.value} {fact.unit} · {fact.period}{fact.currency ? ` · ${fact.currency}` : ""}</li>)}</ul>
      </details> : null}
      <p>{en ? "Only material authorized for model processing can be analyzed. You can supply a source or allow retrieval from supported official sites." : "系统仅分析允许模型处理的资料；你可提供资料，或授权获取受支持的官方网页。"}</p>
      {typeof current.content.run.question_override === "string" ? <p><strong>{en ? "Question for this version: " : "本版本的研究问题："}</strong>{current.content.run.question_override}</p> : null}
      {isLatest ? <Field label={en ? "Question for the next run (optional)" : "下次运行的研究问题（可选）"} helper={en ? "Leave blank to use the topic's original question. The new question is saved with its run." : "留空使用主题创建时的问题；填写后将随本次运行保存。"}><Textarea value={runQuestion} maxLength={10000} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setRunQuestion(event.target.value)} /></Field> : null}
      <label><input type="checkbox" checked={allowSearch} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setAllowSearch(event.target.checked)} /> {en ? "When no material is attached, send the question to the configured search provider to find leads." : "未关联资料时，将问题发送给已配置的搜索服务以查找线索。"}</label>
      {allowSearch && !topic.request.as_of ? <label><input type="checkbox" checked={autoFetchOfficial} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setAutoFetchOfficial(event.target.checked)} /> {en ? "I authorize storing, displaying, sending to the model, and exporting research quotations from up to one BEA or Federal Reserve webpage found by search; I have the rights to use its contents." : "我授权保存、展示并将搜索到的最多一份 BEA 或美联储网页发送给模型，允许导出研究引用，且有权使用其中内容。"}</label> : null}
      {allowSearch && topic.request.scope ? <label><input type="checkbox" checked={runSearchScope} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setRunSearchScope(event.target.checked)} /> {en ? "Include part of the saved scope in source search (sent to the search provider)." : "搜索来源时包含部分已保存的研究范围（将发送给搜索服务）。"}</label> : null}
      {allowSearch && (runQuestion.trim() || topic.request.question) ? <p><small>{en ? "If source search runs, its query is: " : "如需搜索来源，发送的内容："}{sourceDiscoveryQuery(runQuestion.trim() || topic.request.question, topic.request.scope ?? "", runSearchScope)}</small></p> : null}
      <label><input type="checkbox" checked={supportReview} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setSupportReview(event.target.checked)} /> {en ? "Ask the model to review citation support (additional paid calls; not independent verification)." : "请求模型复查引用是否支持结论（会产生额外付费调用；不等于独立核验）。"}</label>
      {supportReview ? <><Field label={en ? "Review mode" : "复查方式"} helper={en ? "Separate review sends each finding in its own call; it can cost more and may finish only partially." : "逐条复查为每条结论单独调用，可能增加费用，也可能只能部分完成。"}><Select value={reviewMode} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setReviewMode(event.target.value as "batch" | "isolated")}><option value="batch">{en ? "Review together" : "合并复查"}</option><option value="isolated">{en ? "Review separately" : "逐条复查"}</option></Select></Field>{reviewMode === "isolated" ? <Field label={en ? "Maximum model calls including research (2–10)" : "模型调用总次数上限（含研究，2–10）"}><Input type="number" min="2" max="10" value={maxModelCalls} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setMaxModelCalls(event.target.value)} /></Field> : null}<p><small>{en ? "Review is also subject to the total token and time budgets; unknown usage reserves the full call limit." : "复查还受总 Token 和时间预算限制；用量未知时按调用上限预留。"}</small></p></> : null}
      <label><input type="checkbox" checked={requireCounterarguments} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setRequireCounterarguments(event.target.checked)} /> {en ? "Require a cited counterargument for a complete result (turn off for straightforward fact checks)." : "完成研究需要有引文的反方意见（简单事实核验可关闭）。"}</label>
      {focusOptions.length ? <Field label={en ? "Focus on a saved quotation (optional)" : "优先分析已保存的引文（可选）"} helper={en ? "The selected passage and nearby context are included in the next analysis." : "下次分析将包含所选原文及附近上下文。"}><select value={focusChoice} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setFocusChoice(event.target.value)}><option value="">{en ? "No selected quotation" : "不指定引文"}</option>{focusOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field> : null}
      <details className="general-source"><summary>{en ? "Research budget" : "研究预算"}</summary>
        <Field label={en ? "Time limit in seconds (1–180)" : "时间上限（秒，1–180）"}><Input type="number" min="1" max="180" value={maxElapsed} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setMaxElapsed(event.target.value)} /></Field>
        <Field label={en ? "Maximum model tokens" : "模型 Token 上限"}><Input type="number" min="0" max="20000" value={maxTokens} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setMaxTokens(event.target.value)} /></Field>
        <Field label={en ? "Input tokens per call (optional)" : "每次调用的输入 Token 上限（可选）"} helper={en ? "Leave blank for the configured limit. A smaller limit may omit more material; it cannot exceed the deployment limit. The total budget must also cover output and any review call." : "留空使用服务配置的上限；调低可能遗漏更多资料，不能超过服务配置的上限。总预算还需覆盖输出和可能的复查调用。"}><Input type="number" min="1" max="12000" value={maxInputTokens} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setMaxInputTokens(event.target.value)} /></Field>
        <Field label={en ? "Maximum cost in USD (optional)" : "费用上限（美元，可选）"} helper={en ? "If provider pricing is unknown, a cost cap prevents external calls." : "供应商价格未知时，设置费用上限会阻止外部调用。"}><Input type="number" min="0" step="0.01" value={maxCost} disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => setMaxCost(event.target.value)} /></Field>
      </details>
      {operation ? <p role="status">{en ? "Run status: " : "运行状态："}{statusLabel(operation.status, en)}{running ? <> <Button disabled={busy} onClick={() => void cancel()}>{en ? "Cancel" : "取消"}</Button></> : null}</p> : null}
      {pollNotice ? <p role="status">{pollNotice}</p> : null}
      {runInputs ? <details className="general-source"><summary>{en ? "Run input snapshot" : "运行时输入记录"}</summary>
        <p>{en ? "Captured" : "记录时间"}: {new Date(runInputs.captured_at).toLocaleString()} · {en ? "Source version" : "输入版本"}: {runInputs.parent_version_id}</p>
        <p>{en ? "Time limit" : "时间上限"}: {runInputs.budget.max_elapsed_seconds} s · {en ? "Token limit" : "Token 上限"}: {runInputs.budget.max_total_tokens} · {en ? "Cost limit (USD)" : "费用上限（美元）"}: {runInputs.budget.max_cost_usd ?? (en ? "not set" : "未设置")}</p>
        <p>{en ? "Input limit per call" : "每次调用输入上限"}: {runInputs.provider_limits?.max_input_tokens ?? (en ? "unknown" : "未知")}</p>
        <p>{en ? "Model" : "模型"}: {runInputs.model ?? (en ? "unknown" : "未知")} · {en ? "Provider" : "服务方"}: {runInputs.provider ?? (en ? "unknown" : "未知")}</p>
        {runInputs.planned_official_source_id && operation && ["failed", "cancelled"].includes(operation.status) && isLatest ? <p>{en ? "If a retrieved page was saved before interruption, you can attach it without fetching again." : "如果中断前已保存网页，可直接恢复，无需再次获取。"} <Button disabled={busy || running} onClick={() => void recoverOfficialSource()}>{en ? "Recover saved page" : "恢复已保存网页"}</Button></p> : null}
        <ul>{runInputs.sources.map((source) => <li key={source.source_id}>{source.source_id} · SHA-256: {source.content_sha256} · {source.is_simulated ? (en ? "Simulated; excluded from real research" : "模拟资料，排除于真实研究") : (en ? "Model processing" : "模型处理") + ": " + (source.permissions.model_input ? (en ? "allowed" : "允许") : (en ? "not allowed" : "未允许"))}</li>)}</ul>
      </details> : null}
      {operation && ["failed", "cancelled"].includes(operation.status) ? <p role="status">{en ? "This run cannot resume from a checkpoint. Saved versions remain available. Starting another run makes new external requests; previous requests may still be billed." : "此运行无法从检查点续跑，已保存的版本仍可查看。再次运行会发起新的外部请求，之前的请求仍可能产生费用。"}</p> : null}
      {current.content.run.status ? <p>{en ? "Last run result: " : "上次运行结果："}{statusLabel(String(current.content.run.status), en)}</p> : null}
      {current.content.run.checkpoint === "generation" ? <p role="status">{en ? "Generation checkpoint: citation review had not finished when this draft was saved." : "生成阶段草稿：保存此版本时，引文复查尚未完成。"}</p> : null}
      {current.content.run.support_review_requested ? <p>{en ? "Review during this run: " : "本次运行的模型证据复查："}{en ? String(current.content.run.support_review_status) : ({ pending: "保存时未完成", complete: "已完成", partial: "部分完成", failed: "失败", budget_exhausted: "预算不足", not_reviewable: "无可复查内容" } as Record<string, string>)[String(current.content.run.support_review_status)] ?? String(current.content.run.support_review_status)}。{en ? "This is not independent verification." : "该结果不构成独立核验。"}</p> : null}
      <h3>{en ? "Sources" : "资料来源"}</h3>
      {current.content.source_candidates?.length ? <section className="general-candidates"><p>{en ? "Unverified source candidates" : "待核验的来源线索"}</p>{current.content.source_candidates.map((candidate, index) => <article key={index}><strong>{candidate.title}</strong><CandidateDate publishedAt={candidate.published_at} cutoff={topic.request.as_of} en={en} />{candidate.status === "retrieved_unverified" ? <small>{en ? "Retrieved for this run; content remains unverified." : "本次已获取，内容仍需核验。"}</small> : <Button disabled={busy || running} onClick={() => selectCandidate(candidate)}>{en ? "Attach source" : "添加此来源"}</Button>}</article>)}</section> : null}
      {sourceMetadata.length ? <><p>{en ? "Attached material (declared dates are not independently verified). Unchecking only excludes a source from the next run; history is preserved." : "已关联资料（声明的时间尚未独立核验）。取消勾选只影响下次研究，历史记录保留。"}</p><ul>{sourceMetadata.map((source) => { const href = safeCitationUrl(source.url); return <li key={source.source_id}><label><input type="checkbox" checked={!excludedSourceIds.includes(source.source_id)} disabled={busy || running || !isLatest || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onChange={(event) => { const included = event.target.checked; setExcludedSourceIds((items) => included ? items.filter((id) => id !== source.source_id) : [...items, source.source_id]); setFocusChoice(""); }} /> {en ? "Use in next run" : "本次研究使用"}</label>{" "}{href ? <a href={href} target="_blank" rel="noreferrer">{source.title || source.source_id}</a> : source.title || source.source_id} {source.is_simulated ? <strong>{en ? "(simulated; excluded from real research)" : "（模拟，未用于真实研究）"}</strong> : null} {source.status !== "available" ? (en ? "(unavailable)" : "（不可用）") : <><br /><SourceTimes source={source} en={en} /></>}{["official_html", "user_html", "sec_filing_html"].includes(source.retrieval_method ?? "") ? <p><small>{en ? "Extracted HTML text; table markers are generated and browser rendering is not reproduced. Check the original document for layout and visibility." : "HTML 提取文本；表格标记由系统生成，提取结果不等同于浏览器呈现，请查阅原文件确认排版与可见内容。"}</small></p> : null}{source.retrieval_method === "user_pdf" ? <p><small>{en ? "PDF text order: " : "PDF 文字读取方式："}{source.pdf_text_order === "layout" ? (en ? "preserve layout" : "保留版式") : source.pdf_text_order === "reading" ? (en ? "reading order" : "阅读顺序") : (en ? "not recorded for this older source" : "旧资料未记录")}</small></p> : null}{source.pdf_text_spacing_warning ? <p role="status">{en ? "Extracted text may be missing spaces between words. Check the original PDF and consider adding it again using the other text order; the warning is not a complete quality check." : "提取文本可能缺少词间空格。请核对原 PDF，并考虑使用另一种读取方式重新添加；此提示不能全面检查提取质量。"}</p> : null}{source.retrieval_method === "user_pdf" && source.original_pdf_available && isLatest ? <Button disabled={busy || running || (pendingRun.current?.topicId === topic.topic_id && pendingRun.current?.versionId === current.version_id)} onClick={() => void reextractSource(source)}>{source.pdf_text_order === "layout" ? (en ? "Try reading order" : "尝试阅读顺序") : (en ? "Try preserving layout" : "尝试保留版式")}</Button> : null}{(source.retrieval_method === "user_pdf" && source.original_pdf_available || ["user_html", "sec_filing_html"].includes(source.retrieval_method ?? "") && source.original_html_available) && source.permissions?.display && source.permissions.export ? <> <Button disabled={busy} onClick={() => void downloadOriginal(source.source_id, ["user_html", "sec_filing_html"].includes(source.retrieval_method ?? ""))}>{en ? "Download original" : "下载原文件"}</Button></> : null}</li>; })}</ul></> : null}
      {current.content.sources.length ? <ul>{current.content.sources.map((source) => { const href = safeCitationUrl(source.url); return <li key={source.source_id}>{href ? <a href={href} target="_blank" rel="noreferrer">{source.title || source.source_id}</a> : source.title || source.source_id} <SourceTimes source={source} en={en} /></li>; })}</ul> : <p>{en ? "No source analyzed." : "尚无已分析来源。"}</p>}
      {facts.length ? <>
        <h3>{en ? "Extracted figures" : "提取的数值"}</h3>
        <p>{en ? "Values, units and periods require review." : "数值、单位和期间仍需核验。"}</p>
        {isLatest && !running ? <Button disabled={busy} onClick={() => setEditingFacts((value) => !value)}>{editingFacts ? (en ? "Done editing figures" : "完成编辑数值") : (en ? "Edit figures" : "编辑数值")}</Button> : null}
        {editingFacts && isLatest && !running ? <p>{en ? "The original quotation is retained as context. An edited figure is not independently verified." : "原引文将作为上下文保留；修改后的数值尚未独立核验。"}</p> : null}
        {facts.map((fact, index) => <article className="general-claim" key={`${fact.fact_id}:${index}`}>
          <strong>{fact.label}</strong>
          {fact.authorship === "user" || !originalFacts.has(JSON.stringify(fact)) ? <small> {en ? "User-edited; not independently verified." : "用户编辑，尚未独立核验。"}</small> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.source_value_warnings?.length ? <div role="status"><p>{en ? "Check this edited figure against its source:" : "请核对编辑后的数值与来源："}</p><ul>{fact.source_value_warnings.map((warning, warningIndex) => <li key={warningIndex}>{factSourceWarning(warning, en)}</li>)}</ul></div> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.currency_conflict_unverified ? <p role="status">{en ? "The fact currency conflicts with a currency cue on the same quoted value. Review the source before calculating." : "事实的币种与引文中同一数值旁的明确币种不一致，计算前请核对原文。"}</p> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.currency_quote_missing ? <p role="status">{en ? "This table quotation does not identify the currency. Check the original source and currency before relying on the calculation." : "该表格引文未标明币种；使用计算结果前请核对原文及币种。"}</p> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.table_period_conflict ? <p role="status">{en ? `The fact states ${fact.table_period_conflict.declared_year}, but its apparent table column is ${fact.table_period_conflict.column_year}. Check the original table before calculating.` : `事实标注 ${fact.table_period_conflict.declared_year} 年，但表格对应列可能为 ${fact.table_period_conflict.column_year} 年；计算前请核对原表。`}</p> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.table_alignment_unverified ? <p role="status">{en ? "Table column alignment has not been verified. Check the original table, period and unit before relying on this figure or its calculations." : "表格列对齐尚未核实；使用此数值或计算结果前，请核对原始表格、期间和单位。"}</p> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.quotation_match === "pdf_line_hyphen_restored" ? <p role="status">{en ? "This quotation was recovered across a PDF line-break hyphen. Check and replace it with the exact original passage before calculating." : "此引文通过恢复 PDF 换行连字符定位；计算前请核对并替换为准确原文。"}</p> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.direction_context_unverified ? <div><p role="status">{fact.direction_confirmation ? (en ? "You confirmed the sign and subject for this version; the source remains independently unverified." : "你已在此版本确认方向和对象；来源仍未独立核验。") : (en ? "The negative sign was inferred from a decrease in the quotation. Check the subject and whether this is a change or a level before calculating." : "负号根据引文中的下降表述推断；计算前请核对指标对象以及它表示变化还是水平。")}</p>{!fact.direction_confirmation && isLatest && !running ? <Button disabled={busy} onClick={() => void confirmDirection(fact.fact_id)}>{en ? "I checked: this is a negative change" : "我已核对：这是负向变化"}</Button> : null}</div> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.numeric_normalization === "equivalent_decimal_fraction" ? <small> {en ? "Equivalent decimal and fraction were checked exactly." : "小数与分数已按精确数值核对。"}</small> : null}{!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.numeric_normalization === "exact_range_bound" ? <small> {en ? "This figure is a bound extracted from a range." : "该数值是从区间中提取的端点。"}</small> : null}
          {!hasUnsavedFacts && originalFacts.has(JSON.stringify(fact)) && fact.quotation_match === "table_markup_expanded" ? <div>
            <p role="status">{fact.table_confirmation ? (en ? "You reviewed the table for this version; the source remains independently unverified." : "你已核对本版本的表格；来源仍未独立核验。") : (en ? "Compare the original table's company, value, column, period and unit before confirming." : "确认前请对照原始表格核对公司、数值、列、期间和单位。")}</p>
            {!fact.table_confirmation && isLatest && !running ? <Button disabled={busy} onClick={() => void confirmTable(fact.fact_id)}>{en ? "I checked the original table" : "我已核对原始表格"}</Button> : null}
          </div> : null}
          {editingFacts && isLatest && !running ? <>
            <Field label={`${en ? "Metric" : "指标"} ${index + 1}`}><Input value={fact.label ?? ""} maxLength={300} disabled={busy} onChange={(event) => updateFact(index, { label: event.target.value })} /></Field>
            <Field label={`${en ? "Value" : "数值"} ${index + 1}`}><Input value={fact.value ?? ""} inputMode="decimal" maxLength={64} disabled={busy} onChange={(event) => updateFact(index, { value: event.target.value })} /></Field>
            <Field label={`${en ? "Unit" : "单位"} ${index + 1}`}><Input value={fact.unit ?? ""} maxLength={100} disabled={busy} onChange={(event) => updateFact(index, { unit: event.target.value })} /></Field>
            <Field label={`${en ? "Currency (optional)" : "币种（可选）"} ${index + 1}`}><Input value={fact.currency ?? ""} maxLength={16} disabled={busy} onChange={(event) => updateFact(index, { currency: event.target.value || null })} /></Field>
            <Field label={`${en ? "Period" : "期间"} ${index + 1}`}><Input value={fact.period ?? ""} maxLength={100} disabled={busy} onChange={(event) => updateFact(index, { period: event.target.value })} /></Field>
            <CitationEditor mode="replace" loadSource={(sourceId) => api.sourceText(topic.topic_id, current.version_id, sourceId)} sources={sourceMetadata} asOf={topic.request.as_of} disabled={busy} en={en} onAdd={(sourceId, quotation) => setFacts((items) => items.map((item, itemIndex) => {
              if (itemIndex !== index) return item;
              const { location: _location, direction_confirmation: _confirmation, table_confirmation: _tableConfirmation, quotation_match: _match, ...rest } = item;
              return { ...rest, source_id: sourceId, quotation };
            }))} />
          </> : <p>{fact.value} {fact.unit} · {fact.period}{fact.currency ? ` · ${fact.currency}` : ""}</p>}
          {fact.location?.status === "ambiguous" ? <p role="status">{en ? "The quotation has multiple possible locations; this figure is excluded from automatic calculations." : "引文有多个可能位置，该数值未用于自动计算。"}</p> : null}
          {fact.currency && typeof fact.unit === "string" && ["percent", "percentage", "%"].includes(fact.unit.trim().toLowerCase()) ? <p role="status">{en ? "The currency associated with this percentage is unverified; it is excluded from automatic calculations until the metadata is corrected." : "此百分比关联的币种尚未核验；更正资料前不用于自动计算。"}</p> : null}
          <blockquote>{fact.quotation}<small> {fact.source_id}{fact.location?.page ? ` · ${en ? "page" : "第"} ${fact.location.page}${en ? "" : " 页"}` : ""}</small>{fact.quotation_match === "expanded_ellipsis" ? <small> {en ? "Ellipsis expanded to the continuous source text." : "已将省略号展开为连续原文。"}</small> : null}{fact.quotation_match === "trimmed_json_closure" ? <small> {en ? "A model-added closing brace was removed; check the source context." : "已移除模型添加的结束括号，请核对原文上下文。"}</small> : null}{fact.source_id ? <SourceContext key={`${current.version_id}:${fact.source_id}:${fact.location?.character_start}:${fact.location?.character_end}`} sourceId={fact.source_id} location={fact.location} quotation={fact.quotation ?? undefined} en={en} load={api.sourceContext} /> : null}</blockquote>
        </article>)}
        {hasUnsavedFacts ? <p role="status">{en ? "Figures have unsaved changes. Calculations and source differences still reflect the saved version." : "数值修改尚未保存；计算和来源差异仍依据已保存版本。"}</p> : null}
      </> : null}
      {current.content.evidence_differences?.length ? <><h3>{en ? "Differences to review" : "需要核查的数值差异"}</h3><p>{en ? "Matching labels and periods do not establish the same company, accounting basis or independent sources. Check the original material before treating a difference as a conflict." : "指标名称和期间相同，并不代表公司、统计口径或来源独立性相同；请查阅原文后再判断是否冲突。"}</p>{current.content.evidence_differences.length === 50 ? <p>{en ? "At most 50 candidate differences are displayed; additional differences may exist." : "最多展示 50 组待核查差异，可能还有其他差异。"}</p> : null}{current.content.evidence_differences.map((difference, index) => <article className="general-claim" key={index}><strong>{difference.label}</strong><p>{difference.period} · {difference.unit}{difference.currency ? ` · ${difference.currency}` : ""}</p>{difference.matching_basis === "release_qualifiers_ignored" ? <p>{en ? "Release qualifiers were ignored for matching. Compare the original periods and labels below." : "匹配时忽略了发布说明，请对照下方的原始期间与指标名称。"}</p> : null}<ul>{difference.facts.map((fact) => <li key={fact.fact_id}>{fact.value} · {sourceMetadata.find((source) => source.source_id === fact.source_id)?.title || fact.source_id} · {fact.fact_id}{fact.label && fact.label !== difference.label ? ` · ${fact.label}` : ""}{fact.period && fact.period !== difference.period ? ` · ${fact.period}` : ""}</li>)}</ul>{difference.truncated ? <small>{en ? "Additional observations were omitted." : "还有未显示的数值。"}</small> : null}</article>)}</> : null}
      {current.content.calculation_differences?.length ? <><h3>{en ? "Claim and calculation differences" : "结论与计算待核查差异"}</h3><p>{en ? "These are possible mismatches in the saved version, not a semantic judgment. Check the claim, inputs and rounding." : "以下是已保存版本中可能存在的差异，并非语义判定；请核对结论、输入和舍入。"}</p>{hasUnsavedReview ? <p role="status">{en ? "Unsaved edits are not reflected here." : "此处尚未反映未保存的修改。"}</p> : null}{current.content.calculation_differences.map((difference, index) => <article className="general-claim" key={index}><p>{en ? `Finding ${difference.claim_index + 1}` : `结论 ${difference.claim_index + 1}`}：{difference.claimed_value}% · {en ? `calculation ${difference.calculation_index + 1}` : `计算 ${difference.calculation_index + 1}`}：{difference.calculated_value}%</p></article>)}</> : null}
      {current.content.amount_differences?.length ? <><h3>{en ? "Amounts requiring review" : "待核查的金额表述"}</h3><p>{en ? "A currency amount may not match the cited figures after unit conversion. Check the original source, units and period." : "金额换算后可能与所引数值不一致，请核对原文、单位与期间；这不是自动判错。"}</p>{hasUnsavedReview ? <p role="status">{en ? "Unsaved edits are not reflected here." : "此处尚未反映未保存的修改。"}</p> : null}{current.content.amount_differences.map((difference, index) => <article className="general-claim" key={index}><p>{en ? `Finding ${difference.claim_index + 1}` : `结论 ${difference.claim_index + 1}`}：{difference.claimed_amount}</p><ul>{difference.source_fact_ids.map((factId) => { const fact = current.content.facts.find((item) => item.fact_id === factId); return <li key={factId}>{fact ? `${fact.label}: ${fact.value} ${fact.unit} · ${fact.period}` : factId}</li>; })}</ul></article>)}</> : null}
      {isLatest && !running && facts.length >= 2 ? <details className="general-source"><summary>{en ? "Create a calculation from saved figures" : "根据已保存数值创建计算"}</summary>
        <p>{en ? "Choose the first value (current value or numerator) and the second value (baseline or denominator). A difference is first minus second. Check each input's source, period and unit." : "选择第一个数值（本期或分子）和第二个数值（基期或分母）；差值按第一个减第二个计算。请核对各输入的来源、期间和单位。"}</p>
        <Field label={en ? "Calculation type" : "计算类型"}><select className="text-input select-input" value={calculationFormula} onChange={(event) => setCalculationFormula(event.target.value as typeof calculationFormula)}><option value="difference">{en ? "Difference" : "差值"}</option><option value="growth_percent">{en ? "Growth rate" : "增长率"}</option><option value="margin_percent">{en ? "Margin" : "利润率"}</option></select></Field>
        <Field label={en ? "First value" : "第一个数值"}><select className="text-input select-input" value={calculationFirst} onChange={(event) => setCalculationFirst(event.target.value)}><option value="">{en ? "Choose a figure" : "选择数值"}</option>{facts.map((fact, index) => <option key={`${fact.fact_id}:${index}`} value={fact.fact_id}>{fact.label} · {fact.value} {fact.unit} · {fact.period}</option>)}</select></Field>
        <Field label={en ? "Second value" : "第二个数值"}><select className="text-input select-input" value={calculationSecond} onChange={(event) => setCalculationSecond(event.target.value)}><option value="">{en ? "Choose a figure" : "选择数值"}</option>{facts.map((fact, index) => <option key={`${fact.fact_id}:${index}`} value={fact.fact_id}>{fact.label} · {fact.value} {fact.unit} · {fact.period}</option>)}</select></Field>
        <Button disabled={busy} onClick={() => void createCalculation()}>{en ? "Calculate and save" : "计算并保存"}</Button>
      </details> : null}
      {current.content.calculations.length ? <><h3>{en ? "Calculations" : "计算过程"}</h3><p>{en ? "Calculations use the recorded inputs. Their source precision and any prior rounding are not established; results may differ from reported figures." : "计算使用已记录的输入值。来源精度及是否已舍入尚未确认，结果可能与报告中的数值不同。"}</p>{current.content.calculations.map((calculation, index) => <article className="general-claim" key={index}><strong>{calculation.formula_id}</strong>{calculation.authorship === "user" ? <small> {en ? "User-edited; not verified by the calculation engine." : "用户编辑，未经过计算引擎核验。"}</small> : null}{calculation.input_status && calculation.input_status !== "matched" ? <p role="status">{en ? "The saved inputs no longer match the current facts or cannot be checked. This historical result requires recalculation." : "保存的计算输入与当前事实不一致或无法核对；此历史结果需要重新计算。"}</p> : null}<p>{calculation.expression} = {calculation.value} {calculation.unit}</p>{Array.isArray(calculation.inputs) && calculation.inputs.some((input) => facts.some((fact) => fact.fact_id === input.fact_id && fact.table_alignment_unverified)) ? <p role="status">{en ? "A calculation input comes from a table whose column alignment has not been verified. Check the original table, period and unit." : "计算输入来自列对齐尚未核实的表格，请核对原始表格、期间和单位。"}</p> : null}{calculation.period_granularity_check === "different_labels_verify_stock_or_flow" ? <p role="status">{en ? "Period labels differ in granularity. Check whether the figures are stocks or flows and whether the periods are comparable." : "两个期间标签的粒度不同，请核对指标是存量还是流量，以及期间是否可比。"}</p> : null}{calculation.formula_id === "growth_percent" && (calculation.period_order_check !== "recognized_formats_only" || calculation.authorship === "user" || (calculation.input_status && calculation.input_status !== "matched")) ? <p role="status">{en ? "Period order was not automatically checked; verify the current and baseline periods." : "未自动核验期间先后，请检查本期和基期。"}</p> : null}<ul>{Array.isArray(calculation.inputs) ? calculation.inputs.map((input) => <li key={input.fact_id}>{input.fact_id}: {input.value} {input.unit} · {input.period}</li>) : null}</ul>{isLatest && ["1", "2"].includes(calculation.formula_version ?? "") && Array.isArray(calculation.inputs) && calculation.inputs.length === 2 ? <Button disabled={busy || running} onClick={() => void recalculate(index)}>{en ? "Recalculate" : "重新计算"}</Button> : null}</article>)}</> : null}
      <h3>{en ? "Findings and counterarguments" : "研究结论与反方"}</h3>
      {isLatest && !running ? <Button disabled={busy} onClick={() => setEditingClaims((value) => !value)}>{editingClaims ? (en ? "Done editing findings" : "完成编辑结论") : (en ? "Edit findings" : "编辑结论")}</Button> : null}
      {editingClaims && isLatest && !running ? <p>{en ? "Edited findings retain their previous quotations as context; their support must be checked again." : "修改后的结论会保留原引文作为上下文，引用是否支持新结论需要重新核验。"}</p> : null}
      {claims.length ? claims.map((claim, index) => <article className="general-claim" key={index}><strong>{claim.role === "counterargument" ? (en ? "Counterargument" : "反方") : (en ? "Finding" : "结论")}</strong>{editingClaims && isLatest && !running ? <><Field label={`${en ? "Type" : "类型"} ${index + 1}`}><Select value={claim.role === "counterargument" ? "counterargument" : "support"} disabled={busy} onChange={(event) => { const role = event.target.value as "support" | "counterargument"; setClaims((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, role } : item)); }}><option value="support">{en ? "Finding" : "结论"}</option><option value="counterargument">{en ? "Counterargument" : "反方"}</option></Select></Field><Field label={`${claim.role === "counterargument" ? (en ? "Counterargument" : "反方") : (en ? "Finding" : "结论")} ${index + 1}`}><Textarea value={claim.text} maxLength={3000} disabled={busy} onChange={(event) => { const value = event.target.value; setClaims((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, text: value } : item)); }} /></Field><Button disabled={busy} onClick={() => setClaims((items) => items.filter((_, itemIndex) => itemIndex !== index))}>{en ? "Remove" : "移除"}</Button></> : <p>{claim.text}</p>}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.causal_attribution_unverified ? <p role="status">{en ? "The attached quotations do not contain a clear causal cue. Check the evidence for the stated cause; this warning does not decide whether the explanation is true." : "所附引文未出现明确的因果表述，请核对所述原因的依据；此提示不判断解释本身是否正确。"}</p> : null}<small>{claim.authorship === "user" || !originalClaims.has(JSON.stringify(claim)) ? (en ? "User-edited; source support has not been independently verified." : "用户编辑；来源是否支持结论尚未独立核验。") : (en ? "Model interpretation; source support has not been independently verified." : "模型解释；来源是否支持结论尚未独立核验。")}</small>{originalClaims.has(JSON.stringify(claim)) && claim.support_status === "unsupported" ? <p role="status">{en ? "No verifiable quotation is attached to this finding." : "此结论没有可定位的引文。"}</p> : originalClaims.has(JSON.stringify(claim)) && claim.support_status === "incomplete" ? <p role="status">{claim.citations?.some((citation) => citation.quotation_match === "table_markup_expanded") ? (en ? "The full table row was restored; column alignment and support require review." : "已恢复完整表格行；列对齐及是否支持结论仍需核查。") : claim.citations?.some((citation) => citation.location?.status === "ambiguous") ? (en ? "A quotation has multiple possible locations; check the original source." : "引文存在多个可能位置，请核对原始资料。") : claim.citations?.some((citation) => ["split_conjunction", "split_ellipsis"].includes(citation.quotation_match ?? "")) ? (en ? "A joined quotation was split into source passages; check whether they support the finding." : "拼接引文已拆成原文片段；请核对是否支持该结论。") : claim.citations?.some((citation) => citation.partial_token) ? (en ? "A quotation cuts through a word or number; check the original context." : "引文截断了单词或数字，请核对原文上下文。") : (en ? "Some quotations could not be located and were omitted." : "部分引文无法定位，已省略。")}</p> : null}{claim.support_review && originalClaims.has(JSON.stringify(claim)) ? <p><small>{en ? "Model review: " : "模型复查："}{en ? claim.support_review.verdict : ({ supports: "认为支持", does_not_support: "认为不支持", contradicts: "发现矛盾", uncertain: "无法判断" } as Record<string, string>)[claim.support_review.verdict]}{en ? "; not independently verified." : "；尚未独立核验。"}</small></p> : null}{Boolean(current.content.run.support_review_requested) && (!claim.support_review || !originalClaims.has(JSON.stringify(claim))) ? <p role="status">{en ? "No model review result is saved for this finding; it may not have been covered, the review may have failed, or the finding may have changed since the run." : "此结论没有已保存的模型复查结果；可能未被覆盖、复查失败，或结论在运行后已修改。"}{current.created_by === "agent" && originalClaims.has(JSON.stringify(claim)) && reviewOmissionHint(current.content.run, index, en) ? <> {reviewOmissionHint(current.content.run, index, en)}</> : null}</p> : null}{claim.citations?.map((citation, citationIndex) => <blockquote key={citationIndex}>{citation.quotation ?? ""}<small> {citation.source_id}{citation.location?.page ? ` · ${en ? "page" : "第"} ${citation.location.page}${en ? "" : " 页"}` : ""}</small>{citation.partial_token ? <small> {en ? "Quotation cuts through a word or number." : "引文截断了单词或数字。"}</small> : null}{citation.quotation_match === "table_markup_expanded" ? <small> {en ? "Full original table row restored; check column alignment." : "已恢复完整原始表格行；请核对列对齐。"}</small> : null}{citation.quotation_match === "expanded_ellipsis" ? <small> {en ? "Ellipsis expanded to the continuous source text." : "已将省略号展开为连续原文。"}</small> : null}{citation.quotation_match === "trimmed_json_closure" ? <small> {en ? "A model-added closing brace was removed; check the source context." : "已移除模型添加的结束括号，请核对原文上下文。"}</small> : null}{["split_conjunction", "split_ellipsis"].includes(citation.quotation_match ?? "") ? <small> {en ? "One of the separately located passages from a joined quotation." : "拼接引文中分别定位的一段原文。"}</small> : null}<SourceContext key={`${current.version_id}:${citation.source_id}:${citation.location?.character_start}:${citation.location?.character_end}`} sourceId={citation.source_id} location={citation.location} quotation={citation.quotation} en={en} load={api.sourceContext} onUseContext={editingClaims && isLatest && !running && !busy && citation.location?.status === "ambiguous" ? (quotation) => setClaims((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, citations: (item.citations ?? []).map((existing, itemCitationIndex) => itemCitationIndex === citationIndex ? { source_id: existing.source_id, quotation } : existing) } : item)) : undefined} />{editingClaims && isLatest && !running ? <Button disabled={busy} onClick={() => setClaims((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, citations: (item.citations ?? []).filter((_, itemCitationIndex) => itemCitationIndex !== citationIndex) } : item))}>{en ? "Remove quotation" : "移除引文"}</Button> : null}</blockquote>)}{editingClaims && isLatest && !running ? <CitationEditor loadSource={(sourceId) => api.sourceText(topic.topic_id, current.version_id, sourceId)} sources={sourceMetadata} asOf={topic.request.as_of} disabled={busy || (claim.citations?.length ?? 0) >= 10} en={en} onAdd={(sourceId, quotation) => setClaims((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, citations: [...(item.citations ?? []), { source_id: sourceId, quotation }] } : item))} /> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.rate_unit_warnings?.length ? <p role="status">{en ? "Possible unit confusion: interest rate levels are usually percentages; percentage points describe changes or differences. Check the wording and source." : "可能混淆单位：利率水平通常用百分比，百分点用于表示变化或差额；请核对原文和表述。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.publication_date_warnings?.length ? <div role="status"><p>{en ? "Publication date needs review. The source date is user-declared and may be wrong." : "发布日期需要核查；来源时间为用户声明，也可能有误。"}</p><ul>{claim.publication_date_warnings.map((warning, warningIndex) => <li key={`${warning.source_id}:${warningIndex}`}>{warning.claimed_text} · {en ? "declared source time" : "来源声明时间"} {warning.declared_published_at}</li>)}</ul></div> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.citation_context_warnings?.length ? <div><p role="status">{en ? "This quotation may omit relevant source context: " : "引文可能缺少相关的原文上下文："}{claim.citation_context_warnings.map((warning) => en ? warning.context_metric : ({ "GDP": "国内生产总值", "GDI": "国内生产总收入", "personal saving rate": "个人储蓄率", "PCE price index": "个人消费支出物价指数", "financial table header": "财务表格表头", "unfinished quoted phrase": "未完整引用的短语", "numeric quotation subject": "金额前的原文指标与期间上下文", "personal-pronoun antecedent": "代词所指的原文主语和上下文", "named entity prefix": "引文前的机构名称", "possessive entity": "引文前的所属实体", "relative-clause antecedent": "关系从句所指的原文主语", "related unquoted sentence": "可能相关但未引用的相邻原文" } as Record<string, string>)[warning.context_metric] ?? warning.context_metric).join("、")}。{en ? " Check the original context and whether it supports the finding." : "请核对原文上下文是否支持结论。"}</p>{editingClaims && isLatest && !running && !busy && (claim.citations?.length ?? 0) < 10 ? claim.citation_context_warnings.map((warning, warningIndex) => { const source = sourceMetadata.find((item) => item.source_id === warning.source_id); if (!source || !canUseCitationSource(source, topic.request.as_of)) return null; return <SourceContext key={`${warning.source_id}:${warningIndex}:${warning.context_character_start}:${warning.context_character_end}`} sourceId={warning.source_id} location={{ ...(typeof warning.context_character_start === "number" ? { character_start: warning.context_character_start } : {}), ...(typeof warning.context_character_end === "number" ? { character_end: warning.context_character_end } : {}) }} en={en} load={api.sourceContext} onAddSelected={(quotation) => setClaims((items) => items.map((item, itemIndex) => { if (itemIndex !== index) return item; const citations = item.citations ?? []; if (citations.length >= 10 || citations.some((existing) => existing.source_id === warning.source_id && existing.quotation === quotation)) return item; return { ...item, citations: [...citations, { source_id: warning.source_id, quotation }] }; }))} />; }) : null}</div> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.balance_direction_warnings?.some((warning) => !warning.basis || warning.basis === "trade_deficit") ? <p role="status">{en ? "Possible direction inconsistency: a smaller services surplus increases the combined deficit if other components are unchanged. Check definitions, signs, periods and the source." : "可能存在方向不一致：在其他部分不变时，服务顺差减少会扩大合计逆差。请核对定义、正负号、期间与原文。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.balance_direction_warnings?.some((warning) => warning.basis === "revision_vs_change") ? <p role="status">{en ? "A revision to an estimate does not by itself establish that the underlying measure fell or contradicts growth. Check the original estimate, actual change, period and other evidence." : "估计值的修订本身不能证明指标实际下降，也不能直接反驳增长。请核对原估计、实际变化、期间和其他证据。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.balance_direction_warnings?.some((warning) => warning.basis === "operating_profit") ? <p role="status">{en ? "Possible direction inconsistency: lower revenue reduces operating profit if other components are unchanged. Check the accounting basis, signs, periods and the source." : "可能存在方向不一致：在其他部分不变时，收入减少会降低营业利润。请核对会计口径、正负号、期间与原文。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_quarters?.length ? <p role="status">{en ? "These quarter and year labels could not be matched in the attached quotations: " : "所附引文未匹配以下季度与年份："}{claim.uncited_quarters.join("、")}。{en ? "Check the period and source context; matching labels do not establish support." : "请核对期间及原文上下文；标签匹配也不代表引文支持结论。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_dollar_amounts?.length ? <p role="status">{en ? "These dollar-symbol amounts could not be matched in the attached quotations: " : "所附引文未匹配以下带 $ 符号的金额："}{claim.uncited_dollar_amounts.join("、")}。{en ? "Check currency, units, the original source and any calculation. A numeric match does not establish support." : "请核对币种、单位、原文及可能的计算；数值匹配也不代表引文支持结论。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_claim_amounts?.length ? <p role="status">{en ? "These USD amounts could not be confirmed from the attached quotations: " : "无法从所附引文确认以下美元金额："}{claim.uncited_claim_amounts.join("、")}。{en ? "Check currency, units, the original source and any calculation. A numeric match does not establish support." : "请核对币种、单位、原文与可能的计算；数值匹配也不代表引文支持结论。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_amounts?.length ? <p role="status">{en ? "The matched fact passages for these amounts are not fully covered by the attached quotations: " : "所附引文未完整覆盖以下金额的匹配事实片段："}{claim.uncited_amounts.join("、")}。{en ? "Check the candidate sources and context; number matches alone do not establish support." : "请核对候选来源与上下文；数值匹配不代表引文支持结论。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_fact_values?.length ? <p role="status">{en ? "The cited sources contain matching figures outside the attached quotations: " : "已引用的来源中有以下匹配数值，但所附引文未覆盖对应片段："}{claim.uncited_fact_values.join("、")}。{en ? "Review the candidate passages and their meaning; a numeric match does not establish support." : "请核对候选原文及其含义；数值相同不代表支持该结论。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_percentage_points?.length ? <p role="status">{en ? "These percentage-point figures do not appear in the attached quotations: " : "以下百分点数值未出现在所附引文中："}{claim.uncited_percentage_points.join("、")}。{en ? "Check the source and any calculation." : "请核对来源与可能的计算。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_basis_points?.length ? <p role="status">{en ? "These basis-point figures do not appear in the attached quotations: " : "以下基点数值未出现在所附引文中："}{claim.uncited_basis_points.join("、")}。{en ? " Check the source and any calculation." : "请核对来源与可能的计算。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_percentage_bounds?.length ? <p role="status">{en ? "The attached quotations do not establish these strict upper bounds: " : "所附引文未证明以下严格上限："}{claim.uncited_percentage_bounds.join("、")}。{en ? "Check the comparison qualifier; a threshold is not an exact value." : "请核对“小于”等限定词，上限不等于精确值。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.reused_percentages?.length ? <p role="status">{en ? "This percentage occurs more than once in the finding: " : "以下百分比在同一结论中多次出现："}{claim.reused_percentages.join("、")}。{en ? "Check that each measure has its own supporting quotation; matching numbers can refer to different measures." : "请分别核对每个指标的依据；相同数值可能对应不同指标。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.uncited_percentages?.length ? <p role="status">{en ? "These percentages do not appear in the attached quotations: " : "以下百分比未出现在所附引文中："}{claim.uncited_percentages.join("、")}。{en ? "They may be calculated; check the calculation and source. Numeric matches alone do not prove support." : "它们可能来自计算，请核对计算和来源；数值匹配也不代表引文支持结论。"}</p> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.suggested_calculations?.length ? <details className="general-source"><summary>{en ? "Possibly related calculations" : "可能相关的计算"}</summary><p>{en ? "The value matches a calculation whose inputs are covered by the attached quotations. Check the entity, period, formula and meaning; this does not verify the finding." : "数值与某项计算相符，计算输入包含在所附引文中。请核对对象、期间、公式与含义；这不代表结论已核验。"}</p><ul>{claim.suggested_calculations.map((candidate, candidateIndex) => <li key={`${candidate.calculation_index}:${candidateIndex}`}>{candidate.claimed_percentage} · {candidate.formula_id} · {candidate.input_fact_ids.join(" / ")} · {en ? "calculation" : "计算"} {candidate.calculation_index + 1}</li>)}</ul></details> : null}{!hasUnsavedReview && originalClaims.has(JSON.stringify(claim)) && claim.suggested_related_quotations?.length ? <details className="general-source"><summary>{en ? "Numbers found in other findings" : "其他研究条目中的相同数值"}</summary><p>{en ? "These passages match a number only. Check the company, unit, currency, period and meaning; they may not support this finding." : "这些原文仅匹配数值，请核对公司、单位、币种、期间和含义；它们未必支持此结论。"}</p>{editingClaims && isLatest && !running ? <Button disabled={busy || (claim.citations?.length ?? 0) >= 10} onClick={() => setClaims((items) => items.map((item, itemIndex) => { if (itemIndex !== index) return item; const citations = [...(item.citations ?? [])]; for (const candidate of item.suggested_related_quotations ?? []) { if (citations.length >= 10) break; if (!citations.some((citation) => citation.source_id === candidate.source_id && citation.quotation === candidate.quotation)) citations.push({ source_id: candidate.source_id, quotation: candidate.quotation }); } return { ...item, citations }; }))}>{en ? "Add candidate quotations" : "添加候选引文"}</Button> : null}{claim.suggested_related_quotations.map((candidate, candidateIndex) => <blockquote key={`${candidate.source_id}:${candidateIndex}`}>{candidate.quotation}<small> {candidate.matched_amounts.join(" / ")} · {en ? "finding" : "研究条目"} {candidate.from_claim_index + 1} · {candidate.source_id}</small><SourceContext sourceId={candidate.source_id} location={candidate.location} quotation={candidate.quotation} en={en} load={api.sourceContext} /></blockquote>)}</details> : null}{!hasUnsavedFacts && originalClaims.has(JSON.stringify(claim)) && claim.suggested_fact_evidence?.length ? <details className="general-source"><summary>{en ? "Possibly related numeric evidence" : "可能相关的数值来源"}</summary><p>{en ? "These links match a number only; entity, period and whether the source supports the finding require review." : "仅按数值匹配；公司、期间及资料是否支持结论仍需核查。"}</p>{editingClaims && isLatest && !running ? <Button disabled={busy || (claim.citations?.length ?? 0) >= 10} onClick={() => setClaims((items) => items.map((item, itemIndex) => { if (itemIndex !== index) return item; const citations = [...(item.citations ?? [])]; for (const evidence of item.suggested_fact_evidence ?? []) { if (citations.length >= 10) break; if (!citations.some((citation) => citation.source_id === evidence.source_id && citation.quotation === evidence.quotation)) citations.push({ source_id: evidence.source_id, quotation: evidence.quotation }); } return { ...item, citations }; }))}>{en ? "Add candidate quotations" : "添加候选引文"}</Button> : null}{claim.suggested_fact_evidence.map((item, itemIndex) => <blockquote key={`${item.fact_id}:${itemIndex}`}>{item.quotation}<small> {item.value} {item.unit} · {item.period} · {item.source_id}</small><SourceContext sourceId={item.source_id} location={item.location} quotation={item.quotation} en={en} load={api.sourceContext} /></blockquote>)}</details> : null}</article>) : <p>{en ? "No findings yet." : "尚无研究结论。"}</p>}
      {editingClaims && isLatest && !running ? <div className="general-actions"><Button disabled={busy || claims.length >= 2000} onClick={() => setClaims((items) => [...items, { text: "", role: "support", citations: [] }])}>{en ? "Add finding" : "添加结论"}</Button><Button disabled={busy || claims.length >= 2000} onClick={() => setClaims((items) => [...items, { text: "", role: "counterargument", citations: [] }])}>{en ? "Add counterargument" : "添加反方"}</Button></div> : null}
      <h3>{en ? "Gaps and limitations" : "缺口与限制"}</h3><ul>{current.content.limitations.map((item, index) => <li key={index}>{localizeLimitation(item, en)}</li>)}</ul>
      {current.created_by === "agent" && (current.content.summary || current.note) ? <p>{en ? "Your summary and review notes were carried forward from the previous version and have not been reviewed against this run." : "总结和复核意见沿用自上一版本，尚未结合本次研究重新确认。"}</p> : null}
      <Field label={en ? "Your summary" : "你的总结"}><Textarea value={summary} disabled={!isLatest || running} onChange={(event) => setSummary(event.target.value)} /></Field>
      <Field label={en ? "Review notes" : "复核意见"}><Textarea value={note} disabled={!isLatest || running} onChange={(event) => setNote(event.target.value)} /></Field>
      <div className="general-actions"><Button disabled={busy || running || !isLatest} onClick={() => void save("draft")}>{en ? "Save draft" : "保存草稿"}</Button><Button disabled={busy || running || !isLatest} onClick={() => void save("confirmed")}>{en ? "Confirm" : "确认"}</Button><Button disabled={busy || running || !isLatest} onClick={() => void save("reservation")}>{en ? "Save reservation" : "保留异议"}</Button><Button disabled={busy} onClick={() => void exportResearch(false, true)}>{en ? "Export readable report" : "导出可读报告"}</Button><Button disabled={busy} onClick={() => void exportResearch(false)}>{en ? "Export research JSON" : "导出研究 JSON"}</Button><Button disabled={busy} onClick={() => void exportResearch(true)}>{en ? "Export record only" : "导出记录摘要"}</Button></div>
      <p>{en ? "The record-only export excludes research text and source content." : "记录摘要不包含研究正文和来源内容。"}</p>
    </section> : null}
  </section>;
}
