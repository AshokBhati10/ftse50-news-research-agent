import { describe, expect, it } from "vitest";
import { buildExecutiveSummary, keyPointLabel } from "./executiveSummary";
import type { QueryAnswer } from "../types";

const answer = (over: Partial<QueryAnswer> = {}): QueryAnswer => ({
  query: "q",
  task_type: "news_research",
  answer: "NVIDIA added $150 billion to its buyback on 28 September 2026.\n\nSecond paragraph.",
  key_points: [
    "Record buyback: NVIDIA added $150 billion, reported 28 September 2026.",
    "Open Agent Safety Platform launched with no financial impact quantified.",
  ],
  sources: [
    {
      title: "Buyback [WSJ]",
      url: "https://www.wsj.com/a",
      source_name: "wsj.com",
      published_at: "2026-09-28T00:00:00+00:00",
      supporting_excerpt: "",
      supports_claims: [0],
    },
  ],
  confidence: 0.5,
  limitations: [
    "Sources report slightly different revenue figures.",
    "All evidence items are short snippets.",
  ],
  verified: true,
  ...over,
});

describe("buildExecutiveSummary", () => {
  it("composes exactly four sections from existing report data", () => {
    const md = buildExecutiveSummary(answer());
    const headings = md.split("\n").filter((l) => l.startsWith("## "));
    expect(headings).toEqual([
      "## Overview",
      "## Key Developments",
      "## Financial Impact",
      "## Risks & Conflicts",
    ]);
    // Overview keeps the full answer text.
    expect(md).toContain("Second paragraph.");
    // The development itself, not just its label; the key point already states its date.
    expect(md).toContain("- **Record buyback:** NVIDIA added $150 billion, reported 28 September 2026.\n");
    expect(md).toContain("- Open Agent Safety Platform launched with no financial impact quantified.");
    // No source links (those stay in Key Findings) and no placeholder for unknown dates.
    expect(md).not.toContain("wsj.com");
    expect(md).not.toContain("date not stated");
    // Figures are pulled verbatim from key points.
    expect(md).toContain("- **Record buyback:** **$150 billion**");
    // Only conflict/uncertainty notes appear; missing-data notes do not.
    expect(md).toContain("- Sources report slightly different revenue figures.");
    expect(md).not.toContain("short snippets");
    expect(md).not.toMatch(/Verified vs|Evidence Limitations/);
  });

  it("adds the source date only when the key point states none", () => {
    const md = buildExecutiveSummary(
      answer({ key_points: ["Buyback: NVIDIA expanded its repurchase programme."] })
    );
    expect(md).toContain("- **Buyback:** NVIDIA expanded its repurchase programme (28 Sept 2026)");
  });

  it("keeps a date stated in the key point when the source is undated", () => {
    const md = buildExecutiveSummary(
      answer({
        key_points: ["Reflection AI talks: reported 10 October 2026 by Reuters."],
        sources: [{ title: "t", url: "https://reuters.com/x", source_name: "reuters.com",
          published_at: null, supporting_excerpt: "", supports_claims: [0] }],
      })
    );
    expect(md).toContain("**Reflection AI talks:** reported 10 October 2026 by Reuters.");
    expect(md).not.toContain("date not stated");
  });

  it("omits sections with no supporting data and never invents content", () => {
    const md = buildExecutiveSummary(
      answer({ key_points: [], sources: [], limitations: [] })
    );
    expect(md.startsWith("## Overview")).toBe(true);
    expect(md).not.toMatch(/## (Key Developments|Financial Impact|Risks)/);
  });

  it("leaves an answer that already has Markdown structure untouched", () => {
    const own = "## Summary\n\n- point **5%**";
    expect(buildExecutiveSummary(answer({ answer: own }))).toBe(own);
  });

  it("derives a short headline from a key point", () => {
    expect(keyPointLabel("Record buyback: NVIDIA added money.")).toBe("Record buyback");
    expect(keyPointLabel("Short sentence here. Another one.")).toBe("Short sentence here");
    expect(keyPointLabel("word ".repeat(40)).endsWith("…")).toBe(true);
  });
});
