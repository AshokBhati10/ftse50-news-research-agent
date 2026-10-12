/** Answer-first report rendering: the direct answer leads, evidence supports it. */
import { describe, expect, it, vi } from "vitest";
import { within, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ReportDetailPage from "./ReportDetailPage";
import type { ReportDetail } from "../../types";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<any>();
  return {
    ...actual,
    api: {
      ...actual.api,
      report: vi.fn(),
    },
  };
});

import { api } from "../../lib/api";

function answerReport(): ReportDetail {
  return {
    summary: {
      id: "report_answer_1",
      date: new Date().toISOString().slice(0, 10),
      report_type: "query",
      title: "Who is the CEO of HSBC Holdings?",
      generated_at: new Date().toISOString(),
      company_count: 0,
      article_count: 0,
      companies: [],
    },
    companies: [],
    findings: [],
    answer: {
      query: "Who is the CEO of HSBC Holdings?",
      task_type: "factual_lookup",
      answer: "Georges Elhedery is the Group Chief Executive of HSBC Holdings plc.",
      key_points: ["HSBC appointed Georges Elhedery as Group Chief Executive."],
      sources: [
        {
          title: "HSBC confirms Georges Elhedery as Group Chief Executive",
          url: "https://www.hsbc.com/news/2024/elhedery-ceo",
          source_name: "hsbc.com",
          published_at: "2024-09-06T00:00:00+00:00",
          supporting_excerpt: "HSBC Holdings plc announces Georges Elhedery as Group Chief Executive.",
          supports_claims: [0],
        },
      ],
      confidence: 0.95,
      limitations: [],
      verified: true,
    },
    query: "Who is the CEO of HSBC Holdings?",
    metadata: {
      run_id: "run_a1",
      search_config: { task_type: "factual_lookup" },
      counts: { tool_calls: 1, evidence_items: 2, sources_cited: 1 },
      plan_summary: {
        task_type: "factual_lookup",
        intent_summary: "Find HSBC's current CEO.",
        tool_trace: [
          {
            tool: "web_search",
            arguments_summary: "query='HSBC Holdings Group Chief Executive'",
            observation_summary: "2 results (searxng)",
            ok: true,
          },
        ],
      },
      warnings: [],
    },
    has_markdown: true,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/reports/report_answer_1"]}>
      <Routes>
        <Route path="/reports/:reportId" element={<ReportDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

/** Matcher that checks a paragraph's full text (bold spans split direct text). */
function paragraphWith(text: string) {
  return (_content: string, element: Element | null) =>
    !!element && element.tagName === "P" && element.textContent === text;
}

describe("Answer-first report", () => {
  it("leads with the direct answer and keeps downloads at the bottom", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    const answer = await screen.findByText(
      paragraphWith("Georges Elhedery is the Group Chief Executive of HSBC Holdings plc.")
    );
    expect(answer).toBeInTheDocument();
    expect(screen.getByText("Executive Summary")).toBeInTheDocument();

    // JSON + Markdown downloads are present at the bottom of the report.
    expect(
      screen.getByRole("link", { name: /Download JSON/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Download Markdown/i })
    ).toBeInTheDocument();
  });

  it("shows only conflict/uncertainty notes in the summary, never a Caveats section, evidence note or limitations section", async () => {
    const rep = answerReport();
    rep.answer!.limitations = [
      "The publication date is approximate.",
      "One item is reported but unverified.",
    ];
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findByText("Executive Summary");
    expect(
      screen.queryByRole("heading", { name: "Caveats" })
    ).not.toBeInTheDocument();
    // Uncertainty goes under Risks & Conflicts; missing-data notes are omitted.
    const card = screen.getByText("Executive Summary").parentElement as HTMLElement;
    expect(
      within(card).getByRole("heading", { name: "Risks & Conflicts" })
    ).toBeInTheDocument();
    expect(within(card).getByText("One item is reported but unverified.")).toBeInTheDocument();
    expect(
      within(card).queryByRole("heading", { name: "Evidence Limitations" })
    ).not.toBeInTheDocument();
    expect(
      within(card).queryByRole("heading", { name: "Verified vs. Reported" })
    ).not.toBeInTheDocument();
    expect(
      within(card).queryByText("The publication date is approximate.")
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Evidence note/)).not.toBeInTheDocument();
    expect(screen.queryByText(/limitations apply/)).not.toBeInTheDocument();
  });

  it("renders a Markdown answer as formatted paragraphs and lists", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.answer!.answer = [
      "AstraZeneca completed a **$2bn** equity investment in Summit Therapeutics.",
      "",
      "1. **5 Oct 2026** — completion reported.",
      "2. **2 Oct 2026** — collaboration announced.",
    ].join("\n");
    vi.mocked(api.report).mockResolvedValue(rep);
    const { container } = renderPage();
    await screen.findByText(/AstraZeneca completed a/);
    // Bold renders as a real element; no literal Markdown markers remain.
    expect(container.querySelector("strong")?.textContent).toBe("$2bn");
    expect(container.textContent).not.toContain("**");
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("renders a sectioned executive summary with headings, lists, bold and links", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.answer!.limitations = ["Some sources were paywalled."];
    rep.answer!.answer = [
      "## Overview",
      "NVIDIA raised its buyback by **$150B** on **28 Sep 2026**.",
      "",
      "## Key Developments",
      "- **28 Sep 2026** — buyback increase ([NVIDIA](https://nvidianews.nvidia.com/news/x)).",
      "- **8 Oct 2026** — $1B US science commitment.",
      "",
      "## Financial Impact",
      "1. Authorization now **$235B** through fiscal 2028.",
      "",
      "## Verified vs. Reported",
      "- Verified: buyback (https://nvidianews.nvidia.com/news/x).",
      "- Reported: SpaceX seeks **$40B** financing.",
      "",
      "## Risks & Conflicts",
      "- Outlets differ on whether Reflection AI is an acquisition.",
      "",
      "## Evidence Limitations",
      "Some sources were paywalled.",
    ].join("\n");
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findByText(/NVIDIA raised its buyback/);

    const card = screen.getByText("Executive Summary").parentElement as HTMLElement;
    const headings = Array.from(card.querySelectorAll("h2")).map((h) => h.textContent);
    expect(headings).toEqual([
      "Overview",
      "Key Developments",
      "Financial Impact",
      "Verified vs. Reported",
      "Risks & Conflicts",
      "Evidence Limitations",
    ]);
    expect(card.querySelectorAll("ul li").length).toBe(5);
    expect(card.querySelectorAll("ol li")).toHaveLength(1);
    const strongs = Array.from(card.querySelectorAll("strong")).map((s) => s.textContent);
    expect(strongs).toEqual(expect.arrayContaining(["$150B", "28 Sep 2026", "$235B", "$40B"]));
    const hrefs = Array.from(card.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual([
      "https://nvidianews.nvidia.com/news/x",
      "https://nvidianews.nvidia.com/news/x",
    ]);
    expect(card.textContent).not.toContain("**");
    expect(card.textContent).not.toContain("##");
    // No status-count badges or separate evidence note under the summary.
    expect(screen.queryByText(/Evidence note/)).not.toBeInTheDocument();
    expect(screen.queryByText(/limitations apply/)).not.toBeInTheDocument();
  });

  it("reflows a dense prose answer into short paragraphs with bold figures", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.answer!.answer =
      "AstraZeneca completed a $2bn deal on 5 Oct 2026. " +
      "It also reported rising revenue. Guidance was raised.";
    vi.mocked(api.report).mockResolvedValue(rep);
    const { container } = renderPage();
    await screen.findByText(/AstraZeneca completed a/);

    // Prose is reflowed into short paragraphs, not mechanical bullets.
    expect(container.querySelectorAll("ul li")).toHaveLength(0);
    // The answer paragraph block has more than one paragraph.
    const answerPs = Array.from(container.querySelectorAll("p")).filter((p) =>
      (p.textContent ?? "").includes("AstraZeneca completed")
    );
    expect(answerPs.length).toBeGreaterThanOrEqual(1);
    // Financial figure and date are bold.
    const strongs = Array.from(container.querySelectorAll("strong")).map(
      (s) => s.textContent
    );
    expect(strongs).toContain("$2bn");
    expect(strongs).toContain("5 Oct 2026");
  });

  it("renders Markdown headings and lists from an already-Markdown answer", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.answer!.answer = [
      "## Overview",
      "",
      "AstraZeneca reported several developments.",
      "",
      "### Details",
      "",
      "- Completion reported on 5 Oct 2026.",
      "- Collaboration announced on 2 Oct 2026.",
    ].join("\n");
    vi.mocked(api.report).mockResolvedValue(rep);
    const { container } = renderPage();
    await screen.findByText("AstraZeneca reported several developments.");

    expect(
      screen.getByRole("heading", { name: "Overview", level: 2 })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Details", level: 3 })
    ).toBeInTheDocument();
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    // No raw Markdown markers are shown.
    expect(container.textContent).not.toContain("##");
    expect(container.textContent).not.toContain("- Completion");
  });

  it("shows Key Findings with inline citations", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    await screen.findByText("Key Findings");
    expect(
      screen.getByText("HSBC appointed Georges Elhedery as Group Chief Executive.")
    ).toBeInTheDocument();
    // The source URL is linked inline under the key finding.
    const keyFindings = screen
      .getByText("HSBC appointed Georges Elhedery as Group Chief Executive.")
      .closest("li") as HTMLElement;
    const links = within(keyFindings)
      .getAllByRole("link")
      .filter(
        (el) =>
          el.getAttribute("href") ===
          "https://www.hsbc.com/news/2024/elhedery-ceo"
      );
    expect(links.length).toBe(1);
    for (const link of links) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
  });

  it("omits the Sources and Limitations sections", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    await screen.findByText(paragraphWith("Georges Elhedery is the Group Chief Executive of HSBC Holdings plc."));
    expect(
      screen.queryByRole("heading", { name: /Sources/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /Limitations/i })
    ).not.toBeInTheDocument();
  });

  it("shows both Key Findings and Findings sections", async () => {
    const rep = answerReport();
    rep.findings = [
      {
        title: "AstraZeneca deal headline",
        url: "https://example.com/deal",
        source: "reuters.com",
        published_at: "2026-10-05T00:00:00+00:00",
        relevance: 0.9,
        key_finding: "AstraZeneca completed the deal.",
        significance: "Expands the oncology pipeline.",
        impact: "high",
        sentiment: "positive",
        reason: "Material deal.",
        confidence: 0.8,
        source_quality: 0.8,
        evidence_excerpt: "Excerpt.",
      },
    ];
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findByText("AstraZeneca completed the deal.");

    // Both sections are present — neither is merged or removed.
    expect(
      screen.getByRole("heading", { name: "Key Findings" })
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Findings" })).toBeInTheDocument();
    // Key point takeaway appears alongside the detailed finding.
    expect(
      screen.getByText("HSBC appointed Georges Elhedery as Group Chief Executive.")
    ).toBeInTheDocument();

    // Finding details: business impact, publication date and clickable link.
    expect(screen.getByText(/high impact/i)).toBeInTheDocument();
    expect(screen.getByText(/5 Oct 2026/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "AstraZeneca deal headline" });
    expect(link).toHaveAttribute("href", "https://example.com/deal");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("removes duplicate stories and repeated explanations across both sections", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [
      "AstraZeneca completed the 2bn deal.", // repeats a finding -> dropped
      "Guidance for the year was raised.", // distinct -> kept
    ];
    rep.findings = [
      {
        title: "Deal completed",
        url: "https://example.com/deal",
        source: "reuters.com",
        published_at: "2026-10-05T00:00:00+00:00",
        relevance: 0.9,
        key_finding: "AstraZeneca completed the 2bn deal.",
        significance: "Expands the oncology pipeline.",
        impact: "high",
        sentiment: "positive",
        reason: "Material deal.",
        confidence: 0.8,
        source_quality: 0.8,
        evidence_excerpt: "Excerpt.",
      },
      {
        // Same story syndicated elsewhere — duplicate, must be removed.
        title: "Deal completed",
        url: "https://wire.example/deal",
        source: "wire.example",
        published_at: "2026-10-05T00:00:00+00:00",
        relevance: 0.7,
        key_finding: "AstraZeneca completed the 2bn deal.",
        significance: "Expands the oncology pipeline.",
        impact: "high",
        sentiment: "positive",
        reason: "Material deal.",
        confidence: 0.7,
        source_quality: 0.6,
        evidence_excerpt: "Excerpt.",
      },
    ];
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findAllByText("Guidance for the year was raised.");

    // The Key Findings card does not repeat what the detailed Findings already say. (The Executive Summary
    // states every key development itself, so the text is not unique on the page.)
    const keyFindings = screen.getByText("Key Findings").closest(".rounded-xl") as HTMLElement;
    expect(within(keyFindings).getByText("Guidance for the year was raised.")).toBeInTheDocument();
    expect(within(keyFindings).queryByText("AstraZeneca completed the 2bn deal.")).not.toBeInTheDocument();
    // The duplicate syndicated finding is removed (one story, one link).
    const dealLinks = screen
      .getAllByRole("link", { name: /deal/i })
      .filter((l) => l.getAttribute("href") === "https://example.com/deal");
    expect(dealLinks).toHaveLength(1);
  });

  it("keeps the strongest publisher and lists corroborating coverage", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.findings = [
      {
        title: "AstraZeneca completes $2bn Summit deal",
        url: "https://www.reuters.com/markets/azn-summit",
        source: "reuters.com",
        published_at: "2026-10-05T00:00:00+00:00",
        relevance: 0.6,
        key_finding: "AstraZeneca completed its $2bn equity investment in Summit Therapeutics.",
        significance: "Expands the oncology pipeline.",
        impact: "high",
        sentiment: "positive",
        reason: "Material.",
        confidence: 0.8,
        source_quality: 0.8,
        evidence_excerpt: "Excerpt.",
      },
      {
        // Same event, lower-tier publisher — must merge, not duplicate.
        title: "AstraZeneca closes Summit investment",
        url: "https://finance.yahoo.com/news/azn-summit",
        source: "finance.yahoo.com",
        published_at: "2026-10-05T00:00:00+00:00",
        relevance: 0.95,
        key_finding: "AstraZeneca closed its $2bn investment in Summit Therapeutics.",
        significance: "Boosts its cancer drug pipeline.",
        impact: "high",
        sentiment: "positive",
        reason: "Material.",
        confidence: 0.7,
        source_quality: 0.4,
        evidence_excerpt: "Excerpt.",
      },
    ];
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findByText("Also reported by");

    // The duplicate story is not repeated as its own finding.
    expect(
      screen.queryByRole("link", { name: "AstraZeneca closes Summit investment" })
    ).not.toBeInTheDocument();
    // Strongest source is the primary link; the other is corroboration.
    expect(
      screen.getByRole("link", { name: "AstraZeneca completes $2bn Summit deal" })
    ).toHaveAttribute("href", "https://www.reuters.com/markets/azn-summit");
    expect(
      screen.getByRole("link", { name: "finance.yahoo.com" })
    ).toHaveAttribute("href", "https://finance.yahoo.com/news/azn-summit");
  });

  it("labels uncertain dates and distinguishes unverified claims", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.findings = [
      {
        title: "AstraZeneca board approval",
        url: "https://example.com/approval",
        source: "example.com",
        published_at: null,
        relevance: 0.7,
        key_finding: "The board approved the transaction.",
        significance: "Clears a key hurdle.",
        impact: "medium",
        sentiment: "neutral",
        reason: "Relevant.",
        confidence: 0.6,
        source_quality: 0.5,
        evidence_excerpt: "",
      },
      {
        title: "Takeover rumour",
        url: "https://example.com/rumour",
        source: "example.com",
        published_at: null,
        relevance: 0.5,
        key_finding: "An unconfirmed rumour about a takeover circulated.",
        significance: "Unclear impact.",
        impact: "low",
        sentiment: "neutral",
        reason: "Relevant.",
        confidence: 0.3,
        source_quality: 0.4,
        evidence_excerpt: "",
      },
    ];
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findByText("AstraZeneca board approval");

    // No fabricated dates: missing dates are stated as such.
    expect(screen.getAllByText("date not stated").length).toBeGreaterThanOrEqual(2);
    // Reported vs unverified claims are distinguished.
    expect(screen.getByText("unverified")).toBeInTheDocument();
    expect(screen.getAllByText("reported").length).toBeGreaterThanOrEqual(1);
  });

  it("preserves genuinely different events from the same company", async () => {
    const rep = answerReport();
    rep.answer!.key_points = [];
    rep.findings = [
      {
        title: "AstraZeneca completes deal",
        url: "https://www.reuters.com/markets/azn-deal",
        source: "reuters.com",
        published_at: "2026-10-05T00:00:00+00:00",
        relevance: 0.9,
        key_finding: "AstraZeneca completed its $2bn equity investment.",
        significance: "Expands oncology.",
        impact: "high",
        sentiment: "positive",
        reason: "Material.",
        confidence: 0.8,
        source_quality: 0.8,
        evidence_excerpt: "",
      },
      {
        title: "AstraZeneca lowers guidance",
        url: "https://www.reuters.com/markets/azn-guidance",
        source: "reuters.com",
        published_at: "2026-10-06T00:00:00+00:00",
        relevance: 0.85,
        key_finding: "AstraZeneca lowered its full-year guidance.",
        significance: "Cost pressures weighed on margins.",
        impact: "high",
        sentiment: "negative",
        reason: "Material.",
        confidence: 0.8,
        source_quality: 0.8,
        evidence_excerpt: "",
      },
    ];
    vi.mocked(api.report).mockResolvedValue(rep);
    renderPage();
    await screen.findByText("AstraZeneca completes deal");
    expect(
      screen.getByRole("link", { name: "AstraZeneca lowers guidance" })
    ).toBeInTheDocument();
  });

  it("omits confidence, verified and research metadata/details, with downloads at the bottom", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    await screen.findByText(paragraphWith("Georges Elhedery is the Group Chief Executive of HSBC Holdings plc."));

    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/query research/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Research metadata/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Research details/i)).not.toBeInTheDocument();

    // JSON + Markdown downloads are present at the bottom of the report.
    expect(
      screen.getByRole("link", { name: /Download JSON/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Download Markdown/i })
    ).toBeInTheDocument();
  });

  it("falls back to findings when no answer block exists", async () => {
    const legacy = answerReport();
    legacy.answer = null;
    legacy.findings = [
      {
        title: "HSBC news",
        url: "https://example.com/x",
        source: "example.com",
        published_at: null,
        relevance: 0.8,
        key_finding: "Something happened.",
        significance: "Matters.",
        impact: "medium",
        sentiment: "neutral",
        reason: "Relevant.",
        confidence: 0.7,
        source_quality: 0.5,
        evidence_excerpt: "",
      },
    ];
    vi.mocked(api.report).mockResolvedValue(legacy);
    renderPage();
    expect(await screen.findByText("Findings")).toBeInTheDocument();
    expect(screen.queryByText("Executive Summary")).not.toBeInTheDocument();
  });
});
