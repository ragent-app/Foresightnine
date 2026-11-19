import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateResearch, useTopics } from "../api/queries";
import type { CreatedSource } from "../api/types";
import { formatDateTime } from "../lib/format";
import { Composer, type ComposerHandle, type ComposerToken } from "../ui/Composer";
import { useLayout } from "../ui/AppLayout";
import { NetworkBanner, Toolbar } from "../ui/Toolbar";
import { describeError } from "./research/errors";
import { AddSourcesPanel, type AddSourceInitial } from "./research/AddSourcesPanel";
import { DEFAULT_OPTIONS, localInputToIso, optionsValid, runRequestFields, type RunOptions } from "./research/options";
import { optionTokens } from "./research/optionTokens";
import { ResearchSettingsPanel, type TopicFields } from "./research/ResearchSettingsPanel";

type HomePanel = { kind: "settings" } | { kind: "add"; initial?: AddSourceInitial } | null;

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
  const [panel, setPanel] = useState<HomePanel>(null);
  // Sources created before the topic exists; they become the topic's initial sources.
  const [staged, setStaged] = useState<CreatedSource[]>([]);

  useEffect(() => {
    setPanelOpen(panel !== null);
    return () => setPanelOpen(false);
  }, [panel, setPanelOpen]);

  const tokens: ComposerToken[] = staged.map((source) => ({
    key: `source-${source.source_id}`,
    label: `资料：${source.title}`,
    removeLabel: `移除资料 ${source.title}`,
    onRemove: () => setStaged((list) => list.filter((item) => item.source_id !== source.source_id)),
  }));
  if (topic.asOf) tokens.push({ key: "asof", label: `截止 ${formatDateTime(localInputToIso(topic.asOf))}`, removeLabel: "移除截止时间", onRemove: () => setTopic({ ...topic, asOf: "" }) });
  if (topic.scope.trim()) tokens.push({ key: "scope", label: `范围：${topic.scope.trim()}`, removeLabel: "移除研究范围", onRemove: () => setTopic({ ...topic, scope: "" }) });
  tokens.push(...optionTokens(options, setOptions));

  const canSubmit = question.trim().length > 0 || staged.length > 0;
  const submit = () => {
    const text = question.trim();
    if (!canSubmit || create.isPending || !optionsValid(options)) return;
    const asOf = localInputToIso(topic.asOf);
    create.mutate(
      {
        // With sources and no question the backend asks for a document overview.
        input: {
          question: text,
          source_ids: staged.map((source) => source.source_id),
          ...(topic.scope.trim() ? { scope: topic.scope.trim() } : {}),
          ...(asOf ? { as_of: asOf } : {}),
        },
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
            onOpenSettings={() => setPanel({ kind: "settings" })}
            onAddSources={() => setPanel({ kind: "add" })}
            onDropFiles={(files) => { if (files[0]) setPanel({ kind: "add", initial: { tab: "file", file: files[0] } }); }}
            label="研究问题"
            placeholder={staged.length > 0 ? "写下问题；也可以直接开始，先概括这些资料" : "例如：比较一家公司最近两个财年的总收入，并说明主要变化来源"}
            submitLabel="开始研究"
            busy={create.isPending}
            canSubmit={canSubmit}
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
      {panel?.kind === "settings" ? (
        <ResearchSettingsPanel
          mode="new"
          options={options}
          topic={topic}
          onApply={(nextOptions, nextTopic) => { setOptions(nextOptions); setTopic(nextTopic); }}
          onClose={() => setPanel(null)}
        />
      ) : null}
      {panel?.kind === "add" ? (
        <AddSourcesPanel
          asOfIso={localInputToIso(topic.asOf)}
          initial={panel.initial}
          onAdded={async (source) => { setStaged((list) => (list.some((item) => item.source_id === source.source_id) ? list : [...list, source])); }}
          onClose={() => setPanel(null)}
        />
      ) : null}
    </>
  );
}
