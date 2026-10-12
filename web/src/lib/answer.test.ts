import { describe, expect, it } from "vitest";
import { boldKeyTerms, extractDate, formatAnswerMarkdown, splitSentences } from "./answer";

describe("formatAnswerMarkdown", () => {
  it("leaves already-structured Markdown without key terms untouched", () => {
    const md = "## Summary\n\nAstraZeneca did X.\n\n- point one\n- point two";
    expect(formatAnswerMarkdown(md)).toBe(md);
  });

  it("bolds plain dates/figures in structured Markdown but not headings, links or existing bold", () => {
    const md = [
      "## Update 2026-10-05",
      "- Shares rose 5% on 5 Oct 2026 [1] (**£2bn** already bold).",
      "See [report 2026-10-05](https://x.example/2026-10-05) and https://y.example/2026-10-05.",
    ].join("\n");
    expect(formatAnswerMarkdown(md)).toBe(
      [
        "## Update 2026-10-05",
        "- Shares rose **5%** on **5 Oct 2026** [1] (**£2bn** already bold).",
        "See [report 2026-10-05](https://x.example/2026-10-05) and https://y.example/2026-10-05.",
      ].join("\n")
    );
  });

  it("does not double-bold text that already contains emphasis", () => {
    const md = "Completed on **5 Oct 2026** for **$2bn**.";
    expect(formatAnswerMarkdown(md)).toBe(md);
  });

  it("bolds dates, figures and tickers in a single sentence", () => {
    expect(formatAnswerMarkdown("Shares rose 5% on 5 Oct 2026.")).toBe(
      "Shares rose **5%** on **5 Oct 2026**."
    );
    expect(formatAnswerMarkdown("AZN.L rose.")).toBe("**AZN.L** rose.");
  });

  it("reflows a dense paragraph into short paragraphs, not bullets", () => {
    const out = formatAnswerMarkdown(
      "AstraZeneca did X. It also did Y. And Z. Fourth thing."
    );
    expect(out).toBe("AstraZeneca did X. It also did Y.\n\nAnd Z. Fourth thing.");
    expect(out).not.toContain("- ");
  });

  it("keeps a short answer as a single short paragraph", () => {
    expect(formatAnswerMarkdown("First development happened. Second one too.")).toBe(
      "First development happened. Second one too."
    );
  });

  it("bolds provided subject company names", () => {
    expect(
      formatAnswerMarkdown("AstraZeneca did X and AstraZeneca did Y.", ["AstraZeneca"])
    ).toBe("**AstraZeneca** did X and **AstraZeneca** did Y.");
  });

  it("preserves line-based answers as separate paragraphs", () => {
    expect(
      formatAnswerMarkdown("Company: AstraZeneca\nDate: 5 Oct 2026")
    ).toBe("Company: AstraZeneca\n\nDate: **5 Oct 2026**");
  });
});

describe("splitSentences", () => {
  it("splits on sentence boundaries", () => {
    expect(splitSentences("One thing. Two things! Three things?")).toEqual([
      "One thing.",
      "Two things!",
      "Three things?",
    ]);
  });

  it("does not split decimals, pence or abbreviations", () => {
    expect(splitSentences("The yield rose to 9.5p. Shares fell.")).toEqual([
      "The yield rose to 9.5p.",
      "Shares fell.",
    ]);
    expect(splitSentences("Approx. 7 October 2026 was notable. It mattered.")).toEqual([
      "Approx. 7 October 2026 was notable.",
      "It mattered.",
    ]);
  });
});

describe("boldKeyTerms", () => {
  it("bolds monetary amounts, percentages and dates", () => {
    expect(boldKeyTerms("Up 33% to £4.7bn by 24 September 2026.")).toBe(
      "Up **33%** to **£4.7bn** by **24 September 2026**."
    );
  });
});

describe("extractDate", () => {
  it("prefers the reported/published date over other dates", () => {
    expect(
      extractDate("revenue for the quarter ended 26 July 2026, reported 26 August 2026")
    ).toBe("26 August 2026");
    expect(extractDate("Deal closed on 5 October 2026.")).toBe("5 October 2026");
    expect(extractDate("No date here.")).toBeNull();
  });
});
