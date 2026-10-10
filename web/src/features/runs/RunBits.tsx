/**
 * Shared research-run UI: status badges, live subscription hook
 * (SSE with polling fallback), and the live progress card.
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Radio } from "lucide-react";
import { api } from "../../lib/api";
import type { ResearchRun, RunStatus } from "../../types";
import { formatDateTime } from "../../lib/format";
import { Badge, Card, CardHeader, ProgressBar } from "../../components/ui";
import { cn } from "../../lib/cn";

export const TERMINAL_STATUSES: RunStatus[] = [
  "completed",
  "failed",
  "cancelled",
  "interrupted",
];
export const isTerminal = (s: RunStatus) => TERMINAL_STATUSES.includes(s);

const STATUS_TONE: Record<RunStatus, "up" | "down" | "mixed" | "info" | "neutral"> = {
  queued: "neutral",
  running: "info",
  completed: "up",
  failed: "down",
  cancelled: "mixed",
  interrupted: "mixed",
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {(status === "running" || status === "queued") && (
        <span className="animate-pulse-dot h-1.5 w-1.5 rounded-full bg-indigo-400" />
      )}
      <Badge tone={STATUS_TONE[status]}>{status}</Badge>
    </span>
  );
}

/**
 * Live view of a run: initial fetch, then SSE updates, falling back to
 * polling if the event stream cannot be established.
 */
export function useRunLive(runId: string | undefined) {
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const poller = useRef<number | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let cleanup: (() => void) | null = null;

    const stopPolling = () => {
      if (poller.current) {
        clearInterval(poller.current);
        poller.current = null;
      }
    };
    const applyRun = (r: ResearchRun) => {
      if (cancelled) return;
      setRun(r);
      if (isTerminal(r.status)) {
        stopPolling();
        cleanup?.();
        setStreaming(false);
      }
    };
    const startPolling = () => {
      if (poller.current || cancelled) return;
      setStreaming(false);
      poller.current = window.setInterval(async () => {
        try {
          applyRun(await api.run(runId));
        } catch {
          /* keep trying; the banner shows the last known state */
        }
      }, 3000);
    };

    (async () => {
      try {
        const initial = await api.run(runId);
        if (cancelled) return;
        applyRun(initial);
        if (isTerminal(initial.status)) return;
        // Live updates via SSE; fall back to polling on stream failure.
        cleanup = api.subscribeToRun(
          runId,
          (evt) => {
            const payload = evt as { type: string; run?: ResearchRun };
            if (payload.run) applyRun(payload.run);
          },
          () => startPolling()
        );
        setStreaming(true);
      } catch (e) {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "Failed to load run.");
      }
    })();

    return () => {
      cancelled = true;
      stopPolling();
      cleanup?.();
    };
  }, [runId]);

  return { run, error, streaming, setRun };
}

export const QUERY_STAGE_LABELS: Record<string, string> = {
  // Agentic research loop (default for query runs).
  plan: "Planning the research",
  tool: "Gathering evidence",
  synthesize: "Synthesizing the answer",
  validate: "Validating sources",
  report: "Writing report",
  // Legacy simple pipeline (query runs with agentic: false).
  search: "Searching the web",
  extract: "Extracting articles",
  deduplicate: "Deduplicating",
  analyze: "Analysing with LLM",
  done: "Done",
};

const AGENT_STAGE_ORDER = ["plan", "tool", "synthesize", "validate", "report"];
const SIMPLE_STAGE_SET = new Set(["search", "extract", "deduplicate", "analyze"]);

