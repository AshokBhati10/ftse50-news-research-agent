import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import DeepResearchPage from "./DeepResearchPage";
import { api, ApiError } from "../../lib/api";
import type { DeepResearchJob, DeepResearchResult } from "../../types";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { startDeepResearch: vi.fn(), deepResearchJob: vi.fn(), deepResearchHistory: vi.fn() } };
});

const JOB = "a".repeat(32);
const result: DeepResearchResult = {
  question: "q",
  answer_markdown: "## Overview\n\nNVIDIA raised its buyback by **$150B** [1].",
  citations: [{ number: 1, url: "https://a.com/x", title: "Full article", source: "a.com", date: "2026-09-28" }],
  sub_questions: ["What happened?"],
  rounds: 1,
  queries_run: ["nvidia buyback"],
  open_gaps: [],
  plan: [{ sub_question: "What happened?", category: "news", time_range: "month", queries: ["nvidia buyback"] }],
  sources: [
    { url: "https://a.com/x", title: "Full article", source: "a.com", engines: ["google"], published: "2026-09-28", extraction: "extracted" },
    { url: "https://b.com/y", title: "Teaser only", source: "b.com", engines: ["bing"], published: null, extraction: "snippet_only" },
  ],
  warnings: ["engine google unresponsive (CAPTCHA)"],
  coverage: { "What happened?": true },
};
const job = (over: Partial<DeepResearchJob>): DeepResearchJob => ({ job_id: JOB, status: "pending", ...over });

async function type(q: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: q } });
}
async function tick(ms = 5100) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  vi.mocked(api.startDeepResearch).mockReset();
  vi.mocked(api.deepResearchJob).mockReset();
  vi.mocked(api.deepResearchHistory).mockReset().mockResolvedValue([]);
});
afterEach(() => vi.useRealTimers());

