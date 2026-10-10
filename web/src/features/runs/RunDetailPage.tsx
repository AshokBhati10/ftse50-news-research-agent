/** Live detail view for a single research run (query or company mode). */
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Square } from "lucide-react";
import { api } from "../../lib/api";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  ErrorState,
  LoadingState,
} from "../../components/ui";
import { isTerminal, QUERY_STAGE_LABELS, useRunLive, RunProgressCard, RunStatusBadge } from "./RunBits";

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const { run, error, streaming } = useRunLive(runId);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  if (error) return <ErrorState message={error} />;
  if (!run) return <LoadingState message="Loading run…" />;

  const active = !isTerminal(run.status);
  const isQuery = run.mode === "query";
  const req = run.request as {
    query?: string | null;
    max_results?: number | null;
    time_range?: string | null;
    fresh_search?: boolean;
    tickers?: string[] | null;
    limit_companies?: number | null;
    agentic?: boolean;
    no_cache?: boolean;
  };
  const warnings = Array.isArray(run.stats?.warnings)
    ? (run.stats.warnings as string[])
    : [];

  const cancel = async () => {
    setCancelling(true);
    setCancelError(null);
    try {
      await api.cancelRun(run.run_id);
    } catch (e) {
      setCancelError(e instanceof Error ? e.message : "Could not cancel the run.");
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="space-y-6">
      <Link
        to="/runs"
        className="inline-flex items-center gap-1.5 text-sm text-terminal-muted hover:text-white"
      >
        <ArrowLeft className="h-4 w-4" /> Back to runs
      </Link>

      {cancelError && <Alert tone="down">{cancelError}</Alert>}

      <RunProgressCard run={run} />

      <Card>
        <CardHeader title="Run configuration" />
        <dl className="grid gap-3 px-5 py-4 text-sm sm:grid-cols-2">
          {isQuery ? (
            <>
              <div className="sm:col-span-2">
                <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">Query</dt>
                <dd className="mt-0.5 text-terminal-text">{run.query}</dd>
              </div>
              <ConfigRow label="Max results" value={String(req.max_results ?? "—")} />
              <ConfigRow label="Time range" value={req.time_range ?? "—"} />
              <ConfigRow label="Fresh search" value={req.fresh_search ? "Enabled" : "Disabled"} />
              <ConfigRow
                label="Stage"
                value={run.stage ? (QUERY_STAGE_LABELS[run.stage] ?? run.stage) : "—"}
              />
            </>
          ) : (
            <>
              <div>
                <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">Tickers</dt>
                <dd className="mt-0.5 font-mono text-xs text-terminal-text">
                  {req.tickers && req.tickers.length > 0 ? req.tickers.join(", ") : "Full FTSE 50 universe"}
                </dd>
              </div>
              <ConfigRow label="Company limit" value={String(req.limit_companies ?? "—")} />
              <ConfigRow label="Agentic mode" value={req.agentic ? "Enabled" : "Disabled"} />
              <ConfigRow label="Cache" value={req.no_cache ? "Disabled (fresh)" : "Enabled"} />
            </>
          )}
        </dl>
      </Card>

      {warnings.length > 0 && (
        <Card>
          <CardHeader title="Warnings" subtitle="Limitations recorded during this run" />
          <ul className="list-disc space-y-1 px-5 py-4 pl-10 text-sm text-amber-200/90">
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Card>
      )}

      <div className="flex items-center gap-3">
        <RunStatusBadge status={run.status} />
        {isQuery && <Badge tone="info">query research</Badge>}
        {streaming && active && (
          <span className="text-xs text-terminal-faint">Receiving live updates…</span>
        )}
        {!streaming && active && (
          <span className="text-xs text-terminal-faint">Polling for updates…</span>
        )}
        {active && (
          <Button variant="danger" onClick={cancel} disabled={cancelling} className="ml-auto">
            <Square className="h-4 w-4" />
            {cancelling ? "Cancelling…" : "Cancel run"}
          </Button>
        )}
      </div>
    </div>
  );
}

function ConfigRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">{label}</dt>
      <dd className="mt-0.5 text-terminal-text">{value}</dd>
    </div>
  );
}
