/** Live detail view for a single research run. */
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Square } from "lucide-react";
import { api } from "../../lib/api";
import {
  Alert,
  Button,
  Card,
  CardHeader,
  ErrorState,
  LoadingState,
} from "../../components/ui";
import { isTerminal, useRunLive, RunProgressCard, RunStatusBadge } from "./RunBits";

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const { run, error, streaming } = useRunLive(runId);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  if (error) return <ErrorState message={error} />;
  if (!run) return <LoadingState message="Loading run…" />;

  const active = !isTerminal(run.status);
  const req = run.request as {
    tickers?: string[] | null;
    limit_companies?: number | null;
    agentic?: boolean;
    no_cache?: boolean;
  };

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
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">Tickers</dt>
            <dd className="mt-0.5 font-mono text-xs text-terminal-text">
              {req.tickers && req.tickers.length > 0 ? req.tickers.join(", ") : "Full FTSE 50 universe"}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">Company limit</dt>
            <dd className="mt-0.5 text-terminal-text">{req.limit_companies ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">Agentic mode</dt>
            <dd className="mt-0.5 text-terminal-text">{req.agentic ? "Enabled" : "Disabled"}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">Cache</dt>
            <dd className="mt-0.5 text-terminal-text">{req.no_cache ? "Disabled (fresh)" : "Enabled"}</dd>
          </div>
        </dl>
      </Card>

      <div className="flex items-center gap-3">
        <RunStatusBadge status={run.status} />
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
