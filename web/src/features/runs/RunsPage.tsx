/**
 * Research Runs page: configuration form (company selection + options),
 * active-run banner, and run history.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { FlaskConical, Play, RotateCcw, Search, Square } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { Company, ResearchRun } from "../../types";
import { formatDateTime } from "../../lib/format";
import {
  Alert,
  Button,
  Card,
  CardHeader,
  Checkbox,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  TextInput,
} from "../../components/ui";
import { isTerminal, RunProgressCard, RunStatusBadge, useRunLive } from "./RunBits";

const CONFIRM_THRESHOLD = 20;

export default function RunsPage() {
  const navigate = useNavigate();
  const [companies, setCompanies] = useState<Company[] | null>(null);
  const [companiesError, setCompaniesError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState("");
  const [agentic, setAgentic] = useState(false);
  const [noCache, setNoCache] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [history, setHistory] = useState<ResearchRun[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const loadCompanies = async () => {
    setCompaniesError(null);
    try {
      const res = await api.companies();
      setCompanies(res.companies);
    } catch (e) {
      setCompaniesError(e instanceof Error ? e.message : "Failed to load companies.");
    }
  };

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
    loadCompanies();
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    if (!companies) return [];
    const q = query.trim().toLowerCase();
    if (!q) return companies;
    return companies.filter(
      (c) => c.name.toLowerCase().includes(q) || c.ticker.toLowerCase().includes(q)
    );
  }, [companies, query]);

  const selectedCount = selected.size;
  const effectiveCount = selectedCount > 0 ? selectedCount : (companies?.length ?? 0);
  const needsConfirm = effectiveCount >= CONFIRM_THRESHOLD;

  const toggle = (ticker: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(ticker)) next.delete(ticker);
      else next.add(ticker);
      return next;
    });
  };

  const selectAll = () => {
    if (!companies) return;
    setSelected(new Set(filtered.map((c) => c.ticker)));
  };

  const validate = (): string | null => {
    if (limit.trim() !== "") {
      const n = Number(limit);
      if (!Number.isInteger(n) || n < 1 || n > 60)
        return "Company limit must be a whole number between 1 and 60.";
    }
    return null;
  };

  const doStart = async () => {
    setConfirmOpen(false);
    const problem = validate();
    if (problem) {
      setFormError(problem);
      return;
    }
    setSubmitting(true);
    setFormError(null);
    try {
      const run = await api.startRun({
        tickers: selectedCount > 0 ? [...selected] : undefined,
        limit_companies: limit.trim() === "" ? undefined : Number(limit),
        agentic,
        no_cache: noCache,
      });
      navigate(`/runs/${run.run_id}`);
    } catch (e) {
      setFormError(
        e instanceof ApiError
          ? e.message
          : "Failed to start the research run."
      );
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const problem = validate();
    if (problem) {
      setFormError(problem);
      return;
    }
    if (needsConfirm) setConfirmOpen(true);
    else void doStart();
  };

  const resetForm = () => {
    setSelected(new Set());
    setQuery("");
    setLimit("");
    setAgentic(false);
    setNoCache(false);
    setFormError(null);
  };

  return (
    <div className="space-y-6">
      {activeRunId && <ActiveRunBanner runId={activeRunId} onDone={loadHistory} />}

      {/* Configuration form */}
      <Card>
        <CardHeader
          title="New research run"
          subtitle="Launches the real Python pipeline through the API"
        />
        <form onSubmit={onSubmit} className="space-y-5 px-5 py-5">
          {formError && <Alert tone="down">{formError}</Alert>}

          <div>
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-terminal-muted">
                Companies{" "}
                <span className="text-indigo-300">
                  · {selectedCount > 0 ? `${selectedCount} selected` : "all selected"}
                </span>
              </span>
              <div className="flex gap-2">
                <Button type="button" variant="ghost" className="px-2 py-1 text-xs" onClick={selectAll}>
                  Select filtered
                </Button>
                <Button type="button" variant="ghost" className="px-2 py-1 text-xs" onClick={() => setSelected(new Set())}>
                  Clear
                </Button>
              </div>
            </div>
            <div className="relative mb-2">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-terminal-faint" />
              <TextInput
                placeholder="Filter by company name or ticker…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="pl-9"
              />
            </div>
            {companies === null && !companiesError && (
              <div className="flex items-center gap-2 rounded-lg border border-terminal-border px-3 py-6 text-sm text-terminal-muted">
                <LoadingState message="Loading company universe…" />
              </div>
            )}
            {companiesError && (
              <ErrorState message={companiesError} onRetry={loadCompanies} />
            )}
            {companies && (
              <div className="max-h-64 overflow-y-auto rounded-lg border border-terminal-border">
                {filtered.length === 0 && (
                  <p className="px-3 py-6 text-center text-sm text-terminal-faint">
                    No companies match “{query}”.
                  </p>
                )}
                {filtered.map((c) => (
                  <label
                    key={c.ticker}
                    className="flex cursor-pointer items-center gap-3 border-b border-terminal-border/50 px-3 py-2 text-sm last:border-0 hover:bg-white/[0.02]"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(c.ticker)}
                      onChange={() => toggle(c.ticker)}
                      className="h-4 w-4 rounded border-terminal-border bg-terminal-surface accent-indigo-500"
                    />
                    <span className="flex-1 truncate text-terminal-text">{c.name}</span>
                    <span className="font-mono text-xs text-terminal-faint">{c.ticker}</span>
                  </label>
                ))}
              </div>
            )}
            <p className="mt-1.5 text-xs text-terminal-faint">
              Leave all unselected to research the full FTSE 50 universe.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Company limit" hint="Optional cap, applied after ticker filtering">
              <TextInput
                type="number"
                min={1}
                max={60}
                placeholder="50"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
            </Field>
            <div className="sm:col-span-2 grid gap-3 sm:grid-cols-2">
              <Checkbox
                label="Agentic mode"
                hint="Let the research agent plan tool calls per company"
                checked={agentic}
                onChange={(e) => setAgentic(e.target.checked)}
              />
              <Checkbox
                label="Disable cache"
                hint="Force fresh search, extraction and analysis"
                checked={noCache}
                onChange={(e) => setNoCache(e.target.checked)}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" variant="primary" disabled={submitting || companies === null}>
              <Play className="h-4 w-4" />
              {submitting ? "Starting…" : "Start research"}
            </Button>
            <Button type="button" variant="ghost" onClick={resetForm}>
              <RotateCcw className="h-4 w-4" /> Reset form
            </Button>
            {needsConfirm && (
              <span className="text-xs text-amber-300/90">
                {effectiveCount} companies — confirmation required before launch.
              </span>
            )}
          </div>
        </form>
      </Card>

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
                  <th className="px-3 py-3 font-medium">Created</th>
                  <th className="px-3 py-3 font-medium">Status</th>
                  <th className="px-3 py-3 font-medium">Companies</th>
                  <th className="px-3 py-3 font-medium">Articles</th>
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
                    <td className="whitespace-nowrap px-3 py-3 text-xs text-terminal-muted">
                      {formatDateTime(r.created_at)}
                    </td>
                    <td className="px-3 py-3"><RunStatusBadge status={r.status} /></td>
                    <td className="px-3 py-3 tabular-nums text-terminal-muted">
                      {r.completed + r.failed}/{r.total_companies || "—"}
                    </td>
                    <td className="px-3 py-3 tabular-nums text-terminal-muted">{r.articles_found}</td>
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

      {/* Launch confirmation */}
      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} title="Confirm research run">
        <p className="text-sm leading-relaxed text-terminal-text">
          This will research <strong className="text-white">{effectiveCount} companies</strong>{" "}
          through live search and the paid LLM endpoint
          {agentic ? " in agentic mode" : ""}. Depending on provider latency this can
          take a long time and consume API quota.
        </p>
        <div className="mt-5 flex justify-end gap-3">
          <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={doStart} disabled={submitting}>
            <Play className="h-4 w-4" /> Launch run
          </Button>
        </div>
      </Dialog>
    </div>
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
