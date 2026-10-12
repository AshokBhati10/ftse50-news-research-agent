/**
 * Single research-run page: shows a clear in-progress indicator while the
 * agent works, then automatically displays the completed answer inline —
 * no separate report page required. Failures and partial results are shown
 * explicitly.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Square } from "lucide-react";
import { api } from "../../lib/api";
import type { ReportDetail, ResearchRun } from "../../types";
import {
  Alert,
  Button,
  Card,
  ErrorState,
  LoadingState,
  Spinner,
} from "../../components/ui";
import {
  isTerminal,
  QUERY_STAGE_LABELS,
  RunProgressCard,
  RunStatusBadge,
  useRunLive,
} from "./RunBits";
import QueryReportView from "../reports/QueryReportView";

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const { run, error, streaming } = useRunLive(runId);
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const terminal = run ? isTerminal(run.status) : false;
  const isQuery = run?.mode === "query";
  const reportId = run?.report_id ?? null;

  const loadReport = useCallback(async () => {
    if (!reportId) return;
    setReportError(null);
    try {
      setReport(await api.report(reportId));
    } catch (e) {
      setReportError(
        e instanceof Error ? e.message : "Failed to load the completed report."
      );
    }
  }, [reportId]);

  useEffect(() => {
    // Once the run reaches a terminal state, load its report automatically.
    if (!terminal || !isQuery || !reportId) return;
    loadReport();
  }, [terminal, isQuery, reportId, loadReport]);

  const cancel = async () => {
    if (!run) return;
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

  if (error) return <ErrorState message={error} />;
  if (!run) return <LoadingState message="Loading run…" />;

  const active = !terminal;
  const warnings = Array.isArray(run.stats?.warnings)
    ? (run.stats.warnings as string[])
    : [];

  return (
    <div className="space-y-6">
      <Link
        to="/runs"
        className="inline-flex items-center gap-1.5 text-sm text-terminal-muted hover:text-white"
      >
        <ArrowLeft className="h-4 w-4" /> Back to runs
      </Link>

      {cancelError && <Alert tone="down">{cancelError}</Alert>}

      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-xl font-semibold text-white">
            {run.query ?? run.run_id}
          </h2>
          <p className="mt-1 font-mono text-xs text-terminal-faint">{run.run_id}</p>
        </div>
        <div className="ml-auto">
          <RunStatusBadge status={run.status} />
        </div>
      </div>

      {active && (
        <ResearchInProgress
          run={run}
          streaming={streaming}
          cancelling={cancelling}
          onCancel={cancel}
        />
      )}

      {!active && run.status !== "completed" && (
        <Alert tone={run.status === "failed" ? "down" : "mixed"}>
          {statusMessage(run.error, run.status, Boolean(reportId))}
        </Alert>
      )}

      {!active && warnings.length > 0 && (
        <Alert>
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}

      {/* Completed (or partial) results appear on this same page. */}
      {!active && isQuery && reportId && (
        reportError ? (
          <ErrorState message={reportError} onRetry={loadReport} />
        ) : report ? (
          <QueryReportView report={report} embedded />
        ) : (
          <LoadingState message="Loading your results…" />
        )
      )}

      {!active && run.status === "completed" && !reportId && (
        <Alert tone="mixed">
          The run completed but produced no report. Try a broader query or
          disable fresh search to reuse cached results.
        </Alert>
      )}

      {/* Legacy company runs: progress card (when idle) + report link. */}
      {!isQuery && (
        <>
          {!active && <RunProgressCard run={run} />}
          {run.report_id && (
            <Link
              to={`/reports/${run.report_id}`}
              className="inline-block text-sm text-indigo-300 hover:underline"
            >
              Open report {run.report_id}
            </Link>
          )}
        </>
      )}
    </div>
  );
}

function ResearchInProgress({
  run,
  streaming,
  cancelling,
  onCancel,
}: {
  run: ResearchRun;
  streaming: boolean;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const stageLabel = run.stage
    ? (QUERY_STAGE_LABELS[run.stage] ?? run.stage)
    : "Getting started";
  return (
    <div className="space-y-6">
      <Card className="border-indigo-500/30">
        <div className="flex items-center gap-4 px-5 py-5">
          <Spinner className="h-6 w-6 shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-white">Research in progress</p>
            <p className="mt-0.5 text-xs text-terminal-muted">
              {run.mode === "query"
                ? `${stageLabel}…`
                : `Researching ${run.total_companies} companies (${run.completed + run.failed} done)`}
            </p>
            <p className="mt-0.5 text-[11px] text-terminal-faint">
              {streaming ? "Receiving live updates" : "Checking for updates"} — the
              finished answer will appear here automatically.
            </p>
          </div>
          <Button variant="danger" onClick={onCancel} disabled={cancelling} className="shrink-0">
            <Square className="h-4 w-4" />
            {cancelling ? "Cancelling…" : "Cancel"}
          </Button>
        </div>
      </Card>
      <RunProgressCard run={run} />
    </div>
  );
}

function statusMessage(
  runError: string | null,
  status: string,
  hasReport: boolean
): string {
  if (runError) return runError;
  if (status === "cancelled") {
    return hasReport
      ? "The run was cancelled. Partial results are shown below."
      : "The run was cancelled before any results were produced.";
  }
  if (status === "interrupted") {
    return hasReport
      ? "The run was interrupted (the server restarted). Partial results are shown below."
      : "The run was interrupted (the server restarted).";
  }
  return "The research run failed. Please try again.";
}
