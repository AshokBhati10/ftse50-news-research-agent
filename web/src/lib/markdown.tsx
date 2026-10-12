/**
 * Minimal, dependency-free Markdown renderer for research answers.
 *
 * Research answers routinely contain light Markdown (bold dates, numbered
 * lists, links, headings). Rendering that text inside a single <p> collapses
 * whitespace and leaves literal `**` markers on screen. This module converts
 * the safe subset the agent emits into React elements — never via
 * dangerouslySetInnerHTML — so untrusted model/web text cannot inject markup.
 *
 * Supported: paragraphs, headings, bold (double asterisk or double
 * underscore), italic (single asterisk or single underscore), `inline code`,
 * [links](https://…) and bare https:// URLs, unordered/ordered (nestable) lists, > blockquotes,
 * --- horizontal rules and simple | tables |.
 */
import React from "react";

const SAFE_HREF = /^https?:\/\/[^\s]+$/i;

const HEADINGS: Record<number, "h1" | "h2" | "h3" | "h4" | "h5" | "h6"> = {
  1: "h1",
  2: "h2",
  3: "h3",
  4: "h4",
  5: "h5",
  6: "h6",
};

const HEADING_CLASS: Record<number, string> = {
  1: "mt-5 mb-2 text-xl font-semibold text-white first:mt-0",
  2: "mt-5 mb-2 text-lg font-semibold text-white first:mt-0",
  3: "mt-4 mb-1.5 text-base font-semibold text-white first:mt-0",
  4: "mt-3 mb-1.5 text-sm font-semibold uppercase tracking-wide text-terminal-muted first:mt-0",
  5: "mt-3 mb-1 text-sm font-semibold text-terminal-muted first:mt-0",
  6: "mt-3 mb-1 text-xs font-semibold uppercase tracking-wide text-terminal-faint first:mt-0",
};

const LINK_CLASS = "break-words text-indigo-300 underline decoration-indigo-400/40 underline-offset-2 hover:text-indigo-200";
const CODE_CLASS = "rounded bg-white/10 px-1.5 py-0.5 font-mono text-[0.85em] text-terminal-text";

function isWordChar(ch: string | undefined): boolean {
  return !!ch && /[A-Za-z0-9]/.test(ch);
}

