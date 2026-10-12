/**
 * Builds the Executive Summary Markdown from data the report already carries.
 *
 * The API's `answer.answer` is plain prose (the model is told not to emit
 * Markdown), so structure has to be composed client-side. Nothing here adds
 * claims: every line is taken from the answer, key points, cited sources
 * or limitations; sections with no supporting data are omitted.
 *
 *   Overview · Key Developments · Financial Impact · Risks & Conflicts
 *
 * Key Developments states each key point itself (label plus what happened), with the source date only
 * when the key point has none; per-item source links live in the Key Findings card.
 *
 * If the answer already contains Markdown structure it is used unchanged.
 */
import type { QueryAnswer } from "../types";
import { formatDate } from "./format";
import { extractDate, extractFigures, formatAnswerMarkdown, hasMarkdownStructure } from "./answer";

const MAX_NOTES = 3;
const MAX_LABEL = 80;
const MAX_DEVELOPMENT = 280;

// Limitations that describe conflict/uncertainty rather than missing data.
const RISK_PATTERN =
  /\b(conflict|conflicting|contradict\w*|differ(?:s|ent|ing)?|inconsisten\w*|discrepan\w*|unverified|not (?:been )?(?:cross-)?verified|not independently|opinion|commentary|speculat\w*|rumou?r\w*)\b/i;

function mdText(s: string): string {
  return s.replace(/[\[\]]/g, "").replace(/\s+/g, " ").trim();
}

/** Short headline for a key point: its "Label:" prefix, else its first clause. */
export function keyPointLabel(kp: string): string {
  const t = kp.replace(/\s+/g, " ").trim();
  const colon = t.indexOf(":");
  if (colon > 0 && colon <= MAX_LABEL) return t.slice(0, colon).trim();
  const sentence = t.split(/(?<=[.!?])\s/)[0] ?? t;
  if (sentence.length <= MAX_LABEL) return sentence.replace(/[.!?]$/, "");
  const cut = sentence.slice(0, MAX_LABEL);
  return cut.slice(0, cut.lastIndexOf(" ") > 20 ? cut.lastIndexOf(" ") : MAX_LABEL) + "…";
}

/** The key development itself ("**Label:** what happened"), not just its headline; trimmed to a readable length. */
function developmentLine(kp: string, date: string | null): string {
  let t = mdText(kp);
  if (t.length > MAX_DEVELOPMENT) {
    const cut = t.slice(0, MAX_DEVELOPMENT);
    t = cut.slice(0, cut.lastIndexOf(" ") > 40 ? cut.lastIndexOf(" ") : MAX_DEVELOPMENT).replace(/[,;:\s]+$/, "") + "…";
  }
  if (date) t = `${t.replace(/\.$/, "")} (${date})`;
  const colon = t.indexOf(":");
  return colon > 0 && colon <= MAX_LABEL ? `**${t.slice(0, colon)}:**${t.slice(colon + 1)}` : t;
}

function section(title: string, body: string[]): string {
  return body.length ? `## ${title}\n\n${body.join("\n")}` : "";
}

export function buildExecutiveSummary(
  answer: QueryAnswer,
  highlights: string[] = []
): string {
  const base = formatAnswerMarkdown(answer.answer, highlights);
  if (!base || hasMarkdownStructure(answer.answer.trim())) return base;

  const parts: string[] = [`## Overview\n\n${base}`];
  const sources = answer.sources ?? [];

  // Key Developments: one bullet per key point.
  const developments: string[] = [];
  const financial: string[] = [];
  (answer.key_points ?? []).forEach((kp, i) => {
    if (!kp.trim()) return;
    const label = keyPointLabel(kp);
    const src = sources.find((s) => s.supports_claims.includes(i));
    // The source's publication date is added only when the key point does not already state a date; an
    // unknown date is simply left out, never invented.
    const date = src?.published_at && !extractDate(kp) ? formatDate(src.published_at) : null;
    developments.push(`- ${developmentLine(kp, date)}`);
    const figures = extractFigures(kp);
    if (figures.length) financial.push(`- **${mdText(label)}:** ${figures.join(", ")}`);
  });
  parts.push(section("Key Developments", developments));
  parts.push(section("Financial Impact", financial));

  // Risks & Conflicts: the answer's own notes about conflict or uncertainty.
  // (Missing-data notes are deliberately not part of the summary.)
  const risks = (answer.limitations ?? [])
    .map((l) => l.trim())
    .filter((l) => l && RISK_PATTERN.test(l))
    .slice(0, MAX_NOTES)
    .map((l) => `- ${l}`);
  parts.push(section("Risks & Conflicts", risks));

  return parts.filter(Boolean).join("\n\n");
}
