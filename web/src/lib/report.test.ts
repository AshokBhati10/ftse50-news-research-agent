import { describe, expect, it } from "vitest";
import {
  claimStatus,
  dateLabel,
  dedupeFindings,
  dedupeKeyPoints,
  extractHighlights,
  mergeFindings,
  normalizeText,
  prioritizeFindings,
  sameEvent,
  sourceTier,
} from "./report";
import type { QueryFinding } from "../types";

function finding(overrides: Partial<QueryFinding> = {}): QueryFinding {
  return {
    title: "A headline",
    url: "https://example.com/a",
    source: "example.com",
    published_at: "2026-10-05T00:00:00+00:00",
    relevance: 0.8,
    key_finding: "Something happened.",
    significance: "It matters.",
    impact: "medium",
    sentiment: "neutral",
    reason: "Relevant.",
    confidence: 0.7,
    source_quality: 0.5,
    evidence_excerpt: "",
    ...overrides,
  };
}

describe("extractHighlights", () => {
  it("picks subject company names and ignores role/stop words", () => {
    expect(extractHighlights("Who is the CEO of HSBC Holdings?")).toEqual(["HSBC"]);
    expect(extractHighlights("Latest developments in semiconductor stocks")).toEqual([]);
    expect(extractHighlights("AstraZeneca latest news")).toEqual(["AstraZeneca"]);
    expect(extractHighlights(null)).toEqual([]);
  });
});

describe("dedupeFindings", () => {
  it("removes findings that share a URL or repeat the same story", () => {
    const out = dedupeFindings([
      finding({ url: "https://example.com/deal", title: "Deal done", key_finding: "Deal completed." }),
      finding({ url: "https://example.com/deal?ref=x", title: "Deal done again", key_finding: "Deal completed." }),
      finding({ url: "https://example.com/other", title: "Other story", key_finding: "Something else." }),
    ]);
    expect(out).toHaveLength(2);
    expect(out.map((f) => f.url)).toEqual([
      "https://example.com/deal",
      "https://example.com/other",
    ]);
  });

  it("keeps distinct stories", () => {
    const out = dedupeFindings([
      finding({ url: "https://a.com/1", title: "Chips rally", key_finding: "Demand rose." }),
      finding({ url: "https://b.com/2", title: "Banks fall", key_finding: "Margins fell." }),
    ]);
    expect(out).toHaveLength(2);
  });
});

describe("dedupeKeyPoints", () => {
  it("drops key points that repeat a detailed finding", () => {
    const findings = [
      finding({ title: "Deal done", key_finding: "AstraZeneca completed the 2bn deal." }),
    ];
    const out = dedupeKeyPoints(
      [
        "AstraZeneca completed the 2bn deal.", // repeats the finding -> dropped
        "Guidance for the year was raised.", // distinct -> kept
      ],
      findings
    );
    expect(out).toEqual(["Guidance for the year was raised."]);
  });

  it("drops duplicate key points but keeps distinct short takeaways", () => {
    const out = dedupeKeyPoints(
      [
        "Demand rose sharply.",
        "Demand rose sharply.", // exact duplicate
        "Margins improved.",
      ],
      []
    );
    expect(out).toEqual(["Demand rose sharply.", "Margins improved."]);
  });

  it("keeps a short takeaway that is merely a subset of a finding", () => {
    const findings = [
      finding({ key_finding: "Demand rose sharply across the quarter." }),
    ];
    expect(dedupeKeyPoints(["Demand rose sharply."], findings)).toEqual([
      "Demand rose sharply.",
    ]);
  });
});

describe("normalizeText", () => {
  it("lowercases and strips punctuation but keeps figures", () => {
    expect(normalizeText("Up 33% to £4.7bn!")).toBe("up 33% to £4.7bn");
  });
});