/** Render inline Markdown (bold, italic, code, links). Never emits raw HTML. */
export function renderInline(
  text: string,
  keyPrefix = "md",
  inLink = false
): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let buf = "";
  let i = 0;
  let k = 0;

  const flush = () => {
    if (buf) {
      out.push(buf);
      buf = "";
    }
  };

  while (i < text.length) {
    const rest = text.slice(i);
    const prev = i > 0 ? text[i - 1] : undefined;
    let m: RegExpMatchArray | null;

    // `code`
    if ((m = rest.match(/^`([^`]+)`/))) {
      flush();
      out.push(
        React.createElement("code", { key: `${keyPrefix}-c${k++}`, className: CODE_CLASS }, m[1])
      );
      i += m[0].length;
      continue;
    }

    // [label](url)
    if ((m = rest.match(/^\[([^\]]+)\]\(([^)\s]+)\)/))) {
      flush();
      const label = renderInline(m[1], `${keyPrefix}-l${k}`, true);
      const href = m[2].trim();
      out.push(
        SAFE_HREF.test(href)
          ? React.createElement(
              "a",
              {
                key: `${keyPrefix}-a${k++}`,
                href,
                target: "_blank",
                rel: "noopener noreferrer",
                className: LINK_CLASS,
              },
              label
            )
          : React.createElement("span", { key: `${keyPrefix}-a${k++}` }, label)
      );
      i += m[0].length;
      continue;
    }

    // Bare https://… URL (not preceded by a word character) → safe link.
    if (!inLink && !isWordChar(prev) && (m = rest.match(/^https?:\/\/[^\s<>\])]+/))) {
      const url = m[0].replace(/[.,;:!?'"*_]+$/, "");
      if (SAFE_HREF.test(url)) {
        flush();
        out.push(
          React.createElement(
            "a",
            {
              key: `${keyPrefix}-u${k++}`,
              href: url,
              target: "_blank",
              rel: "noopener noreferrer",
              className: LINK_CLASS,
            },
            url
          )
        );
        i += url.length;
        continue;
      }
    }

    // **bold** or __bold__
    if ((m = rest.match(/^\*\*([^*]+)\*\*/)) || (m = rest.match(/^__([^_]+)__/))) {
      flush();
      out.push(
        React.createElement(
          "strong",
          { key: `${keyPrefix}-b${k++}`, className: "font-semibold text-white" },
          renderInline(m[1], `${keyPrefix}-b${k}`)
        )
      );
      i += m[0].length;
      continue;
    }

    // *italic* (single asterisk, not part of a word)
    if (rest[0] === "*" && rest[1] !== "*" && !isWordChar(prev) &&
        (m = rest.match(/^\*([^*\n]+?)\*(?!\*)/))) {
      flush();
      out.push(
        React.createElement(
          "em",
          { key: `${keyPrefix}-i${k++}`, className: "italic" },
          renderInline(m[1], `${keyPrefix}-i${k}`)
        )
      );
      i += m[0].length;
      continue;
    }

    // _italic_ (underscore emphasis, not inside identifiers)
    if (rest[0] === "_" && !isWordChar(prev) &&
        (m = rest.match(/^_([^_\n]+?)_(?!_)/)) &&
        !isWordChar(text[i + m[0].length])) {
      flush();
      out.push(
        React.createElement(
          "em",
          { key: `${keyPrefix}-i${k++}`, className: "italic" },
          renderInline(m[1], `${keyPrefix}-i${k}`)
        )
      );
      i += m[0].length;
      continue;
    }

    buf += text[i];
    i++;
  }

  flush();
  return out;
}

function isBlank(line: string): boolean {
  return !line.trim();
}

function isHeading(line: string): boolean {
  return /^#{1,6}\s+/.test(line);
}

function isRule(line: string): boolean {
  return /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line);
}

function isBlockquote(line: string): boolean {
  return /^\s*>\s?/.test(line);
}

function isListStart(line: string): boolean {
  return /^\s*(?:[-*+]|\d+[.)])\s+/.test(line);
}

function isTableStart(lines: string[], i: number): boolean {
  const header = lines[i];
  const sep = lines[i + 1];
  if (!header || !sep || !header.includes("|")) return false;
  return /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(sep);
}

function splitRow(row: string): string[] {
  let r = row.trim();
  if (r.startsWith("|")) r = r.slice(1);
  if (r.endsWith("|")) r = r.slice(0, -1);
  return r.split("|").map((c) => c.trim());
}

function parseBlockquote(lines: string[], start: number, keyPrefix: string):
  [React.ReactNode, number] {
  const inner: string[] = [];
  let i = start;
  while (i < lines.length && isBlockquote(lines[i])) {
    inner.push(lines[i].replace(/^\s*>\s?/, ""));
    i++;
  }
  const body = inner.join("\n");
  return [
    React.createElement(
      "blockquote",
      {
        key: `${keyPrefix}-q`,
        className: "border-l-2 border-indigo-500/60 pl-4 text-terminal-muted",
      },
      renderBlocks(body, `${keyPrefix}-q`)
    ),
    i,
  ];
}

function parseTable(lines: string[], start: number, keyPrefix: string):
  [React.ReactNode, number] {
  const header = splitRow(lines[start]);
  let i = start + 2;
  const rows: string[][] = [];
  while (i < lines.length && !isBlank(lines[i]) && lines[i].includes("|")) {
    rows.push(splitRow(lines[i]));
    i++;
  }
  const cellClass = "border border-terminal-border px-2.5 py-1.5 align-top";
  return [
    React.createElement(
      "div",
      { key: `${keyPrefix}-t`, className: "my-3 overflow-x-auto" },
      React.createElement(
        "table",
        { className: "w-full border-collapse text-sm" },
        React.createElement(
          "thead",
          null,
          React.createElement(
            "tr",
            null,
            header.map((c, ci) =>
              React.createElement(
                "th",
                { key: ci, className: `${cellClass} bg-white/5 text-left font-semibold text-white` },
                renderInline(c, `${keyPrefix}-th${ci}`)
              )
            )
          )
        ),
        React.createElement(
          "tbody",
          null,
          rows.map((row, ri) =>
            React.createElement(
              "tr",
              { key: ri },
              header.map((_, ci) =>
                React.createElement(
                  "td",
                  { key: ci, className: `${cellClass} text-terminal-text` },
                  renderInline(row[ci] ?? "", `${keyPrefix}-td${ri}-${ci}`)
                )
              )
            )
          )
        )
      )
    ),
    i,
  ];
}

function parseList(lines: string[], start: number, keyPrefix: string):
  [React.ReactNode, number] {
  const firstMatch = lines[start].match(/^(\s*)([-*+]|\d+[.)])\s+/);
  const ordered = firstMatch ? /^\d/.test(firstMatch[2]) : false;
  const baseIndent = firstMatch ? firstMatch[1].length : 0;
  const items: React.ReactNode[] = [];
  let i = start;
  let itemKey = 0;

  const itemMatch = (line: string) => line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);

  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      // Skip blank lines only when the same list clearly continues.
      let j = i + 1;
      while (j < lines.length && isBlank(lines[j])) j++;
      const next = j < lines.length ? itemMatch(lines[j]) : null;
      if (next && next[1].length === baseIndent && /^\d/.test(next[2]) === ordered) {
        i = j;
        continue;
      }
      break;
    }
    const m = itemMatch(line);
    // A different list kind or a shallower/deeper marker starts another list.
    if (!m || m[1].length !== baseIndent || /^\d/.test(m[2]) !== ordered) break;
    const indent = m[1].length;
    const content: string[] = [m[3]];
    const nested: string[] = [];
    let j = i + 1;
    while (j < lines.length) {
      const nxt = lines[j];
      if (isBlank(nxt)) {
        // A blank followed by a deeper item is a loose-list separator.
        let p = j + 1;
        while (p < lines.length && isBlank(lines[p])) p++;
        const pm = p < lines.length ? itemMatch(lines[p]) : null;
        if (pm && pm[1].length > indent) {
          nested.push(...lines.slice(j, p));
          j = p;
          continue;
        }
        break;
      }
      const nm = itemMatch(nxt);
      if (nm) {
        if (nm[1].length > indent) {
          nested.push(nxt);
          j++;
          continue;
        }
        break;
      }
      if (nxt.match(/^\s+/) && nxt.trim()) {
        content.push(nxt.trim()); // wrapped continuation of this item
        j++;
        continue;
      }
      break;
    }
    items.push(
      React.createElement(
        "li",
        { key: `${keyPrefix}-li${itemKey++}`, className: "pl-1" },
        renderInline(content.join(" "), `${keyPrefix}-li${itemKey}`),
        nested.length > 0
          ? renderBlocks(
              nested.map((n) => n.replace(/^\s+/, "")).join("\n"),
              `${keyPrefix}-n${itemKey}`
            )
          : null
      )
    );
    i = j;
  }

  const Tag = ordered ? "ol" : "ul";
  return [
    React.createElement(
      Tag,
      {
        key: `${keyPrefix}-list`,
        className: ordered
          ? "my-2 list-decimal space-y-1.5 pl-5 marker:text-terminal-faint"
          : "my-2 list-disc space-y-1.5 pl-5 marker:text-terminal-faint",
      },
      items
    ),
    i,
  ];
}

/** Parse Markdown block structure into React elements. */
export function renderBlocks(text: string, keyPrefix = "md"): React.ReactNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const nodes: React.ReactNode[] = [];
  let i = 0;
  let k = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      i++;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      const Tag = HEADINGS[level];
      nodes.push(
        React.createElement(
          Tag,
          { key: `${keyPrefix}-h${k++}`, className: HEADING_CLASS[level] },
          renderInline(heading[2], `${keyPrefix}-h${k}`)
        )
      );
      i++;
      continue;
    }

    if (isRule(line)) {
      nodes.push(
        React.createElement("hr", {
          key: `${keyPrefix}-hr${k++}`,
          className: "my-4 border-terminal-border",
        })
      );
      i++;
      continue;
    }

    if (isBlockquote(line)) {
      const [node, next] = parseBlockquote(lines, i, `${keyPrefix}-q${k++}`);
      nodes.push(node);
      i = next;
      continue;
    }

    if (isTableStart(lines, i)) {
      const [node, next] = parseTable(lines, i, `${keyPrefix}-t${k++}`);
      nodes.push(node);
      i = next;
      continue;
    }

    if (isListStart(line)) {
      const [node, next] = parseList(lines, i, `${keyPrefix}-u${k++}`);
      nodes.push(node);
      i = next;
      continue;
    }

    // Paragraph: gather consecutive plain lines.
    const para: string[] = [line.trim()];
    let j = i + 1;
    while (j < lines.length && !isBlank(lines[j]) && !isHeading(lines[j]) &&
           !isRule(lines[j]) && !isBlockquote(lines[j]) &&
           !isListStart(lines[j]) && !isTableStart(lines, j)) {
      para.push(lines[j].trim());
      j++;
    }
    nodes.push(
      React.createElement(
        "p",
        { key: `${keyPrefix}-p${k++}`, className: "leading-relaxed" },
        renderInline(para.join(" "), `${keyPrefix}-p${k}`)
      )
    );
    i = j;
  }

  return nodes;
}

/** Block-level Markdown renderer. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  return React.createElement(
    "div",
    { className: className ? `space-y-2 ${className}` : "space-y-2" },
    renderBlocks(text)
  );
}

/** Inline-only Markdown renderer (for list items and short fields). */
export function InlineMarkdown({ text }: { text: string }) {
  return React.createElement(React.Fragment, null, renderInline(text, "in"));
}
