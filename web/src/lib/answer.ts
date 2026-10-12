/**
 * Formats the synthesized answer into a scannable, financial-brief style
 * Markdown string.
 *
 * The agent answers in plain prose (the model is instructed not to emit
 * Markdown), so a long answer can otherwise render as one dense paragraph.
 * This module:
 *  - leaves already-structured Markdown untouched (headings, lists, bold),
 *  - reflows dense prose into short paragraphs (never mechanical bullets),
 *  - preserves line-based answers as separate paragraphs,
 *  - bolds dates, monetary figures, percentages, LSE tickers and the
 *    subject company names it is given.
 *
 * It only ever adds `**` emphasis to plain text; it never rewrites or drops
 * wording and never emits HTML. The output is rendered by the safe Markdown
 * renderer.
 */

const MONTHS =
  "January|February|March|April|May|June|July|August|September|October|November|December|" +
  "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec";

// Alternation of the deterministic "key terms" we always bold.
const KEY_TERM_SOURCE = [
  `\\b\\d{1,2}\\s+(?:${MONTHS})\\.?\\s+\\d{4}\\b`, // 5 October 2026
  `\\b\\d{4}-\\d{2}-\\d{2}\\b`, // 2026-10-05
  `(?:£|\\$|€)\\s?\\d[\\d.,]*(?:\\s*(?:bn|billion|m|million|k|trillion))?`, // £4.7bn
  `\\b\\d+(?:\\.\\d+)?%`, // 33%
  `\\b[A-Z]{2,4}\\.L\\b`, // AZN.L
].join("|");

const KEY_TERM = new RegExp(KEY_TERM_SOURCE, "g");

const HAS_EMPHASIS = /\*\*|__/;
const HAS_BLOCK_MARKERS = /(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|\|)/;

const ABBREVIATIONS = new Set([
  "approx", "e.g", "i.e", "etc", "vs", "fig", "no", "dr", "mr", "ms", "mrs",
  "ltd", "inc", "co", "corp", "st", "al", "cf", "sec", "u.s", "u.k",
]);

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** True when the text already carries Markdown block/emphasis structure. */
export function hasMarkdownStructure(text: string): boolean {
  return HAS_EMPHASIS.test(text) || HAS_BLOCK_MARKERS.test(text);
}

const FIGURE_RE =
  /(?:£|\$|€)\s?\d[\d.,]*(?:\s*(?:bn|billion|m|million|k|trillion))?|\b\d+(?:\.\d+)?%/g;

/** Distinct monetary figures and percentages in text, in order of appearance. */
export function extractFigures(text: string): string[] {
  const found = (text.match(FIGURE_RE) ?? []).map((f) => f.trim().replace(/[.,]$/, ""));
  return [...new Set(found)].map((f) => `**${f}**`);
}

const DATE_RE = new RegExp(
  `\\b\\d{1,2}\\s+(?:${MONTHS})\\.?\\s+\\d{4}\\b|\\b\\d{4}-\\d{2}-\\d{2}\\b`
);

const PUBLISHED_DATE_RE = new RegExp(
  `\\b(?:reported|published|announced|dated|posted)\\s+(?:on\\s+)?(${DATE_RE.source})`,
  "i"
);

/**
 * An explicit calendar date stated in the text, or null. A date introduced by
 * "reported / published / announced / dated" wins over other dates (e.g. a
 * quarter-end date); otherwise the first date is used.
 */
export function extractDate(text: string): string | null {
  const published = text.match(PUBLISHED_DATE_RE);
  if (published) return published[1];
  return text.match(DATE_RE)?.[0] ?? null;
}

/** Bold dates, monetary figures, percentages and tickers in plain text. */
export function boldKeyTerms(text: string): string {
  return text.replace(KEY_TERM, (m) => `**${m}**`);
}

