const MARKDOWN_LINE_RE = /^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s?|```|~~~|(?:-{3,}|_{3,}|\*{3,})\s*$|\[\[(?:chart|table):[A-Za-z0-9_-]{1,64}\]\]\s*$)/m;
const MARKDOWN_INLINE_RE = /(?:\*\*|~~|`|\[[^\]]*\]|\||(?:^|[\s(>])\*[^*\s])/;
const FENCE_OPEN_RE = /^\s*(`{3,}|~{3,})(.*)$/;
const TABLE_SEP_RE = /^\s*\|?[\s:]*-{3,}[\s:|]*$/;
const PARTIAL_SEP_RE = /^\s*\|[\s:-]*$/;
const LIST_OR_QUOTE_PREFIX_RE = /^\s*(?:[-*+]|\d+\.|>)\s/;

export interface StreamingBlockSplit {
  stableBlocks: string[];
  activeTail: string;
}

/** Fast heuristic: checks whether text uses Markdown syntax (excluding single `$` currency). */
export function hasMarkdownSyntax(text: string): boolean {
  if (!text) return false;
  return MARKDOWN_LINE_RE.test(text) || MARKDOWN_INLINE_RE.test(text);
}

function isFenceClose(line: string, fenceChar: string, fenceLen: number): boolean {
  const trimmed = line.trim();
  if (trimmed.length < fenceLen) return false;
  for (let i = 0; i < trimmed.length; i += 1) {
    if (trimmed[i] !== fenceChar) return false;
  }
  return true;
}

/**
 * Splits streaming Markdown at top-level blank lines into immutable `stableBlocks`
 * and a live `activeTail`, never cutting inside fenced code blocks or contiguous tables.
 */
export function splitStableMarkdownBlocks(text: string): StreamingBlockSplit {
  if (!text) return { stableBlocks: [], activeTail: "" };
  const normalized = text.replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");

  let fenceChar: string | null = null;
  let fenceLen = 0;
  let inListOrQuote = false;
  const completed: string[] = [];
  let blockLines: string[] = [];

  const flushBlock = () => {
    const joined = blockLines.join("\n").trim();
    if (joined) completed.push(joined);
    blockLines = [];
    inListOrQuote = false;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]!;
    if (fenceChar !== null) {
      blockLines.push(line);
      if (isFenceClose(line, fenceChar, fenceLen)) {
        fenceChar = null;
        fenceLen = 0;
      }
      continue;
    }

    const fenceMatch = FENCE_OPEN_RE.exec(line);
    if (fenceMatch) {
      const marker = fenceMatch[1]!;
      const info = fenceMatch[2] ?? "";
      // CommonMark: backtick info strings cannot contain backticks.
      if (!(marker[0] === "`" && info.includes("`"))) {
        fenceChar = marker[0]!;
        fenceLen = marker.length;
        blockLines.push(line);
        continue;
      }
    }

    if (line.trim() === "") {
      // Keep multi-paragraph lists/blockquotes together when the next non-blank line continues them.
      let nextNonBlank: string | null = null;
      for (let j = i + 1; j < lines.length; j += 1) {
        if (lines[j]!.trim() !== "") {
          nextNonBlank = lines[j]!;
          break;
        }
      }
      if (
        inListOrQuote
        && nextNonBlank !== null
        && (LIST_OR_QUOTE_PREFIX_RE.test(nextNonBlank) || /^(?: {2,}|\t)/.test(nextNonBlank))
      ) {
        blockLines.push(line);
        continue;
      }
      flushBlock();
      continue;
    }

    if (LIST_OR_QUOTE_PREFIX_RE.test(line)) {
      inListOrQuote = true;
    } else if (blockLines.length === 0) {
      inListOrQuote = false;
    }
    blockLines.push(line);
  }

  const activeTail = blockLines.join("\n").replace(/^\n+/, "");
  return { stableBlocks: completed, activeTail };
}

function repairDanglingLinkOrImage(text: string): string {
  // Trailing unclosed image: `![alt](https://...` or `![alt`
  const unclosedImgParen = /!\[([^\]\n]*)\]\([^)\n]*$/u.exec(text);
  if (unclosedImgParen) {
    const alt = unclosedImgParen[1]!.trim();
    return text.slice(0, unclosedImgParen.index) + (alt ? `[图片：${alt}]` : "[图片]");
  }
  const unclosedImgBracket = /!\[([^\]\n]*)$/u.exec(text);
  if (unclosedImgBracket) {
    const alt = unclosedImgBracket[1]!.trim();
    return text.slice(0, unclosedImgBracket.index) + (alt ? `[图片：${alt}]` : "[图片]");
  }
  // Trailing unclosed link: `[label](https://...` or `[label` (ignore `[[chart:...]]`)
  const unclosedLinkParen = /(?<!\[)\[([^\]\n]+)\]\([^)\n]*$/u.exec(text);
  if (unclosedLinkParen) {
    return text.slice(0, unclosedLinkParen.index) + unclosedLinkParen[1]!;
  }
  const unclosedLinkBracket = /(?<!\[)\[([^\]\n]*)$/u.exec(text);
  if (unclosedLinkBracket) {
    return text.slice(0, unclosedLinkBracket.index) + unclosedLinkBracket[1]!;
  }
  return text;
}

function repairTrailingInlineFormatting(line: string): string {
  let out = line;
  // Find active inline code span parity on this single line.
  let openTicks = 0;
  let i = 0;
  while (i < out.length) {
    if (out[i] === "\\") {
      i += 2;
      continue;
    }
    if (out[i] === "`") {
      let run = 1;
      while (i + run < out.length && out[i + run] === "`") run += 1;
      if (openTicks === 0) openTicks = run;
      else if (openTicks === run) openTicks = 0;
      i += run;
      continue;
    }
    i += 1;
  }
  if (openTicks > 0) {
    // Strip empty dangling backtick run or close active inline code span.
    if (new RegExp(`(?<!\\\\)\`{${openTicks}}$`).test(out)) {
      return out.slice(0, out.length - openTicks);
    }
    return out + "`".repeat(openTicks);
  }

  // Track `**`, `~~`, and `*` outside inline code spans and list bullet prefixes.
  let boldOpen = false;
  let strikeOpen = false;
  let italicOpen = false;
  let inCode = 0;
  const startOfLine = /^\s*\*\s+/.exec(out);
  let idx = startOfLine ? startOfLine[0].length : 0;
  // Also preserve thematic break (`***`) without tampering.
  if (/^\s*(?:\*{3,}|-{3,}|_{3,})\s*$/.test(out)) return out;

  while (idx < out.length) {
    const ch = out[idx]!;
    if (ch === "\\") {
      idx += 2;
      continue;
    }
    if (ch === "`") {
      let run = 1;
      while (idx + run < out.length && out[idx + run] === "`") run += 1;
      if (inCode === 0) inCode = run;
      else if (inCode === run) inCode = 0;
      idx += run;
      continue;
    }
    if (inCode > 0) {
      idx += 1;
      continue;
    }
    if (ch === "*" && out[idx + 1] === "*") {
      boldOpen = !boldOpen;
      idx += 2;
      continue;
    }
    if (ch === "~" && out[idx + 1] === "~") {
      strikeOpen = !strikeOpen;
      idx += 2;
      continue;
    }
    if (ch === "*") {
      // Only treat `*` as emphasis if not just whitespace around an arithmetic multiplication.
      const prev = idx > 0 ? out[idx - 1]! : " ";
      const next = idx + 1 < out.length ? out[idx + 1]! : " ";
      const opening = !italicOpen && !/\s/.test(next);
      const closing = italicOpen && !/\s/.test(prev);
      if (opening || closing) italicOpen = !italicOpen;
      idx += 1;
      continue;
    }
    idx += 1;
  }

  // Never emit `****` when `**` opened with empty body at line tail; strip dangling opener instead.
  if (boldOpen) {
    if (out.endsWith("**")) out = out.slice(0, -2);
    else out += "**";
  }
  if (strikeOpen) {
    if (out.endsWith("~~")) out = out.slice(0, -2);
    else out += "~~";
  }
  if (italicOpen) {
    if (out.endsWith("*") && !out.endsWith("**")) out = out.slice(0, -1);
    else out += "*";
  }
  return out;
}

function repairIncompleteTable(block: string): string {
  const lines = block.split("\n");
  if (lines.length === 0) return block;
  const header = lines[0]!;
  if (!/^\s*\|/.test(header)) return block;
  const pipeCount = (header.match(/(?<!\\)\|/g) ?? []).length;
  if (pipeCount < 2) return block;
  const cells = header
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split(/(?<!\\)\|/);
  if (cells.length === 0) return block;
  const sepRow = `| ${cells.map(() => "---").join(" | ")} |`;

  if (lines.length === 1) {
    return `${header}\n${sepRow}`;
  }
  if (!TABLE_SEP_RE.test(lines[1]!) && PARTIAL_SEP_RE.test(lines[1]!)) {
    return [header, sepRow, ...lines.slice(2)].join("\n");
  }
  return block;
}

/**
 * Repairs unclosed Markdown constructs at the end of a streaming `activeTail`
 * to prevent raw markers, broken tables, or bracket flicker while tokens arrive.
 * Never alters identifiers using underscores (e.g. `sec_company_concept`) or `$130.5B`.
 */
export function repairIncompleteMarkdown(tail: string): string {
  if (!tail) return "";
  const normalized = tail.replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");

  let fenceChar: string | null = null;
  let fenceLen = 0;
  for (const line of lines) {
    if (fenceChar === null) {
      const m = FENCE_OPEN_RE.exec(line);
      if (m) {
        const marker = m[1]!;
        const info = m[2] ?? "";
        if (!(marker[0] === "`" && info.includes("`"))) {
          fenceChar = marker[0]!;
          fenceLen = marker.length;
        }
      }
    } else if (isFenceClose(line, fenceChar, fenceLen)) {
      fenceChar = null;
      fenceLen = 0;
    }
  }

  if (fenceChar !== null) {
    const suffix = normalized.endsWith("\n") ? fenceChar.repeat(fenceLen) : `\n${fenceChar.repeat(fenceLen)}`;
    return `${normalized}${suffix}`;
  }

  let repaired = repairDanglingLinkOrImage(normalized);
  const repairedLines = repaired.split("\n");
  const lastIdx = repairedLines.length - 1;
  repairedLines[lastIdx] = repairTrailingInlineFormatting(repairedLines[lastIdx]!);
  repaired = repairedLines.join("\n");
  return repairIncompleteTable(repaired);
}
