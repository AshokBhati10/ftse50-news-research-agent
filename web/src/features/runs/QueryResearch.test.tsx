/** Query-driven research UI tests: input, validation, payload, progress. */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryResearchForm } from "./RunsPage";
import { QueryRunProgressCard } from "./RunBits";
import { api } from "../../lib/api";
import type { ResearchRun } from "../../types";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      startRun: vi.fn(),
    },
  };
});

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

function renderForm() {
  return render(
    <MemoryRouter>
      <QueryResearchForm />
    </MemoryRouter>
  );
}

describe("QueryResearchForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders query input, examples, and fresh-search toggle (on by default)", () => {
    renderForm();
    expect(
      screen.getByPlaceholderText("What would you like to research?")
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Latest developments in semiconductor stocks" })
    ).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Fresh search/i })).toHaveAttribute(
      "aria-checked",
      "true"
    );
  });

  it("clicking an example populates the query field without submitting", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.click(
      screen.getByRole("button", { name: "Impact of interest rates on UK banks" })
    );
    const box = screen.getByPlaceholderText(
      "What would you like to research?"
    ) as HTMLTextAreaElement;
    expect(box.value).toBe("Impact of interest rates on UK banks");
    expect(vi.mocked(api.startRun)).not.toHaveBeenCalled();
  });

  it("shows a validation message for an empty query", async () => {
    const user = userEvent.setup();
    renderForm();
    // Button is disabled while empty; force the error path via Ctrl+Enter.
    const box = screen.getByPlaceholderText("What would you like to research?");
    await user.click(box);
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(await screen.findByText("Please enter a research question to start.")).toBeInTheDocument();
    expect(vi.mocked(api.startRun)).not.toHaveBeenCalled();
  });

  it("sends the query payload with fresh search and options", async () => {
    const user = userEvent.setup();
    vi.mocked(api.startRun).mockResolvedValue({ run_id: "run_x" } as never);
    renderForm();
    await user.type(
      screen.getByPlaceholderText("What would you like to research?"),
      "semiconductor stocks"
    );
    // Open advanced settings and change max results.
    await user.click(screen.getByRole("button", { name: /Advanced settings/i }));
    const maxInput = screen.getByLabelText(/Maximum results/i);
    await user.clear(maxInput);
    await user.type(maxInput, "10");
    await user.click(screen.getByRole("button", { name: /Start research/i }));
    await waitFor(() =>
      expect(vi.mocked(api.startRun)).toHaveBeenCalledWith({
        query: "semiconductor stocks",
        max_results: 10,
        time_range: "month",
        fresh_search: true,
      })
    );
    expect(mockNavigate).toHaveBeenCalledWith("/runs/run_x");
  });

  it("trims the query before sending", async () => {
    const user = userEvent.setup();
    vi.mocked(api.startRun).mockResolvedValue({ run_id: "run_y" } as never);
    renderForm();
    await user.type(
      screen.getByPlaceholderText("What would you like to research?"),
      "   uk banks   "
    );
    await user.click(screen.getByRole("button", { name: /Start research/i }));
    await waitFor(() =>
      expect(vi.mocked(api.startRun)).toHaveBeenCalledWith(
        expect.objectContaining({ query: "uk banks" })
      )
    );
  });

  it("fresh-search toggle flips the visible indicator and payload", async () => {
    const user = userEvent.setup();
    vi.mocked(api.startRun).mockResolvedValue({ run_id: "run_z" } as never);
    renderForm();
    const toggle = screen.getByRole("switch", { name: /Fresh search/i });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/Fresh search: off/i)).toBeInTheDocument();
    await user.type(
      screen.getByPlaceholderText("What would you like to research?"),
      "fintech"
    );
    await user.click(screen.getByRole("button", { name: /Start research/i }));
    await waitFor(() =>
      expect(vi.mocked(api.startRun)).toHaveBeenCalledWith(
        expect.objectContaining({ fresh_search: false })
      )
    );
  });

  it("prevents duplicate submissions while starting", async () => {
    const user = userEvent.setup();
    let resolveStart: (v: never) => void = () => undefined;
    vi.mocked(api.startRun).mockImplementation(
      () => new Promise((res) => (resolveStart = res as never))
    );
    renderForm();
    await user.type(
      screen.getByPlaceholderText("What would you like to research?"),
      "query one"
    );
    const btn = screen.getByRole("button", { name: /Start research/i });
    await user.click(btn);
    await user.click(btn); // second click while in flight
    expect(vi.mocked(api.startRun)).toHaveBeenCalledTimes(1);
    resolveStart({ run_id: "run_dup" } as never);
  });

  it("shows a clear message when the API fails", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../../lib/api");
    vi.mocked(api.startRun).mockRejectedValue(
      new ApiError(400, "LLM is not configured")
    );
    renderForm();
    await user.type(
      screen.getByPlaceholderText("What would you like to research?"),
      "banks"
    );
    await user.click(screen.getByRole("button", { name: /Start research/i }));
    expect(await screen.findByText("LLM is not configured")).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("clear resets the form", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.type(
      screen.getByPlaceholderText("What would you like to research?"),
      "something"
    );
    await user.click(screen.getByRole("button", { name: /Clear/i }));
    const box = screen.getByPlaceholderText(
      "What would you like to research?"
    ) as HTMLTextAreaElement;
    expect(box.value).toBe("");
    expect(screen.getByRole("switch", { name: /Fresh search/i })).toHaveAttribute(
      "aria-checked",
      "true"
    );
  });
});

function queryRun(overrides: Partial<ResearchRun> = {}): ResearchRun {
  return {
    run_id: "run_q1",
    status: "running",
    mode: "query",
    query: "semiconductor stocks",
    stage: "extract",
    stats: {
      search: { collected: 12 },
      extract: { extracted: 8 },
    },
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
    request: { query: "semiconductor stocks", fresh_search: true },
    ...overrides,
  };
}

describe("QueryRunProgressCard", () => {
  it("renders honest stage progress with real counts", () => {
    render(
      <MemoryRouter>
        <QueryRunProgressCard run={queryRun()} />
      </MemoryRouter>
    );
    expect(screen.getByText("Query research")).toBeInTheDocument();
    expect(screen.getByText("semiconductor stocks")).toBeInTheDocument();
    expect(screen.getByText("Searching the web")).toBeInTheDocument();
    expect(screen.getByText("Extracting articles")).toBeInTheDocument();
    expect(screen.getByText(/12 collected/)).toBeInTheDocument();
    expect(screen.getByText(/8 extracted/)).toBeInTheDocument();
  });

  it("shows the completed report link and stage", () => {
    render(
      <MemoryRouter>
        <QueryRunProgressCard
          run={queryRun({
            status: "completed",
            stage: "done",
            articles_found: 7,
            finished_at: new Date().toISOString(),
            report_id: "report_2026-10-10",
          })}
        />
      </MemoryRouter>
    );
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("report_2026-10-10")).toBeInTheDocument();
  });
});