describe("sameEvent / mergeFindings (cross-publisher)", () => {
  const reuters = finding({
    title: "AstraZeneca completes $2bn Summit deal",
    url: "https://www.reuters.com/markets/azn-summit",
    source: "reuters.com",
    source_quality: 0.8,
    key_finding: "AstraZeneca completed its $2bn equity investment in Summit Therapeutics.",
    significance: "Expands the oncology pipeline.",
  });
  const yahoo = finding({
    title: "AstraZeneca closes Summit investment",
    url: "https://finance.yahoo.com/news/azn-summit",
    source: "finance.yahoo.com",
    source_quality: 0.4,
    key_finding: "AstraZeneca closed its $2bn investment in Summit Therapeutics.",
    significance: "Boosts its cancer drug pipeline.",
  });

  it("detects the same underlying event across different publishers", () => {
    expect(sameEvent(reuters, yahoo)).toBe(true);
  });

  it("keeps the strongest source and records corroborating coverage", () => {
    const merged = mergeFindings([reuters, yahoo]);
    expect(merged).toHaveLength(1);
    expect(merged[0].url).toBe(reuters.url);
    expect(merged[0].also_reported_by.map((c) => c.url)).toEqual([yahoo.url]);
  });

  it("merges the duplicate even when the stronger source arrives second", () => {
    const merged = mergeFindings([yahoo, reuters]);
    expect(merged).toHaveLength(1);
    expect(merged[0].url).toBe(reuters.url);
  });

  it("preserves genuinely different events from the same company", () => {
    const guidance = finding({
      title: "AstraZeneca lowers guidance",
      url: "https://www.reuters.com/markets/azn-guidance",
      source: "reuters.com",
      key_finding: "AstraZeneca lowered its full-year guidance.",
      significance: "Cost pressures weighed on margins.",
    });
    const merged = mergeFindings([reuters, guidance]);
    expect(merged).toHaveLength(2);
  });
});

describe("sourceTier", () => {
  it("ranks official/regulator above reputable above aggregator above social", () => {
    const tier = (source: string, url: string, q = 0.5) =>
      sourceTier(finding({ source, url, source_quality: q }));
    expect(tier("sec.gov", "https://www.sec.gov/filing/1", 0.9)).toBe(4);
    expect(tier("reuters.com", "https://www.reuters.com/a", 0.8)).toBe(3);
    expect(tier("finance.yahoo.com", "https://finance.yahoo.com/news/a", 0.4)).toBe(1);
    expect(tier("x.com", "https://x.com/company/status/1", 0.9)).toBe(0);
    // Official company domain with top source quality ranks as official.
    expect(tier("astrazeneca.com", "https://www.astrazeneca.com/media/1", 1.0)).toBe(4);
  });

  it("prioritises higher-tier sources in the rendered order", () => {
    const yahoo = finding({ source: "finance.yahoo.com", url: "https://finance.yahoo.com/a", source_quality: 0.4, relevance: 0.95 });
    const reuters = finding({ source: "reuters.com", url: "https://www.reuters.com/a", source_quality: 0.8, relevance: 0.6 });
    const ordered = prioritizeFindings(mergeFindings([yahoo, reuters]));
    expect(ordered[0].url).toBe(reuters.url);
  });
});

describe("dateLabel", () => {
  it("shows an exact date when supported and not hedged", () => {
    const f = finding({ published_at: "2026-10-05T00:00:00+00:00", key_finding: "AstraZeneca completed the deal." });
    expect(dateLabel(f)).toEqual({ text: "5 Oct 2026", approximate: false });
  });

  it("reports a missing date without inventing one", () => {
    const f = finding({ published_at: null });
    expect(dateLabel(f).text).toBe("date not stated");
  });

  it("labels relative timestamps and partial months as approximate", () => {
    const relative = finding({ published_at: "2026-10-05T00:00:00+00:00", key_finding: "The deal closed 3 days ago." });
    expect(dateLabel(relative)).toEqual({ text: "approx. 5 Oct 2026", approximate: true });
    const partial = finding({ published_at: "2026-10-05T00:00:00+00:00", key_finding: "Reported in early October 2026." });
    expect(dateLabel(partial).text).toBe("approx. 5 Oct 2026");
  });
});

describe("claimStatus", () => {
  it("distinguishes confirmed, reported and unverified findings", () => {
    expect(
      claimStatus(finding({ source: "sec.gov", url: "https://www.sec.gov/a", source_quality: 0.9, key_finding: "The board approved the merger." }))
    ).toBe("confirmed");
    expect(
      claimStatus(finding({ source: "reuters.com", url: "https://www.reuters.com/a", source_quality: 0.8, key_finding: "The company plans to cut costs." }))
    ).toBe("reported");
    expect(
      claimStatus(finding({ source: "example.com", url: "https://example.com/a", source_quality: 0.5, key_finding: "An unconfirmed rumour about a takeover." }))
    ).toBe("unverified");
  });
});
