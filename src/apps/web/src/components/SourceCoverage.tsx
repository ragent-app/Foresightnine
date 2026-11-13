import type { ResearchContent } from "../general-research-client";

interface Props { sources: ResearchContent["sources"]; en: boolean }

export function SourceCoverage({ sources, en }: Props) {
  if (!sources.length) return null;
  return <section className="general-source">
    <h3>{en ? "Initial source selection" : "首次分析的资料选取范围"}</h3>
    <p>{en ? "Counts refer to the extracted text selected for the initial model prompt, not the original document layout or proof that the model understood it. Later follow-ups may use different passages." : "这里统计首次模型输入中选取的提取文字，不等同于原文件版面，也不证明模型理解了内容；后续补充可能使用不同片段。"}</p>
    <ul>{sources.map((source) => {
      const coverage = source.initial_prompt_coverage;
      const known = coverage?.method === "initial_prompt_ranges_v1" &&
        (coverage.status === "complete" || coverage.status === "partial") &&
        Number.isSafeInteger(coverage.text_characters) && Number.isSafeInteger(coverage.selected_characters) &&
        coverage.text_characters > 0 && coverage.selected_characters !== null &&
        coverage.selected_characters >= 0 && coverage.selected_characters <= coverage.text_characters;
      return <li key={source.source_id}><strong>{source.title || source.source_id}</strong>{": "}
        {known && coverage ? <>{coverage.selected_characters?.toLocaleString()} / {coverage.text_characters.toLocaleString()} {en ? "extracted characters selected" : "个提取字符被选入"} {coverage.status === "partial" ? (en ? "(partial)" : "（部分）") : (en ? "(complete extracted text)" : "（完整提取文本）")}</> : (en ? "selection coverage not recorded" : "未记录选取范围")}
      </li>;
    })}</ul>
  </section>;
}