describe("DeepResearchPage", () => {
  it("submits, shows pending then running, then renders the cited result", async () => {
    vi.mocked(api.startDeepResearch).mockResolvedValue(job({ status: "pending" }));
    vi.mocked(api.deepResearchJob)
      .mockResolvedValueOnce(job({ status: "pending" }))
      .mockResolvedValueOnce(job({ status: "running" }))
      .mockResolvedValue(job({ status: "completed", result }));
    render(<DeepResearchPage />);
    await type("What happened to NVIDIA?");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /start research/i })));
    expect(api.startDeepResearch).toHaveBeenCalledWith("What happened to NVIDIA?");
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/queued/i);
    await tick();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/Researching your question/);
    expect(screen.getByRole("status")).not.toHaveTextContent(/minutes/i);
    await tick();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByText("$150B").tagName).toBe("STRONG");
    // warnings, research plan and extraction badges are not shown; only cited sources are listed
    expect(screen.queryByText("engine google unresponsive (CAPTCHA)")).not.toBeInTheDocument();
    expect(screen.queryByText("Research plan")).not.toBeInTheDocument();
    expect(screen.queryByText("full text")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Full article" })).toHaveAttribute("href", "https://a.com/x");
    expect(screen.queryByText("Teaser only")).not.toBeInTheDocument();
    // polling stops after completion; the id is kept so a refresh can show the report again
    const calls = vi.mocked(api.deepResearchJob).mock.calls.length;
    await tick(20000);
    expect(vi.mocked(api.deepResearchJob).mock.calls.length).toBe(calls);
    expect(localStorage.getItem("deepResearchJobId")).toBe(JOB);
  });

  it("re-shows a finished report after a refresh and forgets expired jobs", async () => {
    localStorage.setItem("deepResearchJobId", JOB);
    vi.mocked(api.deepResearchJob).mockResolvedValueOnce(job({ status: "completed", result }));
    const first = render(<DeepResearchPage />);
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
    first.unmount();

    vi.mocked(api.deepResearchJob).mockRejectedValueOnce(new ApiError(404, "unknown job id"));
    render(<DeepResearchPage />);
    await act(async () => {});
    expect(screen.getByText("That job no longer exists.")).toBeInTheDocument();
    expect(localStorage.getItem("deepResearchJobId")).toBeNull();
  });

  it("shows the error of a failed job", async () => {
    vi.mocked(api.startDeepResearch).mockResolvedValue(job({}));
    vi.mocked(api.deepResearchJob).mockResolvedValue(job({ status: "failed", error: "RuntimeError: LLM down" }));
    render(<DeepResearchPage />);
    await type("What happened to NVIDIA?");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /start research/i })));
    await tick(100);
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("RuntimeError: LLM down")).toBeInTheDocument();
  });

  it("reports a submission error and does not poll", async () => {
    vi.mocked(api.startDeepResearch).mockRejectedValue(new ApiError(503, "job queue unavailable"));
    render(<DeepResearchPage />);
    await type("What happened to NVIDIA?");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /start research/i })));
    expect(screen.getByText("job queue unavailable")).toBeInTheDocument();
    expect(api.deepResearchJob).not.toHaveBeenCalled();
  });

  it("resumes polling a saved job after a refresh", async () => {
    localStorage.setItem("deepResearchJobId", JOB);
    vi.mocked(api.deepResearchJob).mockResolvedValue(job({ status: "running" }));
    await act(async () => {
      render(<DeepResearchPage />);
    });
    expect(api.deepResearchJob).toHaveBeenCalledWith(JOB);
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start research/i })).toBeDisabled();
  });

  it("keeps polling through a temporary network error", async () => {
    vi.mocked(api.startDeepResearch).mockResolvedValue(job({}));
    vi.mocked(api.deepResearchJob)
      .mockRejectedValueOnce(new ApiError(0, "Cannot reach the backend API."))
      .mockResolvedValue(job({ status: "completed", result }));
    render(<DeepResearchPage />);
    await type("What happened to NVIDIA?");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /start research/i })));
    expect(screen.getByText("Cannot reach the backend API.")).toBeInTheDocument();
    await tick();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.queryByText("Cannot reach the backend API.")).not.toBeInTheDocument();
  });

  const OTHER = "b".repeat(32);
  const rows = [
    { job_id: OTHER, status: "completed" as const, question: "Newest NVIDIA question", created_at: "2026-10-12T10:00:00Z" },
    { job_id: JOB, status: "completed" as const, question: "Older AMD question", created_at: "2026-10-12T09:00:00Z" },
  ];
  const olderResult = { ...result, answer_markdown: "## Older report\n\nAMD text." };

  it("shows previous research before any new job and opens the selected (older) report, not the latest", async () => {
    vi.mocked(api.deepResearchHistory).mockResolvedValue(rows);
    vi.mocked(api.deepResearchJob).mockImplementation(async (id) =>
      id === JOB ? job({ status: "completed", result: olderResult }) : job({ job_id: OTHER, status: "completed", result })
    );
    await act(async () => {
      render(<DeepResearchPage />);
    });
    expect(screen.getByText("Newest NVIDIA question")).toBeInTheDocument();
    expect(screen.getByText("Older AMD question")).toBeInTheDocument();
    expect(api.deepResearchJob).not.toHaveBeenCalled(); // nothing opened yet
    await act(async () => fireEvent.click(screen.getAllByRole("button", { name: "View" })[1]));
    expect(api.deepResearchJob).toHaveBeenCalledWith(JOB);
    expect(screen.getByRole("heading", { name: "Older report" })).toBeInTheDocument();
    expect(localStorage.getItem("deepResearchJobId")).toBe(JOB);
    expect(screen.getByRole("button", { name: "Showing" })).toBeDisabled();
  });

  it("re-shows the opened older report after a page refresh", async () => {
    vi.mocked(api.deepResearchHistory).mockResolvedValue(rows);
    vi.mocked(api.deepResearchJob).mockResolvedValue(job({ status: "completed", result: olderResult }));
    const first = render(<DeepResearchPage />);
    await act(async () => {});
    await act(async () => fireEvent.click(screen.getAllByRole("button", { name: "View" })[1]));
    first.unmount();
    render(<DeepResearchPage />);
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "Older report" })).toBeInTheDocument();
    expect(screen.getByText("Older AMD question")).toBeInTheDocument();
  });

  it("ignores a late reply for a report the user has switched away from", async () => {
    vi.mocked(api.deepResearchHistory).mockResolvedValue(rows);
    let release: (j: DeepResearchJob) => void = () => {};
    vi.mocked(api.deepResearchJob).mockImplementation((id) =>
      id === OTHER
        ? new Promise<DeepResearchJob>((r) => (release = r))
        : Promise.resolve(job({ status: "completed", result: olderResult }))
    );
    await act(async () => {
      render(<DeepResearchPage />);
    });
    await act(async () => fireEvent.click(screen.getAllByRole("button", { name: "View" })[0])); // slow
    await act(async () => fireEvent.click(screen.getAllByRole("button", { name: "View" })[1])); // older, fast
    await act(async () => release(job({ job_id: OTHER, status: "completed", result })));
    expect(screen.getByRole("heading", { name: "Older report" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Overview" })).not.toBeInTheDocument();
  });

  it("refreshes the history after a job is submitted and when it completes", async () => {
    vi.mocked(api.startDeepResearch).mockResolvedValue(job({}));
    vi.mocked(api.deepResearchJob).mockResolvedValue(job({ status: "completed", result }));
    render(<DeepResearchPage />);
    await act(async () => {});
    expect(api.deepResearchHistory).toHaveBeenCalledTimes(1);
    await type("What happened to NVIDIA?");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /start research/i })));
    await tick(100);
    expect(vi.mocked(api.deepResearchHistory).mock.calls.length).toBeGreaterThanOrEqual(3); // mount + submit + completed
  });

  it("shows an empty state only when there is no history", async () => {
    await act(async () => {
      render(<DeepResearchPage />);
    });
    expect(screen.getByText(/No previous research yet/)).toBeInTheDocument();
  });

  it("shows a visible error with retry when the history cannot be loaded", async () => {
    vi.mocked(api.deepResearchHistory).mockRejectedValueOnce(new ApiError(405, "Method Not Allowed"));
    await act(async () => {
      render(<DeepResearchPage />);
    });
    expect(screen.getByText(/Could not load previous research: Method Not Allowed/)).toBeInTheDocument();
    expect(screen.queryByText(/No previous research yet/)).not.toBeInTheDocument();
    vi.mocked(api.deepResearchHistory).mockResolvedValue(rows);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
    expect(screen.getByText("Newest NVIDIA question")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load previous research/)).not.toBeInTheDocument();
  });

  it("requires a question of at least 3 characters", async () => {
    render(<DeepResearchPage />);
    expect(screen.getByRole("button", { name: /start research/i })).toBeDisabled();
    await type("ab");
    expect(screen.getByRole("button", { name: /start research/i })).toBeDisabled();
  });
});
