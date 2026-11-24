import {
  Children,
  Component,
  createContext,
  isValidElement,
  useContext,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { remarkCjkStrong } from "../lib/markdownCjk";
import { extractTableTsv, isNumericCellText, safeExternalHref } from "../lib/markdownSafety";
import {
  hasMarkdownSyntax,
  repairIncompleteMarkdown,
  splitStableMarkdownBlocks,
} from "../lib/markdownStream";
import { Icon } from "./icons";

export type MarkdownVariant = "report" | "draft" | "notes" | "inline";

/**
 * Lets a report show each [[claim:ID]] marker as the claim's citation numbers instead of a
 * text link. Without a provider the marker stays the plain “查看依据” link.
 */
export interface ClaimRefs {
  label: (claimId: string) => string | null;
  open: (claimId: string) => void;
}
export const ClaimRefContext = createContext<ClaimRefs | null>(null);

function ClaimLink({ href, children }: { href: string; children: ReactNode }) {
  const refs = useContext(ClaimRefContext);
  const claimId = href.slice("#claim-".length);
  const label = refs?.label(claimId) ?? null;
  if (!refs || !label) return <a href={href} className="md-link">{children}</a>;
  return (
    <a
      href={href}
      className="claim-ref"
      aria-label="查看依据"
      title="查看依据"
      onClick={(event) => { event.preventDefault(); refs.open(claimId); }}
    >
      {label}
    </a>
  );
}

export interface MarkdownContentProps<TChart = unknown> {
  content: string;
  variant: MarkdownVariant;
  streaming?: boolean;
  showCursor?: boolean;
  chartsById?: ReadonlyMap<string, TChart>;
  renderChart?: (chart: TChart) => ReactNode;
  tablesById?: ReadonlyMap<string, ReactNode>;
  className?: string;
}

const REPORT_ELEMENTS = [
  "h1", "h2", "h3", "h4", "h5", "h6",
  "p", "ul", "ol", "li", "blockquote",
  "pre", "code",
  "table", "thead", "tbody", "tr", "th", "td",
  "strong", "em", "del",
  "a", "img", "hr", "br",
] as const;

const NOTES_ELEMENTS = [
  "h1", "h2", "h3", "h4", "h5", "h6",
  "p", "ul", "ol", "li", "blockquote",
  "code", "strong", "em", "del",
  "a", "img", "br",
] as const;

const INLINE_ELEMENTS = ["p", "strong", "em", "del", "code", "img"] as const;

const REMARK_PLUGINS = [remarkGfm, remarkCjkStrong];
type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };

function remarkClaimLinks() {
  function replaceMarkers(node: MarkdownNode): void {
    if (!node.children || ["link", "linkReference", "code", "inlineCode"].includes(node.type)) return;
    node.children = node.children.flatMap((child): MarkdownNode[] => {
      if (child.type !== "text" || !child.value?.includes("[[claim:")) {
        replaceMarkers(child);
        return [child];
      }
      const pieces: MarkdownNode[] = [];
      let offset = 0;
      for (const match of child.value.matchAll(/\[\[claim:([A-Za-z0-9_-]{1,100})\]\]/g)) {
        pieces.push({ type: "text", value: child.value.slice(offset, match.index) });
        pieces.push({ type: "link", url: `#claim-${match[1]}`, children: [{ type: "text", value: "查看依据" }] });
        offset = match.index + match[0].length;
      }
      pieces.push({ type: "text", value: child.value.slice(offset) });
      return pieces;
    });
  }
  return replaceMarkers;
}

