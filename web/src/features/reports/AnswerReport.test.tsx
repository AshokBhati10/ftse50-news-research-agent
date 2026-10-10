/** Answer-first report rendering: the direct answer leads, evidence supports it. */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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

describe("Answer-first report", () => {
  it("renders the direct answer prominently before sources", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    const answer = await screen.findByText(
      "Georges Elhedery is the Group Chief Executive of HSBC Holdings plc."
    );
    expect(answer).toBeInTheDocument();
    expect(screen.getByText("Answer")).toBeInTheDocument();
    expect(screen.getByText("verified")).toBeInTheDocument();

    // Answer appears before the Sources section in the document.
    const answerHeading = screen.getByText("Answer");
    const sourcesHeading = screen.getByText("Sources (1)");
    expect(
      answerHeading.compareDocumentPosition(sourcesHeading) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it("shows key points, citations and limitations", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    await screen.findByText("Key points");
    expect(
      screen.getByText("HSBC appointed Georges Elhedery as Group Chief Executive.")
    ).toBeInTheDocument();
    const link = screen.getByRole("link", {
      name: /HSBC confirms Georges Elhedery as Group Chief Executive/,
    });
    expect(link).toHaveAttribute(
      "href",
      "https://www.hsbc.com/news/2024/elhedery-ceo"
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/95% confidence/)).toBeInTheDocument();
  });

  it("exposes the tool trace in the research details", async () => {
    vi.mocked(api.report).mockResolvedValue(answerReport());
    renderPage();
    await screen.findByText("Georges Elhedery is the Group Chief Executive of HSBC Holdings plc.");
    const details = screen.getByText(/Research details/);
    expect(details.tagName).toBe("SUMMARY");
    // The trace is inside a collapsed <details>, but present in the DOM.
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByText("Find HSBC's current CEO.")).toBeInTheDocument();
  });

  it("falls back to ranked findings when no answer block exists", async () => {
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
    expect(await screen.findByText("Ranked findings (1)")).toBeInTheDocument();
    expect(screen.queryByText("Answer")).not.toBeInTheDocument();
  });
});