/** Bold key terms plus any provided company names (whole-word, longest first). */
export function emphasize(text: string, highlights: string[] = []): string {
  const names = [...new Set(highlights)]
    .map((h) => h.trim())
    .filter((h) => h.length >= 2)
    .sort((a, b) => b.length - a.length)
    .map((h) => `\\b${escapeRegExp(h)}\\b`);
  const pattern = names.length ? `${KEY_TERM_SOURCE}|${names.join("|")}` : KEY_TERM_SOURCE;
  return text.replace(new RegExp(pattern, "g"), (m) => `**${m}**`);
}

/** Split prose into sentences without breaking decimals or abbreviations. */
export function splitSentences(text: string): string[] {
  const out: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch !== "." && ch !== "!" && ch !== "?") continue;
    // A boundary must be followed by whitespace or the end of the text —
    // this keeps "AZN.L", "U.S." and "9.5" intact.
    if (i + 1 < text.length && !/\s/.test(text[i + 1])) continue;

    let j = i + 1;
    while (j < text.length && /\s/.test(text[j])) j++;
    const atEnd = j >= text.length;
    const next = text[j];
    // A new sentence starts with a capital, digit, quote or currency symbol.
    if (!atEnd && !/[A-Z0-9"“(£$€]/.test(next)) continue;

    if (ch === ".") {
      const prev = text[i - 1] ?? "";
      if (/\d/.test(prev) && /\d/.test(text[i + 1] ?? "")) continue; // 9.5
      if (prev === ".") continue; // ellipsis-ish
      const word = text.slice(0, i).match(/([A-Za-z.]+)$/)?.[1]?.toLowerCase();
      if (word && ABBREVIATIONS.has(word)) continue; // approx. / U.S.
    }
    out.push(text.slice(start, i + 1).trim());
    start = j;
    i = j - 1;
  }
  if (start < text.length) out.push(text.slice(start).trim());
  return out.filter(Boolean);
}

/** Group sentences into short paragraphs (default two sentences each). */
function shortParagraphs(text: string, perParagraph = 2): string[] {
  const sentences = splitSentences(text);
  if (sentences.length <= 1) return [text];
  const paragraphs: string[] = [];
  for (let i = 0; i < sentences.length; i += perParagraph) {
    paragraphs.push(sentences.slice(i, i + perParagraph).join(" "));
  }
  return paragraphs;
}

// Segments that must never be touched: existing emphasis, code, links, URLs.
const PROTECTED = /(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]*`|\[[^\]\n]*\]\([^)\n]*\)|https?:\/\/\S+)/;

/** Bold plain-text key terms inside Markdown without altering its structure. */
export function emphasizeStructured(md: string): string {
  return md
    .split("\n")
    .map((line) => {
      if (/^\s*(#{1,6}\s|```|\|)/.test(line)) return line; // headings, fences, tables
      return line
        .split(PROTECTED)
        .map((seg, i) => (i % 2 === 1 ? seg : boldKeyTerms(seg)))
        .join("");
    })
    .join("\n");
}

/**
 * Return Markdown for the answer: already-Markdown text is preserved and
 * rendered as-is; dense prose is reflowed into short paragraphs and has its
 * key terms emphasised. `highlights` are subject company names to bold.
 */
export function formatAnswerMarkdown(text: string, highlights: string[] = []): string {
  const t = text.replace(/\r\n?/g, "\n").replace(/[ \t]+\n/g, "\n").trim();
  if (!t) return t;

  // Already Markdown (bold or block markers) — keep its structure and only
  // add emphasis to plain-text dates/figures it has not already bolded.
  if (hasMarkdownStructure(t)) return emphasizeStructured(t);

  // Multi-paragraph prose: keep the paragraphing, just emphasise key terms.
  if (/\n\s*\n/.test(t)) {
    return t
      .split(/\n\s*\n/)
      .map((p) => emphasize(p.replace(/\s+/g, " ").trim(), highlights))
      .filter(Boolean)
      .join("\n\n");
  }

  // Line-based answer (e.g. "Label: value" per line): keep the lines.
  if (t.includes("\n")) {
    return t
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => emphasize(line, highlights))
      .join("\n\n");
  }

  // Single dense paragraph: reflow into short paragraphs (never bullets).
  return shortParagraphs(t)
    .map((p) => emphasize(p, highlights))
    .join("\n\n");
}
