/**
 * A tiny, safe formatter for model output. It understands only paragraphs, bullet/numbered lists, `code spans` and
 * citation markers like `[1]`; everything else is literal text. It returns a data structure (never HTML), and the
 * renderer turns it into React text nodes, so no model output is ever parsed as markup. Pure.
 */
export type Inline = { t: "text"; text: string } | { t: "code"; text: string } | { t: "cite"; marker: string; raw: string };
export type Block = { t: "p"; inline: Inline[] } | { t: "ul"; items: Inline[][] } | { t: "ol"; items: Inline[][] };

const MARKER_RE = /\[(\d{1,3})\]/g;
const BULLET_RE = /^\s{0,3}[-*•]\s+(.*)$/;
const NUMBERED_RE = /^\s{0,3}\d{1,3}[.)]\s+(.*)$/;
const MAX_CHARS = 100_000;

function citations(text: string): Inline[] {
  const out: Inline[] = [];
  let at = 0;
  for (const m of text.matchAll(MARKER_RE)) {
    const index = m.index ?? 0;
    if (index > at) out.push({ t: "text", text: text.slice(at, index) });
    out.push({ t: "cite", marker: m[0], raw: m[0] });
    at = index + m[0].length;
  }
  if (at < text.length) out.push({ t: "text", text: text.slice(at) });
  return out;
}

export function parseInline(line: string): Inline[] {
  const out: Inline[] = [];
  let rest = line;
  while (rest.length > 0) {
    const open = rest.indexOf("`");
    const close = open === -1 ? -1 : rest.indexOf("`", open + 1);
    if (open === -1 || close === -1) {
      out.push(...citations(rest));
      break;
    }
    if (open > 0) out.push(...citations(rest.slice(0, open)));
    const code = rest.slice(open + 1, close);
    if (code !== "") out.push({ t: "code", text: code });
    rest = rest.slice(close + 1);
  }
  return out;
}

export function parseBlocks(input: string): Block[] {
  const lines = input.slice(0, MAX_CHARS).replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { kind: "ul" | "ol"; items: string[] } | null = null;
  const flushPara = () => {
    if (para.length > 0) blocks.push({ t: "p", inline: parseInline(para.join("\n")) });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push({ t: list.kind, items: list.items.map(parseInline) });
    list = null;
  };
  for (const line of lines) {
    const bullet = BULLET_RE.exec(line);
    const numbered = bullet ? null : NUMBERED_RE.exec(line);
    const item = bullet ?? numbered;
    if (item) {
      flushPara();
      const kind = bullet ? "ul" : "ol";
      if (list && list.kind !== kind) flushList();
      list ??= { kind, items: [] };
      list.items.push(item[1]);
    } else if (line.trim() === "") {
      flushPara();
      flushList();
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara();
  flushList();
  return blocks;
}