export function RunProgressCard({ run }: { run: ResearchRun }) {
  if (run.mode === "query") return <QueryRunProgressCard run={run} />;

  const done = run.completed + run.failed;
  const active = !isTerminal(run.status);
  const remaining = Math.max(0, run.total_companies - done);

  return (
    <Card>
      <CardHeader
        title={`Run ${run.run_id}`}
        subtitle={`Started ${formatDateTime(run.started_at ?? run.created_at)}`}
        action={<RunStatusBadge status={run.status} />}
      />
      <div className="space-y-4 px-5 py-4">
        <div>
          <div className="mb-1.5 flex items-center justify-between text-xs text-terminal-muted">
            <span>
              {done} of {run.total_companies} companies
              {run.failed > 0 && (
                <span className="text-red-300"> · {run.failed} failed</span>
              )}
            </span>
            <span className="tabular-nums">{run.articles_found} articles</span>
          </div>
          <ProgressBar value={done} max={run.total_companies} />
        </div>

        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <Info label="Current" value={active ? (run.current_company ?? "…") : "—"} mono />
          <Info label="Remaining" value={active ? String(remaining) : "0"} />
          <Info
            label="Finished"
            value={run.finished_at ? formatDateTime(run.finished_at) : "—"}
          />
          <Info
            label="Report"
            value={
              run.report_id ? (
                <Link
                  to={`/reports/${run.report_id}`}
                  className="font-mono text-xs text-indigo-300 underline"
                >
                  {run.report_id}
                </Link>
              ) : (
                "—"
              )
            }
          />
        </dl>

        {run.error && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3.5 py-3 text-sm text-red-200">
            {run.error}
          </div>
        )}

        {run.companies.length > 0 && (
          <div>
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-terminal-faint">
              Per-company progress
            </h4>
            <div className="max-h-56 overflow-y-auto rounded-lg border border-terminal-border">
              <table className="w-full text-left text-xs">
                <tbody>
                  {run.companies.map((c) => (
                    <tr
                      key={c.ticker}
                      className="border-b border-terminal-border/50 last:border-0"
                    >
                      <td className="px-3 py-1.5 font-mono text-terminal-text">
                        {c.ticker}
                      </td>
                      <td className="px-3 py-1.5">
                        <span
                          className={cn(
                            c.status === "completed"
                              ? "text-emerald-300"
                              : "text-red-300"
                          )}
                        >
                          {c.status}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 tabular-nums text-terminal-muted">
                        {c.articles} articles
                      </td>
                      <td className="max-w-[280px] truncate px-3 py-1.5 text-terminal-faint">
                        {c.notes}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {active && (
          <p className="flex items-center gap-1.5 text-xs text-terminal-faint">
            <Radio className="h-3.5 w-3.5 text-indigo-400" />
            Live updates via server-sent events
          </p>
        )}
      </div>
    </Card>
  );
}

function Info({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">{label}</dt>
      <dd className={cn("mt-0.5 truncate text-terminal-text", mono && "font-mono text-xs")}>
        {value}
      </dd>
    </div>
  );
}

const QUERY_STAGE_ORDER = ["search", "extract", "deduplicate", "analyze", "report"];

export function QueryRunProgressCard({ run }: { run: ResearchRun }) {
  const active = !isTerminal(run.status);
  const stats = run.stats as Record<string, Record<string, unknown> | unknown>;
  const counts = {
    collected: num(stats["search"], "collected"),
    extracted: num(stats["extract"], "extracted"),
    candidates: num(stats["deduplicate"], "candidates"),
    findings: num(stats["analyze"], "findings"),
    toolCalls: num(stats["tool"], "call"),
    evidence: num(stats["synthesize"], "evidence_items"),
  };
  // The agentic loop (default) streams plan/tool/synthesize/validate/report;
  // the legacy simple pipeline streams search/extract/deduplicate/analyze/report.
  // A run is "simple" when explicitly requested, or when its current stage
  // belongs to the legacy set (older runs).
  const stage = run.stage ?? "";
  const simple =
    run.request?.agentic === false || SIMPLE_STAGE_SET.has(stage);
  const order = simple ? QUERY_STAGE_ORDER : AGENT_STAGE_ORDER;
  const currentIdx = stage ? order.indexOf(stage) : -1;

  return (
    <Card>
      <CardHeader
        title="Query research"
        subtitle={run.query ?? ""}
        action={<RunStatusBadge status={run.status} />}
      />
      <div className="space-y-4 px-5 py-4">
        {/* Honest stage tracker — no invented percentages */}
        <ol className="space-y-2">
          {order.map((s, i) => {
            const done = currentIdx > i || run.stage === "done";
            const current = run.stage === s && active;
            return (
              <li key={s} className="flex items-center gap-3 text-sm">
                <span
                  className={cn(
                    "flex h-5 w-5 items-center justify-center rounded-full border text-[10px]",
                    done
                      ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                      : current
                        ? "animate-pulse-dot border-indigo-400/60 bg-indigo-500/10 text-indigo-300"
                        : "border-terminal-border text-terminal-faint"
                  )}
                >
                  {done ? "✓" : i + 1}
                </span>
                <span className={cn(done || current ? "text-white" : "text-terminal-faint")}>
                  {QUERY_STAGE_LABELS[s]}
                </span>
                {s === "tool" && counts.toolCalls > 0 && (
                  <span className="text-xs text-terminal-faint">{counts.toolCalls} tool calls</span>
                )}
                {s === "synthesize" && counts.evidence > 0 && (
                  <span className="text-xs text-terminal-faint">{counts.evidence} evidence items</span>
                )}
                {s === "search" && counts.collected > 0 && (
                  <span className="text-xs text-terminal-faint">{counts.collected} collected</span>
                )}
                {s === "extract" && counts.extracted > 0 && (
                  <span className="text-xs text-terminal-faint">{counts.extracted} extracted</span>
                )}
                {s === "deduplicate" && counts.candidates > 0 && (
                  <span className="text-xs text-terminal-faint">{counts.candidates} candidates</span>
                )}
                {s === "analyze" && counts.findings > 0 && (
                  <span className="text-xs text-terminal-faint">{counts.findings} findings</span>
                )}
              </li>
            );
          })}
        </ol>

        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <Info label="Findings" value={String(run.articles_found)} />
          <Info
            label="Finished"
            value={run.finished_at ? formatDateTime(run.finished_at) : "—"}
          />
          <Info
            label="Report"
            value={
              run.report_id ? (
                <Link
                  to={`/reports/${run.report_id}`}
                  className="font-mono text-xs text-indigo-300 underline"
                >
                  {run.report_id}
                </Link>
              ) : (
                "—"
              )
            }
          />
          <Info
            label="Fresh search"
            value={run.request?.fresh_search ? "yes" : "no"}
          />
        </dl>

        {run.error && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3.5 py-3 text-sm text-red-200">
            {run.error}
          </div>
        )}

        {active && (
          <p className="flex items-center gap-1.5 text-xs text-terminal-faint">
            <Radio className="h-3.5 w-3.5 text-indigo-400" />
            Live updates via server-sent events
          </p>
        )}
      </div>
    </Card>
  );
}

function num(stage: unknown, key: string): number {
  if (typeof stage === "object" && stage !== null) {
    const v = (stage as Record<string, unknown>)[key];
    return typeof v === "number" ? v : 0;
  }
  return 0;
}
