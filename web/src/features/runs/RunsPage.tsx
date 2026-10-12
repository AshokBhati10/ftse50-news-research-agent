/**
 * Research Runs page — query-first research workspace with run history.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  FlaskConical,
  Play,
  RotateCcw,
  Sparkles,
  Square,
} from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { ResearchRun } from "../../types";
import { formatDateTime } from "../../lib/format";
import {
  Alert,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Textarea,
  Toggle,
} from "../../components/ui";
import { isTerminal, RunProgressCard, RunStatusBadge, useRunLive } from "./RunBits";
import { cn } from "../../lib/cn";

const MAX_QUERY_LENGTH = 500;

const TIME_RANGES = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "year", label: "Year" },
] as const;

const EXAMPLE_QUERIES = [
  "Latest developments in semiconductor stocks",
  "Impact of interest rates on UK banks",
  "Recent AI infrastructure investments",
  "Latest developments in global energy markets",
];

export default function RunsPage() {
  const [history, setHistory] = useState<ResearchRun[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const loadHistory = async () => {
    setHistoryError(null);
    try {
      const res = await api.runs(20);
      setHistory(res.runs);
      const active = res.runs.find((r) => !isTerminal(r.status));
      setActiveRunId(active ? active.run_id : null);
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : "Failed to load run history.");
    }
  };

  useEffect(() => {
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-6">
      {activeRunId && <ActiveRunBanner runId={activeRunId} onDone={loadHistory} />}

      <QueryResearchForm />

      {/* History */}
      <Card>
        <CardHeader title="Run history" subtitle="Newest first" />
        {history === null && !historyError && <LoadingState message="Loading runs…" />}
        {historyError && <ErrorState message={historyError} onRetry={loadHistory} />}
        {history && history.length === 0 && (
          <EmptyState
            title="No runs yet"
            message="Research runs you start will appear here with live progress and links to their reports."
            icon={<FlaskConical className="h-6 w-6" />}
          />
        )}
        {history && history.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="border-b border-terminal-border text-xs uppercase tracking-wide text-terminal-faint">
                  <th className="px-5 py-3 font-medium">Run</th>
                  <th className="px-3 py-3 font-medium">Query</th>
                  <th className="px-3 py-3 font-medium">Created</th>
                  <th className="px-3 py-3 font-medium">Status</th>
                  <th className="px-3 py-3 font-medium">Results</th>
                  <th className="px-3 py-3 font-medium">Report</th>
                </tr>
              </thead>
              <tbody>
                {history.map((r) => (
                  <tr key={r.run_id} className="border-b border-terminal-border/50 last:border-0 hover:bg-white/[0.02]">
                    <td className="px-5 py-3">
                      <Link to={`/runs/${r.run_id}`} className="font-mono text-xs text-indigo-300 hover:underline">
                        {r.run_id}
                      </Link>
                    </td>
                    <td className="max-w-[260px] truncate px-3 py-3 text-xs text-terminal-muted" title={r.query ?? undefined}>
                      {r.query ?? (r.mode === "companies" ? "FTSE 50 (legacy)" : "—")}
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-xs text-terminal-muted">
                      {formatDateTime(r.created_at)}
                    </td>
                    <td className="px-3 py-3"><RunStatusBadge status={r.status} /></td>
                    <td className="px-3 py-3 tabular-nums text-terminal-muted">
                      {r.mode === "query"
                        ? `${r.articles_found} findings`
                        : `${r.completed + r.failed}/${r.total_companies || "—"} companies`}
                    </td>
                    <td className="px-3 py-3">
                      {r.report_id ? (
                        <Link to={`/reports/${r.report_id}`} className="font-mono text-xs text-indigo-300 hover:underline">
                          {r.report_id}
                        </Link>
                      ) : (
                        <span className="text-xs text-terminal-faint">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------------------
 * Query-driven research form
 * ------------------------------------------------------------------------- */

export function QueryResearchForm() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [freshSearch, setFreshSearch] = useState(true);
  const [timeRange, setTimeRange] = useState("month");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const queryError = useMemo(() => {
    if (query.length > MAX_QUERY_LENGTH)
      return `Query is too long (${query.length}/${MAX_QUERY_LENGTH} characters).`;
    return null;
  }, [query]);

  const start = async () => {
    const q = query.trim();
    if (!q) {
      setError("Please enter a research question to start.");
      return;
    }
    if (queryError) {
      setError(queryError);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const run = await api.startRun({
        query: q,
        time_range: timeRange,
        fresh_search: freshSearch,
      });
      navigate(`/runs/${run.run_id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to start the research run.");
    } finally {
      setSubmitting(false);
    }
  };

  const clear = () => {
    setQuery("");
    setFreshSearch(true);
    setTimeRange("month");
    setError(null);
  };

  return (
    <Card>
      <CardHeader
        title="New research query"
        subtitle="Ask anything — the agent searches the live web, extracts, analyses and ranks"
      />
      <div className="space-y-5 px-5 py-5">
        {error && <Alert tone="down">{error}</Alert>}

        <Field label="Research question">
          <Textarea
            placeholder="What would you like to research?"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            maxLength={MAX_QUERY_LENGTH + 50}
            aria-label="Research question"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void start();
            }}
          />
          <div className="mt-1 flex items-center justify-between text-xs text-terminal-faint">
            <span>Tip: Ctrl+Enter starts the research.</span>
            <span className={cn(query.length > MAX_QUERY_LENGTH && "text-red-300")}>
              {query.length}/{MAX_QUERY_LENGTH}
            </span>
          </div>
        </Field>

        <Field label="Time range" hint="How far back the agent searches for this research">
          <div role="group" aria-label="Time range" className="flex flex-wrap gap-2">
            {TIME_RANGES.map((r) => (
              <button
                key={r.value}
                type="button"
                aria-pressed={timeRange === r.value}
                onClick={() => setTimeRange(r.value)}
                className={cn(
                  "rounded-md border px-4 py-2 text-sm font-medium transition-colors",
                  timeRange === r.value
                    ? "border-indigo-400/60 bg-indigo-500/15 text-white"
                    : "border-terminal-border bg-terminal-bg text-terminal-muted hover:border-indigo-400/60 hover:text-white"
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
        </Field>

        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-terminal-muted">
            <Sparkles className="mr-1 inline h-3.5 w-3.5" />
            Try an example
          </p>
          <div className="flex flex-wrap gap-2">
            {EXAMPLE_QUERIES.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => setQuery(ex)}
                className="rounded-full border border-terminal-border bg-terminal-bg px-3 py-1.5 text-xs text-terminal-muted transition-colors hover:border-indigo-400/60 hover:text-white"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Toggle
            label={`Fresh search: ${freshSearch ? "on" : "off"}`}
            hint={freshSearch ? "Bypasses cached search results" : "May reuse cached search results"}
            checked={freshSearch}
            onChange={setFreshSearch}
          />
          <div className="flex items-center rounded-md border border-terminal-border bg-terminal-bg px-3 py-2.5 text-xs text-terminal-faint">
            Results stream live — search, extract, analyse, report.
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary" onClick={start} disabled={submitting || query.trim() === ""}>
            <Play className="h-4 w-4" />
            {submitting ? "Starting…" : "Start research"}
          </Button>
          <Button variant="ghost" onClick={clear} disabled={submitting}>
            <RotateCcw className="h-4 w-4" /> Clear
          </Button>
        </div>
      </div>
    </Card>
  );
}

function ActiveRunBanner({ runId, onDone }: { runId: string; onDone: () => void }) {
  const { run } = useRunLive(runId);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    if (run && isTerminal(run.status)) onDone();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status]);

  if (!run) return null;

  const cancel = async () => {
    setCancelling(true);
    try {
      await api.cancelRun(runId);
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="space-y-4">
      <Alert tone="info">
        <span>
          Research run <span className="font-mono">{runId}</span> is {run.status} —{" "}
          <Link to={`/runs/${runId}`} className="underline">
            follow live progress
          </Link>
        </span>
      </Alert>
      <RunProgressCard run={run} />
      {!isTerminal(run.status) && (
        <div>
          <Button variant="danger" onClick={cancel} disabled={cancelling}>
            <Square className="h-4 w-4" />
            {cancelling ? "Cancelling…" : "Cancel run"}
          </Button>
        </div>
      )}
    </div>
  );
}
