import type { ResearchV2 } from "@foresightnine/contracts";
import { answerBody, draftNote } from "./answerBody";
import type { AvailableVersionSource } from "../api/types";
import { claimRole, ROLE_LABEL, type CitationIndex } from "./research";

type Claim = ResearchV2.ResearchClaim;
type Citation = NonNullable<Claim["citations"]>[number];

export interface AnswerCopyContext {
  /** Every claim in the version; report markers may point at any of them. */
  claims: readonly Claim[];
  /** The claims that belong to this turn. */
  turnClaims: readonly Claim[];
  tables: readonly ResearchV2.ResearchTable[];
  charts: readonly ResearchV2.ResearchChart[];
  citations: CitationIndex;
  sources: ReadonlyMap<string, AvailableVersionSource>;
  sourceTitle: (sourceId: string) => string;
}

const CLAIM_MARKER = /\[\[claim:([A-Za-z0-9_-]{1,100})\]\]/g;
const ANCHOR_LINE = /^\s*\[\[(chart|table):([A-Za-z0-9_-]{1,64})\]\]\s*$/;
const FENCE = /^\s*(`{3,}|~{3,})/;

function cell(value: string): string {
  return value.replace(/\|/g, "\\|").replace(/\r?\n/g, " ").trim();
}

export function tableMarkdown(table: ResearchV2.ResearchTable): string {
  const head = ["", ...table.columns.map((column) => `${column.label}${column.unit ? ` (${column.unit})` : ""}`)];
  const lines = [
    `| ${head.map(cell).join(" | ")} |`,
    `| ${head.map((_, index) => (index === 0 ? "---" : "---:")).join(" | ")} |`,
    ...table.rows.map((row) => `| ${[row.label ?? "", ...table.columns.map((column) => row.cells[column.key]?.display_value ?? "—")].map(cell).join(" | ")} |`),
  ];
  return [`**${table.title}**${table.subtitle ? ` — ${table.subtitle}` : ""}`, "", ...lines].join("\n");
}

/**
 * Copies a research answer as Markdown that still makes sense outside the app: claim markers become
 * citation numbers, and a source list follows. Quotations are only reproduced when the source may be
 * both displayed and exported; otherwise the list names the source without its text.
 */
export function answerMarkdown(turn: ResearchV2.ResearchTurn, context: AnswerCopyContext): string {
  const used = new Map<number, Citation>();
  const byId = new Map(context.claims.map((claim) => [claim.claim_id ?? "", claim]));
  const referenced = new Set<string>();

  const refsFor = (claim: Claim | undefined): string => {
    if (!claim) return "";
    const numbers: number[] = [];
    for (const citation of claim.citations ?? []) {
      const n = context.citations.numberFor(citation);
      if (n <= 0 || numbers.includes(n)) continue;
      numbers.push(n);
      if (!used.has(n)) used.set(n, citation);
    }
    return numbers.sort((a, b) => a - b).map((n) => `[${n}]`).join("");
  };

  const parts: string[] = [];
  const body = answerBody(turn);
  const report = body?.text ?? "";
  if (body?.kind === "draft") parts.push(`> ${draftNote(context.turnClaims.length > 0)}`);
  if (report) {
    const out: string[] = [];
    let fenced = false;
    for (const line of report.replace(/\r\n?/g, "\n").split("\n")) {
      if (FENCE.test(line)) fenced = !fenced;
      if (fenced || FENCE.test(line)) { out.push(line); continue; }
      const anchor = ANCHOR_LINE.exec(line);
      if (anchor) {
        const [, kind, id] = anchor;
        if (kind === "table") {
          const table = context.tables.find((item) => item.table_id === id);
          if (table) out.push(tableMarkdown(table));
        } else {
          const chart = context.charts.find((item) => item.chart_id === id);
          if (chart) out.push(`（图表：${chart.title}）`);
        }
        continue;
      }
      out.push(line.replace(CLAIM_MARKER, (_, claimId: string) => {
        referenced.add(claimId);
        return refsFor(byId.get(claimId));
      }));
    }
    parts.push(out.join("\n").trim());
  } else {
    const supporting = context.turnClaims.filter((claim) => claimRole(claim) === "support");
    if (supporting.length > 0) {
      parts.push(supporting.map((claim) => {
        referenced.add(claim.claim_id ?? "");
        return `- ${claim.text}${refsFor(claim)}`;
      }).join("\n"));
    }
  }

  const inlineTables = new Set([...report.matchAll(/\[\[table:([A-Za-z0-9_-]{1,64})\]\]/g)].map((match) => match[1]));
  for (const table of context.tables) {
    if ((turn.table_ids ?? []).includes(table.table_id) && !inlineTables.has(table.table_id)) parts.push(tableMarkdown(table));
  }

  const others = context.turnClaims.filter((claim) => claimRole(claim) !== "support" && !referenced.has(claim.claim_id ?? ""));
  if (others.length > 0) {
    parts.push(["## 反方与待复核", ...others.map((claim) => `- ${ROLE_LABEL[claimRole(claim)]}：${claim.text}${refsFor(claim)}`)].join("\n"));
  }

  if (used.size > 0) {
    const lines = [...used.entries()].sort(([a], [b]) => a - b).map(([n, citation]) => {
      const source = context.sources.get(citation.source_id);
      const title = source?.title ?? context.sourceTitle(citation.source_id);
      const simulated = source?.is_simulated ? "（模拟数据）" : "";
      const location = `字符 ${citation.location.character_start}–${citation.location.character_end}`;
      if (source && source.permissions.display && source.permissions.export) {
        return `[${n}] 《${title}》${simulated}：“${citation.quotation.replace(/\s+/g, " ").trim()}”（${location}）`;
      }
      return `[${n}] 《${title}》${simulated}（该资料不允许导出原文）`;
    });
    parts.push(["## 来源", ...lines].join("\n"));
    parts.push("> 引文位置和数字已由系统核对；结论是否被引文充分支持，需要自行判断。");
  }

  return parts.filter(Boolean).join("\n\n").trim();
}

/** A readable plain-text form of the Markdown above, for pasting where Markdown is not rendered. */
export function markdownToPlain(markdown: string): string {
  return markdown
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .filter((line) => !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line))
    .map((line) => {
      let out = line;
      if (/^\s*\|.*\|\s*$/.test(out)) out = out.trim().slice(1, -1).split(/(?<!\\)\|/).map((part) => part.trim()).join("\t").replace(/\\\|/g, "|");
      return out
        .replace(/^\s{0,3}#{1,6}\s+/, "")
        .replace(/^\s*>\s?/, "")
        .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, "$1（$2）")
        .replace(/(\*\*|__)(.+?)\1/g, "$2")
        .replace(/(^|[^*])\*(?!\s)([^*]+?)\*(?!\*)/g, "$1$2")
        .replace(/`([^`]+)`/g, "$1")
        .replace(/~~(.+?)~~/g, "$1");
    })
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
