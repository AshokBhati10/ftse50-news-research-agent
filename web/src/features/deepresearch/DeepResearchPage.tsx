/**
 * Deep Research — submits a question to the background job API, polls its status and shows the cited report.
 *
 * The job id is kept in localStorage so a refresh resumes polling (or re-shows the finished report) instead of
 * losing the run. It is dropped only when the backend no longer knows the job (404).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Telescope } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { DeepJobStatus, DeepResearchHistoryItem, DeepResearchJob, DeepResearchResult } from "../../types";
import { Alert, Badge, Button, Card, CardHeader, Field, Spinner, Textarea } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { Markdown } from "../../lib/markdown";

const STORAGE_KEY = "deepResearchJobId";
const POLL_MS = 5000;
const TONE: Record<DeepJobStatus, "neutral" | "mixed" | "up" | "down"> = {
  pending: "neutral",
  running: "mixed",
  completed: "up",
  failed: "down",
};

export const isDone = (s: DeepJobStatus) => s === "completed" || s === "failed";

export default function DeepResearchPage() {
  const [question, setQuestion] = useState("");
  const [job, setJob] = useState<DeepResearchJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState<DeepResearchHistoryItem[] | null>(null); // null = not loaded yet
  const [historyError, setHistoryError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeId = useRef<string | null>(null); // the job the page is showing; late replies for others are ignored

  const stop = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api.deepResearchHistory());
      setHistoryError(null);
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : "Could not load previous research.");
    }
  }, []);

  const poll = useCallback(async (id: string) => {
    try {
      const next = await api.deepResearchJob(id);
      if (activeId.current !== id) return; // user switched to another report meanwhile
      setJob(next);
      setError(null);
      // Keep the id after completion: the backend retains results for 24h, so a refresh shows the report again.
      if (isDone(next.status)) {
        void loadHistory();
        return;
      }
    } catch (e) {
      if (activeId.current !== id) return;
      if (e instanceof ApiError && e.status === 404) {
        localStorage.removeItem(STORAGE_KEY);
        activeId.current = null;
        setJob(null);
        setError("That job no longer exists.");
        return;
      }
      setError(e instanceof Error ? e.message : "Could not read job status.");
    }
    timer.current = setTimeout(() => void poll(id), POLL_MS);
  }, [loadHistory]);

  // Resume a job after a page refresh.
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      activeId.current = saved;
      void poll(saved);
    }
    void loadHistory();
    return stop;
  }, [poll, loadHistory]);

  const open = (id: string) => {
    stop();
    setError(null);
    setJob(null);
    localStorage.setItem(STORAGE_KEY, id);
    activeId.current = id;
    void poll(id);
    window.scrollTo?.({ top: 0 });
  };

  const submit = async () => {
    const q = question.trim();
    if (q.length < 3) return;
    stop();
    setSubmitting(true);
    setError(null);
    setJob(null);
    try {
      const created = await api.startDeepResearch(q);
      localStorage.setItem(STORAGE_KEY, created.job_id);
      activeId.current = created.job_id;
      setJob(created);
      void loadHistory();
      void poll(created.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the research job.");
    } finally {
      setSubmitting(false);
    }
  };

  const active = job !== null && !isDone(job.status);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="Deep Research"
          subtitle="Plans sub-questions, routes them to news / science / IT / general search, reads pages and cites sources. Runs in the background."
        />
        <div className="space-y-4 px-5 py-4">
          <Field label="Question">
            <Textarea
              value={question}
              maxLength={2000}
              disabled={submitting || active}
              placeholder="e.g. What are the major NVIDIA business and financial developments over the past month?"
              onChange={(e) => setQuestion(e.target.value)}
            />
          </Field>
          <Button onClick={() => void submit()} disabled={submitting || active || question.trim().length < 3}>
            <Telescope className="h-4 w-4" /> {submitting ? "Submitting…" : "Start research"}
          </Button>
        </div>
      </Card>

      {error && <Alert tone="down">{error}</Alert>}
      {job && <JobStatus job={job} />}
      {job?.status === "completed" && job.result && <ResultView result={job.result} />}
      <History items={history} error={historyError} currentId={job?.job_id} onRetry={() => void loadHistory()} onOpen={open} />
    </div>
  );
}

function History({
  items,
  error,
  currentId,
  onOpen,
  onRetry,
}: {
  items: DeepResearchHistoryItem[] | null;
  error: string | null;
  currentId?: string;
  onOpen: (id: string) => void;
  onRetry: () => void;
}) {
  return (
    <Card>
      <CardHeader title="Previous research" subtitle="Recent runs are kept for 24 hours" />
      {error && (
        <div className="space-y-2 px-5 py-4">
          <Alert tone="down">Could not load previous research: {error}</Alert>
          <Button variant="ghost" onClick={onRetry}>
            Retry
          </Button>
        </div>
      )}
      {!error && items === null && <p className="px-5 py-4 text-sm text-terminal-muted">Loading previous research…</p>}
      {!error && items?.length === 0 && (
        <p className="px-5 py-4 text-sm text-terminal-muted">No previous research yet. Completed runs will appear here.</p>
      )}
      {items && items.length > 0 && (
        <ul className="divide-y divide-terminal-border/60">
          {items.map((h) => (
            <li key={h.job_id} className="flex flex-wrap items-center gap-3 px-5 py-2.5 text-sm">
              <Badge tone={TONE[h.status]}>{h.status}</Badge>
              <span className="min-w-0 flex-1 truncate text-terminal-text" title={h.question}>
                {h.question}
              </span>
              {h.created_at && <span className="text-xs text-terminal-faint">{formatDateTime(h.created_at)}</span>}
              <Button variant="ghost" disabled={h.job_id === currentId} onClick={() => onOpen(h.job_id)}>
                {h.job_id === currentId ? "Showing" : "View"}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function JobStatus({ job }: { job: DeepResearchJob }) {
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 px-5 py-4 text-sm">
        <Badge tone={TONE[job.status]}>{job.status}</Badge>
        <span className="font-mono text-xs text-terminal-faint">{job.job_id}</span>
        {job.deduplicated && <span className="text-xs text-terminal-faint">(same question already submitted — reusing that job)</span>}
      </div>
      {(job.status === "pending" || job.status === "running") && (
        <div role="status" aria-live="polite" className="flex items-center gap-3 border-t border-terminal-border/60 px-5 py-4">
          <Spinner className="h-6 w-6 shrink-0" />
          <div className="text-sm">
            <p className="font-medium text-white">
              {job.status === "pending"
                ? "Your question is queued — waiting for a worker."
                : job.retrying
                  ? "Retrying after a temporary error…"
                  : "Researching your question…"}
            </p>
            <p className="text-terminal-muted">
              Searching, reading sources and writing the answer. You can leave this page and come back; it keeps running
              in the background.
            </p>
          </div>
        </div>
      )}
      {job.status === "failed" && (
        <div className="px-5 pb-4">
          <Alert tone="down">{job.error ?? "The research job failed."}</Alert>
        </div>
      )}
    </Card>
  );
}

function ResultView({ result }: { result: DeepResearchResult }) {
  // Only sources the final answer actually cites.
  const citedUrls = new Set((result.citations ?? []).map((c) => c.url));
  const used = result.sources.filter((s) => citedUrls.has(s.url));
  return (
    <>
      <Card className="border-indigo-500/30">
        <div className="space-y-3 px-6 py-5">
          <h3 className="text-sm font-semibold text-white">Answer</h3>
          <Markdown text={result.answer_markdown} className="text-[15px] leading-7 text-white [&_p]:text-white" />
        </div>
      </Card>

      {used.length > 0 && (
        <Card>
          <CardHeader title="Sources retrieved" />
          <ul className="divide-y divide-terminal-border/60">
            {used.map((s) => (
              <li key={s.url} className="flex flex-wrap items-center gap-2 px-5 py-2.5 text-sm">
                <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-indigo-300 hover:underline">
                  {s.title || s.source}
                </a>
                <span className="text-xs text-terminal-faint">
                  {s.source}
                  {s.published ? ` · ${s.published.slice(0, 10)}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
