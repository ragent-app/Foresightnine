import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateResearch, useTopics } from "../api/queries";
import { formatDateTime } from "../lib/format";
import { Composer, type ComposerHandle, type ComposerToken } from "../ui/Composer";
import { useLayout } from "../ui/AppLayout";
import { NetworkBanner, Toolbar } from "../ui/Toolbar";
import { describeError } from "./research/errors";
import { DEFAULT_OPTIONS, EMPTY_BUDGET, hasBudget, localInputToIso, optionsValid, runRequestFields, type RunOptions } from "./research/options";
import { ResearchSettingsPanel, type TopicFields } from "./research/ResearchSettingsPanel";

const EXAMPLES = [
  "比较一家公司最近两个财年的总收入和营业利润率",
  "核对一条新闻里的数字是否与公司公告一致",
  "总结一份报告的主要结论、关键数字和未解决的问题",
];

/** Spec §4.2: the research home doubles as the new-research entry. */
export function HomePage() {
  const navigate = useNavigate();
  const topics = useTopics();
  const create = useCreateResearch();
  const { setPanelOpen } = useLayout();
  const composerRef = useRef<ComposerHandle>(null);
  const [question, setQuestion] = useState("");
  const [topic, setTopic] = useState<TopicFields>({ asOf: "", scope: "" });
  const [options, setOptions] = useState<RunOptions>(DEFAULT_OPTIONS);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    setPanelOpen(settingsOpen);
    return () => setPanelOpen(false);
  }, [settingsOpen, setPanelOpen]);

  const tokens: ComposerToken[] = [];
  if (topic.asOf) tokens.push({ key: "asof", label: `截止 ${formatDateTime(localInputToIso(topic.asOf))}`, removeLabel: "移除截止时间", onRemove: () => setTopic({ ...topic, asOf: "" }) });
  if (topic.scope.trim()) tokens.push({ key: "scope", label: `范围：${topic.scope.trim()}`, removeLabel: "移除研究范围", onRemove: () => setTopic({ ...topic, scope: "" }) });
  if (options.discover === "off") tokens.push({ key: "search", label: "不联网搜索", removeLabel: "恢复联网搜索", onRemove: () => setOptions({ ...options, discover: "auto" }) });
  if (options.autoFetch === "off") tokens.push({ key: "fetch", label: "不自动获取官方资料", removeLabel: "恢复自动获取官方资料", onRemove: () => setOptions({ ...options, autoFetch: "auto" }) });
  if (hasBudget(options)) tokens.push({ key: "budget", label: "已设置预算", removeLabel: "移除预算", onRemove: () => setOptions({ ...options, budget: EMPTY_BUDGET }) });

  const submit = () => {
    const text = question.trim();
    if (!text || create.isPending || !optionsValid(options)) return;
    const asOf = localInputToIso(topic.asOf);
    create.mutate(
      {
        input: { question: text, source_ids: [], ...(topic.scope.trim() ? { scope: topic.scope.trim() } : {}), ...(asOf ? { as_of: asOf } : {}) },
        run: runRequestFields(options),
      },
      { onSuccess: (result) => navigate(`/research/${encodeURIComponent(result.topic.topic_id)}`) },
    );
  };

  const firstTime = topics.data !== undefined && topics.data.length === 0;
  return (
    <>
      <Toolbar scrolled={false} />
      <NetworkBanner />
      <div className="scroll" data-page="HomePage">
      <div className="home">
        <h1>你想研究什么？</h1>
        <div className="home-dock">
          <Composer
            ref={composerRef}
            variant="home"
            value={question}
            onChange={setQuestion}
            onSubmit={submit}
            onOpenSettings={() => setSettingsOpen(true)}
            label="研究问题"
            placeholder="例如：比较一家公司最近两个财年的总收入，并说明主要变化来源"
            submitLabel="开始研究"
            busy={create.isPending}
            canSubmit={question.trim().length > 0}
            tokens={tokens}
            error={create.isError ? describeError(create.error) : null}
          />
        </div>
        {firstTime ? (
          <div className="examples">
            {EXAMPLES.map((example) => (
              <button key={example} type="button" className="link" onClick={() => { setQuestion(example); composerRef.current?.focus(); }}>{example}</button>
            ))}
          </div>
        ) : null}
      </div>
      </div>
      {settingsOpen ? (
        <ResearchSettingsPanel
          mode="new"
          options={options}
          topic={topic}
          onApply={(nextOptions, nextTopic) => { setOptions(nextOptions); setTopic(nextTopic); }}
          onClose={() => setSettingsOpen(false)}
        />
      ) : null}
    </>
  );
}
