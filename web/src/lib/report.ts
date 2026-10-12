/**
 * Report-data helpers: subject highlighting, source ranking and duplicate
 * removal.
 *
 * These operate purely on the data returned by the API — they never invent or
 * rewrite content. Deduplication works on the *event* a finding describes
 * (its key sentence, significance and strong signals such as amounts,
 * percentages and dates) rather than the publisher or the headline, so the
 * same story from different outlets is shown once with corroborating sources.
 */
import { formatDate, hostOf } from "./format";
import type { QueryFinding } from "../types";

/* ------------------------------- highlights ------------------------------ */

const HIGHLIGHT_STOPWORDS = new Set([
  "who", "what", "when", "where", "which", "how", "why", "is", "are", "was",
  "were", "the", "a", "an", "latest", "recent", "new", "top", "best",
  "biggest", "largest", "major", "impact", "impacts", "ceo", "cfo", "cto",
  "coo", "chairman", "chair", "founder", "head", "chief", "ai", "uk", "us",
  "eu", "ipo", "gdp", "ftse", "plc", "ltd", "inc", "corp", "group",
  "holdings", "news", "development", "developments", "update", "updates",
  "market", "markets", "stock", "stocks", "share", "shares", "price",
  "prices", "business", "company", "companies", "report", "research",
  "quarter", "year", "month", "week", "day", "this", "that", "these",
  "those", "of", "for", "on", "in", "and", "or", "to",
]);

/** Company-name tokens from the query to bold in the answer (e.g. "HSBC"). */
export function extractHighlights(query: string | null | undefined): string[] {
  if (!query) return [];
  const tokens = query.match(/\b[A-Z][A-Za-z]{2,}\b/g) ?? [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const token of tokens) {
    const key = token.toLowerCase();
    if (HIGHLIGHT_STOPWORDS.has(key) || seen.has(key)) continue;
    seen.add(key);
    out.push(token);
  }
  return out;
}

/* ---------------------------- text normalisation -------------------------- */

export function normalizeText(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9£$€%.]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Generic words that carry no event signal (kept out of similarity). */
const EVENT_STOPWORDS = new Set([
  "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
  "as", "at", "from", "its", "it", "this", "that", "these", "those", "has",
  "have", "had", "will", "would", "is", "are", "was", "were", "be", "been",
  "being", "after", "before", "into", "over", "amid", "during", "while",
  "also", "more", "most", "than", "which", "who", "what", "when", "where",
  "how", "why", "they", "their", "we", "our", "you", "your", "but", "if",
  "then", "so", "such", "up", "down", "out", "off", "per", "about", "around",
  "approximately", "approx", "circa", "reported", "reportedly", "reports",
  "report", "according", "sources", "source", "said", "says", "new", "latest",
  "recent", "update", "updates", "news", "company", "companies", "group",
  "plc", "ltd", "inc", "corp", "business", "shares", "share", "stock",
  "stocks", "market", "markets", "bn", "billion", "million", "quarter", "year",
]);

/** Significant words (letters only, minus stopwords). */
function significantTokens(s: string): string[] {
  return (s.toLowerCase().match(/[a-z]+/g) ?? []).filter(
    (t) => t.length > 1 && !EVENT_STOPWORDS.has(t)
  );
}

const STRONG_SIGNAL =
  /(?:£|\$|€)\s?\d[\d.,]*(?:\s?(?:bn|billion|m|million|k|trillion))?|\b\d+(?:\.\d+)?%|\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)?(?:bn|m|k)\b|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{4}\b/gi;

/** Comparable signals: amounts, percentages, years and dates. */
function strongSignals(s: string): string[] {
  return (s.match(STRONG_SIGNAL) ?? []).map((m) =>
    m.toLowerCase().replace(/[£$€\s]/g, "")
  );
}

function keyPointTokens(text: string): Set<string> {
  return new Set([...significantTokens(text), ...strongSignals(text)]);
}

function eventTokens(f: QueryFinding): Set<string> {
  return new Set([
    ...significantTokens(`${f.key_finding} ${f.significance}`),
    ...strongSignals(`${f.title} ${f.key_finding} ${f.significance}`),
  ]);
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (!a.size || !b.size) return 0;
  let intersection = 0;
  for (const t of a) if (b.has(t)) intersection++;
  return intersection / (a.size + b.size - intersection);
}

function normalizeUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.hostname.replace(/^www\./, "")}${u.pathname.replace(/\/+$/, "")}`.toLowerCase();
  } catch {
    return url.trim().toLowerCase();
  }
}

/* --------------------------------- sources -------------------------------- */

const OFFICIAL_PATTERNS = [
  /\.gov($|[./])/, /\.gov\./, /sec\.gov/, /fca\.org\.uk/, /londonstockexchange\.com/,
  /companieshouse/, /europa\.eu/, /\.gov\.uk$/, /rns/,
];
const REPUTABLE_PATTERNS = [
  /reuters\.com/, /ft\.com/, /financialtimes/, /bloomberg\.com/, /bbc\.co\.uk/,
  /wsj\.com/, /apnews\.com/, /cnbc\.com/, /theguardian\.com/, /telegraph\.co\.uk/,
  /nytimes\.com/, /economist\.com/, /ft\.com/, /investorschronicle/,
];
const AGGREGATOR_PATTERNS = [
  /finance\.yahoo\.com/, /yahoo\.com/, /marketbeat\.com/, /stocktitan\.net/,
  /morningstar\.com/, /investing\.com/, /simplywall\.st/, /seekingalpha\.com/,
  /tipranks\.com/, /barchart\.com/, /benzinga\.com/, /msn\.com/, /aol\.com/,
  /rttnews\.com/, /wallstreetzen\.com/, /defenseworld\.net/,
];
const SOCIAL_PATTERNS = [
  /twitter\.com/, /(^|\.)x\.com/, /reddit\.com/, /facebook\.com/,
  /linkedin\.com/, /tiktok\.com/, /youtube\.com/,
];

/** Source quality tier: official/regulator 4, reputable 3, other 2, aggregator 1, social 0. */
export function sourceTier(f: Pick<QueryFinding, "source" | "url" | "source_quality">): number {
  const hay = `${hostOf(f.url)} ${f.source ?? ""}`.toLowerCase();
  const q = f.source_quality ?? 0;
  if (SOCIAL_PATTERNS.some((r) => r.test(hay))) return 0;
  if (OFFICIAL_PATTERNS.some((r) => r.test(hay))) return 4;
  if (AGGREGATOR_PATTERNS.some((r) => r.test(hay))) return 1;
  if (REPUTABLE_PATTERNS.some((r) => r.test(hay))) return 3;
  if (q >= 0.9) return 4;
  if (q >= 0.7) return 3;
  if (q >= 0.5) return 2;
  if (q >= 0.3) return 1;
  return 2;
}

function findingStrength(f: QueryFinding): number {
  return (
    sourceTier(f) * 100 +
    (f.source_quality ?? 0) * 10 +
    (f.confidence ?? 0) * 5 +
    (f.relevance ?? 0)
  );
}

/* ------------------------------ presentation ------------------------------ */

/** Order findings so official/regulator and reputable sources come first. */
export function prioritizeFindings(findings: MergedFinding[]): MergedFinding[] {
  return [...findings].sort(
    (a, b) =>
      sourceTier(b) - sourceTier(a) ||
      (b.relevance ?? 0) - (a.relevance ?? 0) ||
      (b.confidence ?? 0) - (a.confidence ?? 0)
  );
}

export type ClaimStatus = "confirmed" | "reported" | "unverified";

const UNVERIFIED_PATTERN =
  /\b(unverified|unconfirmed|rumou?r(?:ed|s)?|speculat(?:ion|ive|ed)|alleged(?:ly)?|no confirmation|not confirmed|could not be verified)\b/i;
const REPORTED_PATTERN =
  /\b(reportedly|reported|according to|sources? (?:said|told)|citing|is said to|claims?|claimed|suggests?|suggested|may|might|could|is expected to|plans? to|is set to|in talks)\b/i;

/**
 * Classify a finding from the text itself: explicit unverified language wins;
 * official/regulator sources with no hedging are confirmed facts; everything
 * else is a reported claim. Never asserts more than the evidence supports.
 */
export function claimStatus(f: QueryFinding): ClaimStatus {
  const text = `${f.title} ${f.key_finding} ${f.significance}`;
  if (UNVERIFIED_PATTERN.test(text)) return "unverified";
  if (sourceTier(f) >= 4 && !REPORTED_PATTERN.test(text)) return "confirmed";
  return "reported";
}

const RELATIVE_PATTERN =
  /\b(\d+\s*(?:day|week|month|hour|minute)s?\s*ago|yesterday|today|last (?:week|month|night)|this (?:week|month)|recently|earlier (?:this|today))\b/i;
const APPROX_PATTERN =
  /\b(approx\.?|approximately|circa|roughly|undated|no exact date|no precise date|no calendar date|relative(?:ly)?)\b/i;
const MONTH_WORD =
  "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|" +
  "aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?";
const PARTIAL_MONTH_PATTERN = new RegExp(
  `\\b(?:early|late|mid)[-\\s](?:${MONTH_WORD}|q[1-4]|\\d{4})\\b`,
  "i"
);

/**
 * Date label for a finding. Exact ISO dates render as an exact date; when the
 * text hedges or uses a relative timestamp the date is marked approximate. A
 * missing date is reported as "date not stated" — never invented.
 */
export function dateLabel(f: QueryFinding): { text: string; approximate: boolean } {
  const text = `${f.title} ${f.key_finding} ${f.significance}`;
  const approximate =
    RELATIVE_PATTERN.test(text) ||
    APPROX_PATTERN.test(text) ||
    PARTIAL_MONTH_PATTERN.test(text);
  const iso = f.published_at;
  if (!iso) return { text: "date not stated", approximate };
  const exact = formatDate(iso);
  if (exact === "—") return { text: "date not stated", approximate };
  return { text: approximate ? `approx. ${exact}` : exact, approximate };
}

/* ------------------------------ deduplication ----------------------------- */

/** Two findings describe the same underlying event? */
export function sameEvent(a: QueryFinding, b: QueryFinding): boolean {
  const ua = normalizeUrl(a.url);
  const ub = normalizeUrl(b.url);
  if (ua && ua === ub) return true;

  const ka = normalizeText(a.key_finding);
  const kb = normalizeText(b.key_finding);
  if (ka && ka === kb) return true;

  const sa = strongSignals(`${a.title} ${a.key_finding} ${a.significance}`);
  const sb = strongSignals(`${b.title} ${b.key_finding} ${b.significance}`);
  let shared = 0;
  for (const s of sa) if (sb.includes(s)) shared++;

  const sim = jaccard(eventTokens(a), eventTokens(b));
  if (shared >= 1 && sim >= 0.34) return true;
  return sim >= 0.6;
}

export interface CorroboratingSource {
  source: string;
  url: string;
  published_at: string | null;
}

export interface MergedFinding extends QueryFinding {
  also_reported_by: CorroboratingSource[];
}

function corroborating(f: QueryFinding): CorroboratingSource {
  return { source: f.source, url: f.url, published_at: f.published_at };
}

function withoutPrimary(list: CorroboratingSource[], primaryUrl: string): CorroboratingSource[] {
  const primaryKey = normalizeUrl(primaryUrl);
  const seen = new Set<string>();
  const out: CorroboratingSource[] = [];
  for (const c of list) {
    const key = normalizeUrl(c.url);
    if (!key || key === primaryKey || seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

/**
 * Merge findings that describe the same event. The strongest source is kept
 * as the primary; other publishers become corroborating sources. Distinct
 * developments (even from the same company) are preserved.
 */
export function mergeFindings(findings: QueryFinding[]): MergedFinding[] {
  const groups: MergedFinding[] = [];
  for (const f of findings) {
    const idx = groups.findIndex((m) => sameEvent(m, f));
    if (idx === -1) {
      groups.push({ ...f, also_reported_by: [] });
      continue;
    }
    const current = groups[idx];
    if (findingStrength(f) > findingStrength(current)) {
      const others = [...current.also_reported_by, corroborating(current)];
      groups[idx] = { ...f, also_reported_by: withoutPrimary(others, f.url) };
    } else {
      const others = [...current.also_reported_by, corroborating(f)];
      current.also_reported_by = withoutPrimary(others, current.url);
    }
  }
  return groups;
}

/** Remove duplicate stories (same URL or same event). */
export function dedupeFindings(findings: QueryFinding[]): QueryFinding[] {
  return mergeFindings(findings).map(({ also_reported_by: _ignored, ...f }) => f);
}

/**
 * Drop key points that merely repeat a detailed finding or another key point.
 * Short, distinct takeaways are kept; only repeated explanations are removed.
 */
export function dedupeKeyPoints(
  keyPoints: string[],
  findings: QueryFinding[]
): string[] {
  const findingKfNorm = findings.map((f) => normalizeText(f.key_finding)).filter(Boolean);
  const findingKf = findings.map((f) => keyPointTokens(f.key_finding));
  const out: string[] = [];
  const seen = new Set<string>();
  for (const kp of keyPoints) {
    const norm = normalizeText(kp);
    if (!norm || seen.has(norm)) continue;
    const kpTokens = keyPointTokens(kp);
    // Drop an exact repeat of a finding's key sentence, or a near-identical one.
    if (findingKfNorm.includes(norm)) continue;
    if (findingKf.some((ft) => ft.size > 0 && jaccard(kpTokens, ft) >= 0.85)) continue;
    if (out.some((prev) => jaccard(keyPointTokens(prev), kpTokens) >= 0.8)) continue;
    seen.add(norm);
    out.push(kp);
  }
  return out;
}