const REPORT_PLUGINS = [remarkGfm, remarkCjkStrong, remarkClaimLinks];
const CLAIM_FRAGMENT = /^#claim-[A-Za-z0-9_-]{1,100}$/;
const CHART_ANCHOR_RE = /^\s*\[\[(chart|table):([A-Za-z0-9_-]{1,64})\]\]\s*$/;
const FENCE_LINE_RE = /^\s*(`{3,}|~{3,})/;

type Segment = { kind: "md"; text: string } | { kind: "chart"; chartId: string } | { kind: "table"; chartId: string };

function splitChartSegments(raw: string): Segment[] {
  const normalized = raw.replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");
  const segments: Segment[] = [];
  let mdBuf: string[] = [];
  let fenceChar: string | null = null;
  let fenceLen = 0;

  const flushMd = () => {
    const joined = mdBuf.join("\n").trim();
    if (joined) segments.push({ kind: "md", text: joined });
    mdBuf = [];
  };

  for (const line of lines) {
    if (fenceChar !== null) {
      mdBuf.push(line);
      const trimmed = line.trim();
      if (trimmed.length >= fenceLen && [...trimmed].every((ch) => ch === fenceChar)) {
        fenceChar = null;
        fenceLen = 0;
      }
      continue;
    }
    const fence = FENCE_LINE_RE.exec(line);
    if (fence) {
      const marker = fence[1]!;
      fenceChar = marker[0]!;
      fenceLen = marker.length;
      mdBuf.push(line);
      continue;
    }
    const anchor = CHART_ANCHOR_RE.exec(line);
    if (anchor) {
      flushMd();
      segments.push({ kind: anchor[1] as "chart" | "table", chartId: anchor[2]! });
      continue;
    }
    mdBuf.push(line);
  }
  flushMd();
  return segments;
}

export function extractReferencedTableIds(raw: string): Set<string> {
  return new Set(splitChartSegments(raw).filter((segment) => segment.kind === "table").map((segment) => (segment as { chartId: string }).chartId));
}

/** Extracts chart IDs referenced via standalone `[[chart:id]]` lines outside fenced code blocks. */
export function extractReferencedChartIds(raw: string): Set<string> {
  const ids = new Set<string>();
  if (!raw || !raw.includes("[[chart:")) return ids;
  for (const segment of splitChartSegments(raw)) {
    if (segment.kind === "chart") ids.add(segment.chartId);
  }
  return ids;
}

function extractNodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractNodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractNodeText(node.props.children);
  }
  return "";
}

function safeCopyText(text: string, onDone: () => void): void {
  if (!text) return;
  const clipboard = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
  if (!clipboard?.writeText) return;
  void clipboard.writeText(text).then(onDone).catch(() => undefined);
}

function MarkdownTable({
  children,
  showCopy,
}: {
  children?: ReactNode;
  showCopy: boolean;
}) {
  const tableRef = useRef<HTMLTableElement>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return undefined;
    const id = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(id);
  }, [copied]);

  const handleCopy = () => {
    if (!tableRef.current) return;
    const tsv = extractTableTsv(tableRef.current);
    safeCopyText(tsv, () => setCopied(true));
  };

  return (
    <div className="md-table-wrap">
      {showCopy ? (
        <div className="md-table-bar">
          <button type="button" className="md-copy-btn" onClick={handleCopy}>
            <Icon name={copied ? "check" : "copy"} size={13} />
            <span>{copied ? "已复制 TSV" : "复制表格"}</span>
          </button>
        </div>
      ) : null}
      <div className="md-table-scroll">
        <table ref={tableRef}>{children}</table>
      </div>
    </div>
  );
}

function MarkdownCodeBlock({
  children,
  showCopy,
}: {
  children?: ReactNode;
  showCopy: boolean;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return undefined;
    const id = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(id);
  }, [copied]);

  let lang = "";
  let rawCode = "";
  const childArray = Children.toArray(children);
  const first = childArray[0];
  if (isValidElement<{ className?: string; children?: ReactNode }>(first)) {
    const cls = typeof first.props.className === "string" ? first.props.className : "";
    const langMatch = /language-([A-Za-z0-9_+-]+)/.exec(cls);
    if (langMatch) lang = langMatch[1]!;
    rawCode = extractNodeText(first.props.children).replace(/\n$/, "");
  } else {
    rawCode = extractNodeText(children).replace(/\n$/, "");
  }

  return (
    <div className="md-code-wrap">
      <div className="md-code-bar">
        <span className="md-code-lang">{lang || "代码"}</span>
        {showCopy ? (
          <button type="button" className="md-copy-btn" onClick={() => safeCopyText(rawCode, () => setCopied(true))}>
            <Icon name={copied ? "check" : "copy"} size={13} />
            <span>{copied ? "已复制" : "复制"}</span>
          </button>
        ) : null}
      </div>
      <pre>{children}</pre>
    </div>
  );
}

function cellClassName(
  isNumeric: boolean,
  styleAlign: unknown,
): string | undefined {
  const classes: string[] = [];
  if (isNumeric || styleAlign === "right") classes.push("md-num");
  else if (styleAlign === "center") classes.push("md-align-center");
  return classes.length > 0 ? classes.join(" ") : undefined;
}

function buildComponents(variant: MarkdownVariant): Components {
  const isReport = variant === "report";
  const allowCopy = isReport;
  return {
    h1: ({ children }) => (variant === "notes" ? <h4>{children}</h4> : <h2>{children}</h2>),
    h2: ({ children }) => (variant === "notes" ? <h4>{children}</h4> : <h2>{children}</h2>),
    h3: ({ children }) => (variant === "notes" ? <h4>{children}</h4> : <h3>{children}</h3>),
    h4: ({ children }) => <h4>{children}</h4>,
    h5: ({ children }) => <h4>{children}</h4>,
    h6: ({ children }) => <h4>{children}</h4>,
    p: ({ children }) => (variant === "inline" ? <span className="md-inline-p">{children}</span> : <p>{children}</p>),
    img: ({ alt }) => {
      const label = typeof alt === "string" ? alt.trim() : "";
      return <span className="md-img-fallback">{label ? `[图片：${label}]` : "[图片]"}</span>;
    },
    a: ({ href, children }) => {
      if (isReport && href && CLAIM_FRAGMENT.test(href)) return <ClaimLink href={href}>{children}</ClaimLink>;
      const safe = safeExternalHref(href);
      if (isReport && safe) {
        return (
          <a href={safe.href} target="_blank" rel="noopener noreferrer" className="md-link">
            <span>{children}</span>
            <Icon name="external" size={12} />
          </a>
        );
      }
      return (
        <span className="md-link-plain">
          {children}
          {safe ? <span className="md-link-host">{`（${safe.host}）`}</span> : null}
        </span>
      );
    },
    table: ({ children }) => <MarkdownTable showCopy={allowCopy}>{children}</MarkdownTable>,
    th: ({ children, style }) => {
      const cls = cellClassName(false, style?.textAlign);
      return <th className={cls}>{children}</th>;
    },
    td: ({ children, style }) => {
      const text = extractNodeText(children);
      const cls = cellClassName(isNumericCellText(text), style?.textAlign);
      return <td className={cls}>{children}</td>;
    },
    pre: ({ children }) => <MarkdownCodeBlock showCopy={allowCopy}>{children}</MarkdownCodeBlock>,
  };
}

const COMPONENTS_BY_VARIANT: Record<MarkdownVariant, Components> = {
  report: buildComponents("report"),
  draft: buildComponents("draft"),
  notes: buildComponents("notes"),
  inline: buildComponents("inline"),
};

function allowedElementsFor(variant: MarkdownVariant): readonly string[] {
  if (variant === "inline") return INLINE_ELEMENTS;
  if (variant === "notes") return NOTES_ELEMENTS;
  return REPORT_ELEMENTS;
}

function safeUrlTransform(url: string): string {
  if (CLAIM_FRAGMENT.test(url)) return url;
  return safeExternalHref(url)?.href ?? "";
}

function PlainParagraphFallback({ content, inline, showCursor }: { content: string; inline: boolean; showCursor: boolean }) {
  if (inline) {
    return (
      <>
        {content}
        {showCursor ? <span className="stream-cursor" aria-hidden="true" /> : null}
      </>
    );
  }
  const paragraphs = content.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
  if (paragraphs.length === 0) {
    return showCursor ? <span className="stream-cursor" aria-hidden="true" /> : null;
  }
  return (
    <>
      {paragraphs.map((text, index) => (
        <p key={index}>
          {text}
          {showCursor && index === paragraphs.length - 1 ? <span className="stream-cursor" aria-hidden="true" /> : null}
        </p>
      ))}
    </>
  );
}

class MarkdownErrorBoundary extends Component<
  { fallbackContent: string; inline: boolean; showCursor: boolean; children: ReactNode },
  { hasError: boolean; contentKey: string }
> {
  override state = { hasError: false, contentKey: "" };

  static getDerivedStateFromError(): { hasError: boolean } {
    return { hasError: true };
  }

  static getDerivedStateFromProps(
    props: { fallbackContent: string },
    prev: { hasError: boolean; contentKey: string },
  ) {
    if (props.fallbackContent !== prev.contentKey) {
      return { hasError: false, contentKey: props.fallbackContent };
    }
    return null;
  }

  override render() {
    if (this.state.hasError) {
      return (
        <PlainParagraphFallback
          content={this.props.fallbackContent}
          inline={this.props.inline}
          showCursor={this.props.showCursor}
        />
      );
    }
    return this.props.children;
  }
}

function MarkdownAstBlock({
  text,
  variant,
}: {
  text: string;
  variant: MarkdownVariant;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={variant === "report" ? REPORT_PLUGINS : REMARK_PLUGINS}
      skipHtml
      unwrapDisallowed
      allowedElements={allowedElementsFor(variant)}
      urlTransform={safeUrlTransform}
      components={COMPONENTS_BY_VARIANT[variant]}
    >
      {text}
    </ReactMarkdown>
  );
}

function MarkdownBlockWithCharts<TChart = unknown>({
  text,
  variant,
  chartsById,
  renderChart,
  tablesById,
}: {
  text: string;
  variant: MarkdownVariant;
  chartsById?: ReadonlyMap<string, TChart> | undefined;
  renderChart?: ((chart: TChart) => ReactNode) | undefined;
  tablesById?: ReadonlyMap<string, ReactNode> | undefined;
}) {
  const allowCharts = variant === "report" || variant === "draft";
  const segments = useMemo(
    () => (allowCharts && (text.includes("[[chart:") || text.includes("[[table:")) ? splitChartSegments(text) : [{ kind: "md" as const, text }]),
    [allowCharts, text],
  );
  const renderedChartIds = new Set<string>();
  return (
    <>
      {segments.map((segment, index) => {
        if (segment.kind === "table") {
          if (renderedChartIds.has(`table:${segment.chartId}`)) return null;
          const table = tablesById?.get(segment.chartId);
          if (table === undefined) return null;
          renderedChartIds.add(`table:${segment.chartId}`);
          return <div className="md-table-slot" key={`table-${segment.chartId}-${index}`}>{table}</div>;
        }
        if (segment.kind === "chart") {
          if (renderedChartIds.has(segment.chartId)) return null;
          const chart = chartsById?.get(segment.chartId);
          if (chart === undefined || !renderChart) return null;
          renderedChartIds.add(segment.chartId);
          return (
            <div className="md-chart-slot" key={`chart-${segment.chartId}-${index}`}>
              {renderChart(chart)}
            </div>
          );
        }
        return <MarkdownAstBlock key={`md-${index}`} text={segment.text} variant={variant} />;
      })}
    </>
  );
}

const FrozenMarkdownBlock = memo(MarkdownBlockWithCharts) as typeof MarkdownBlockWithCharts;

/**
 * Restricted GFM Markdown renderer covering static reports/notes/inline labels and
 * flicker-free L3 streaming drafts. Never uses `innerHTML`, `rehype-raw`, `<style>`, or `<img>`.
 */
export function MarkdownContent<TChart = unknown>({
  content,
  variant,
  streaming = false,
  showCursor = false,
  chartsById,
  renderChart,
  tablesById,
  className,
}: MarkdownContentProps<TChart>) {
  const isInline = variant === "inline";
  const Wrapper = isInline ? "span" : "div";
  const classes = [
    isInline ? "md-inline" : "md-prose",
    `md-v-${variant}`,
    streaming ? "is-streaming" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  const renderInner = useCallback((): ReactNode => {
    if (!content) {
      return showCursor ? <span className="stream-cursor" aria-hidden="true" /> : null;
    }

    if (streaming) {
      if (!hasMarkdownSyntax(content)) {
        return <PlainParagraphFallback content={content} inline={isInline} showCursor={showCursor} />;
      }
      const { stableBlocks, activeTail } = splitStableMarkdownBlocks(content);
      const repairedTail = activeTail ? repairIncompleteMarkdown(activeTail) : "";
      return (
        <>
          {stableBlocks.map((block, index) => (
            <FrozenMarkdownBlock
              key={index}
              text={block}
              variant={variant}
              chartsById={chartsById}
              renderChart={renderChart}
              tablesById={tablesById}
            />
          ))}
          {repairedTail ? (
            <MarkdownBlockWithCharts
              text={repairedTail}
              variant={variant}
              chartsById={chartsById}
              renderChart={renderChart}
              tablesById={tablesById}
            />
          ) : null}
          {showCursor ? <span className="stream-cursor" aria-hidden="true" /> : null}
        </>
      );
    }

    if ((isInline || !(variant === "report" && /\[\[(?:chart|table|claim):/.test(content))) && !hasMarkdownSyntax(content)) {
      return <PlainParagraphFallback content={content} inline={isInline} showCursor={false} />;
    }

    return (
      <MarkdownBlockWithCharts
        text={content}
        variant={variant}
        chartsById={chartsById}
        renderChart={renderChart}
        tablesById={tablesById}
      />
    );
  }, [content, streaming, isInline, showCursor, variant, chartsById, renderChart, tablesById]);

  return (
    <Wrapper className={classes}>
      <MarkdownErrorBoundary fallbackContent={content} inline={isInline} showCursor={showCursor}>
        {renderInner()}
      </MarkdownErrorBoundary>
    </Wrapper>
  );
}
