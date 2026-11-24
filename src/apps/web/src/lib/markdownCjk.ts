/**
 * CommonMark only closes `**` when the next character is a space or punctuation, unless the closing
 * run itself follows punctuation. Chinese text puts a full-width colon or quote just inside the
 * markers and a character straight after (`**时点：**研究…`), so the parser leaves the asterisks
 * as text. This turns such leftover pairs, within one text node, into bold.
 */
type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[] };

const LEFTOVER_STRONG = /\*\*(?=[^\s*])([^*\n]*?[^\s*])\*\*/g;
const SKIP = new Set(["code", "inlineCode", "html"]);

export function remarkCjkStrong() {
  function visit(node: MarkdownNode): void {
    if (!node.children || SKIP.has(node.type)) return;
    node.children = node.children.flatMap((child): MarkdownNode[] => {
      if (child.type !== "text" || !child.value?.includes("**")) {
        visit(child);
        return [child];
      }
      const pieces: MarkdownNode[] = [];
      let offset = 0;
      for (const match of child.value.matchAll(LEFTOVER_STRONG)) {
        if (match.index > offset) pieces.push({ type: "text", value: child.value.slice(offset, match.index) });
        pieces.push({ type: "strong", children: [{ type: "text", value: match[1]! }] });
        offset = match.index + match[0].length;
      }
      if (offset === 0) return [child];
      if (offset < child.value.length) pieces.push({ type: "text", value: child.value.slice(offset) });
      return pieces;
    });
  }
  return visit;
}
