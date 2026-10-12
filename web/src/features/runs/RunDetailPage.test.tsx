/** Single run page: in-progress indicator, auto-displayed answer, failures. */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import RunDetailPage from "./RunDetailPage";
import type { ReportDetail, ResearchRun } from "../../types";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      run: vi.fn(),
      report: vi.fn(),
      cancelRun: vi.fn(),
      subscribeToRun: vi.fn(() => () => undefined),
    },
  };
});

import { api } from "../../lib/api";

function run(overrides: Partial<ResearchRun> = {}): ResearchRun {
  return {
    run_id: "run_q1",
    status: "running",
    mode: "query",
    query: "Latest semiconductor developments",
    stage: "tool",
    stats: {},
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    finished_at: null,
    total_companies: 0,
    completed: 0,
    failed: 0,
    current_company: null,
    articles_found: 0,
    error: null,
    report_id: null,
    companies: [],
    request: { query: "Latest semiconductor developments" },
    ...overrides,
  };
}

function queryReport(): ReportDetail {
  return {
    summary: {
      id: "report_2026-10-10",
      date: "2026-10-10",
      report_type: "query",
      title: "Latest semiconductor developments",
      generated_at: new Date().toISOString(),
      company_count: 0,
      article_count: 0,
      companies: [],
    },
    companies: [],
    findings: [
      {
        title: "Chip demand headline",
        url: "https://example.com/chips-headline",
        source: "example.com",
        published_at: "2026-10-08T00:00:00+00:00",
        relevance: 0.9,
        key_finding: "Demand rose sharply across the quarter.",
        significance: "Improves pricing power.",
        impact: "high",
        sentiment: "positive",
        reason: "Material.",
        confidence: 0.8,
        source_quality: 0.8,
        evidence_excerpt: "Excerpt.",
      },
    ],
    answer: {
      query: "Latest semiconductor developments",
      task_type: "news_research",
      answer: "Semiconductor demand rose sharply this quarter.",
      key_points: ["Demand rose sharply."],
      sources: [
        {
          title: "Chip demand up",
          url: "https://example.com/chips",
          source_name: "example.com",
          published_at: "2026-10-08T00:00:00+00:00",
          supporting_excerpt: "Demand rose sharply.",
          supports_claims: [0],
        },
      ],
      confidence: 0.7,
      limitations: [],
      verified: true,
    },
    query: "Latest semiconductor developments",
    metadata: { run_id: "run_q1" },
    has_markdown: true,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/runs/run_q1"]}>
      <Routes>
        <Route path="/runs/:runId" element={<RunDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("RunDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a clear in-progress indicator while the run is active", async () => {
    vi.mocked(api.run).mockResolvedValue(run({ status: "running" }));
    renderPage();
    expect(await screen.findByText("Research in progress")).toBeInTheDocument();
    expect(vi.mocked(api.report)).not.toHaveBeenCalled();
  });

  it("automatically displays the completed answer on the same page", async () => {
    vi.mocked(api.run).mockResolvedValue(
      run({ status: "completed", stage: "report", report_id: "report_2026-10-10" })
    );
    vi.mocked(api.report).mockResolvedValue(queryReport());
    renderPage();
    expect(
      await screen.findByText("Semiconductor demand rose sharply this quarter.")
    ).toBeInTheDocument();
    expect(screen.queryByText("Research in progress")).not.toBeInTheDocument();
    // The query heading appears once (run header only — not duplicated by the
    // embedded report view).
    expect(
      screen.getAllByText("Latest semiconductor developments")
    ).toHaveLength(1);
    // Embedded report is consistent with the standalone report page.
    expect(
      screen.getByRole("heading", { name: "Key Findings" })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Findings" })
    ).toBeInTheDocument();
    const findingLink = screen.getByRole("link", {
      name: "Chip demand headline",
    });
    expect(findingLink).toHaveAttribute("target", "_blank");
    expect(findingLink).toHaveAttribute("rel", "noopener noreferrer");
    expect(
      screen.getByRole("link", { name: /Download JSON/i })
    ).toBeInTheDocument();
    expect(vi.mocked(api.report)).toHaveBeenCalledWith("report_2026-10-10");
  });

  it("clearly reports a failed run", async () => {
    vi.mocked(api.run).mockResolvedValue(
      run({ status: "failed", error: "LLM authentication failed" })
    );
    renderPage();
    expect(await screen.findByText("LLM authentication failed")).toBeInTheDocument();
  });

  it("notes a completed run that produced no report", async () => {
    vi.mocked(api.run).mockResolvedValue(
      run({ status: "completed", report_id: null })
    );
    renderPage();
    expect(
      await screen.findByText(/completed but produced no report/i)
    ).toBeInTheDocument();
  });
});
