/**
 * Report detail: searchable, filterable and sortable view of one report's
 * ranked articles, with downloads of the original JSON/Markdown files.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ArrowUpDown, Download, ExternalLink, FileText, Search } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { ArticleWithCompany, QueryAnswer, ReportDetail } from "../../types";
import { formatConfidence, formatDate, hostOf } from "../../lib/format";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  impactTone,
  LoadingState,
  SelectInput,
  sentimentTone,
  TextInput,
} from "../../components/ui";
import ArticleDialog from "../../components/ArticleDialog";
import { cn } from "../../lib/cn";

type SortKey = "impact" | "confidence" | "company" | "published";

const IMPACT_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

export default function ReportDetailPage() {
  const { reportId } = useParams<{ reportId: string }>();
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ArticleWithCompany | null>(null);

  const [query, setQuery] = useState("");
  const [sentiment, setSentiment] = useState("all");
  const [impact, setImpact] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("impact");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const load = async () => {
    if (!reportId) return;
    setError(null);
    try {
      setReport(await api.report(reportId));
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 404
          ? `Report ${reportId} was not found.`
          : e instanceof Error ? e.message : "Failed to load the report."
      );
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportId]);

  const articles: ArticleWithCompany[] = useMemo(() => {
    if (!report) return [];
    return report.companies.flatMap((c) =>
      c.articles.map((a) => ({ ...a, company: c.company, ticker: c.ticker, report_id: report.summary.id }))
    );
  }, [report]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = articles.filter((a) => {
      if (sentiment !== "all" && a.sentiment !== sentiment) return false;
      if (impact !== "all" && a.impact !== impact) return false;
      if (q && !`${a.title} ${a.company} ${a.ticker} ${a.reason}`.toLowerCase().includes(q))
        return false;
      return true;
    });
    const dir = sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      switch (sortKey) {
        case "impact":
          return ((IMPACT_RANK[a.impact] ?? 2) - (IMPACT_RANK[b.impact] ?? 2)) * dir || (b.confidence - a.confidence) * dir;
        case "confidence":
          return (a.confidence - b.confidence) * dir;
        case "company":
          return a.company.localeCompare(b.company) * dir;
        case "published":
          return (new Date(a.published_at ?? 0).getTime() - new Date(b.published_at ?? 0).getTime()) * dir;
      }
    });
    return rows;
  }, [articles, query, sentiment, impact, sortKey, sortDir]);

  if (error) return <ErrorState message={error} />;
  if (!report) return <LoadingState message="Loading report…" />;

  if (report.summary.report_type === "query") {
    return <QueryReportView report={report} />;
  }

  const failedCompanies = report.companies.filter((c) => c.articles.length === 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/reports" className="inline-flex items-center gap-1.5 text-sm text-terminal-muted hover:text-white">
          <ArrowLeft className="h-4 w-4" /> Reports
        </Link>
        <h2 className="font-mono text-sm text-white">{report.summary.id}</h2>
        <div className="ml-auto flex gap-2">
          <a href={api.downloadUrl(report.summary.id, "json")} download>
            <Button variant="secondary" className="text-xs"><Download className="h-3.5 w-3.5" /> JSON</Button>
          </a>
          {report.has_markdown && (
            <a href={api.downloadUrl(report.summary.id, "md")} download>
              <Button variant="secondary" className="text-xs"><Download className="h-3.5 w-3.5" /> Markdown</Button>
            </a>
          )}
        </div>
      </div>

      {failedCompanies.length > 0 && (
        <Alert>
          {failedCompanies.length} of {report.companies.length} companies produced no
          articles in this report
          {failedCompanies[0]?.notes ? ` — e.g. ${failedCompanies[0].ticker}: ${failedCompanies[0].notes.slice(0, 120)}` : ""}.
        </Alert>
      )}

      <Card>
        <CardHeader
          title={`Ranked articles (${visible.length} of ${articles.length})`}
          subtitle={`${report.summary.company_count} companies · generated ${report.summary.generated_at ?? report.summary.date}`}
        />
        <div className="grid gap-3 border-b border-terminal-border px-5 py-4 sm:grid-cols-2 lg:grid-cols-5">
          <div className="relative lg:col-span-2">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-terminal-faint" />
            <TextInput placeholder="Search headline, company, ticker…" value={query} onChange={(e) => setQuery(e.target.value)} className="pl-9" />
          </div>
          <SelectInput value={sentiment} onChange={(e) => setSentiment(e.target.value)} aria-label="Filter by sentiment">
            <option value="all">All sentiments</option>
            <option value="positive">Positive</option>
            <option value="negative">Negative</option>
            <option value="neutral">Neutral</option>
          </SelectInput>
          <SelectInput value={impact} onChange={(e) => setImpact(e.target.value)} aria-label="Filter by impact">
            <option value="all">All impacts</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </SelectInput>
          <div className="flex gap-2">
            <SelectInput value={sortKey} onChange={(e) => setSortKey(e.target.value as SortKey)} aria-label="Sort by" className="flex-1">
              <option value="impact">Impact</option>
              <option value="confidence">Confidence</option>
              <option value="company">Company</option>
              <option value="published">Published</option>
            </SelectInput>
            <Button variant="ghost" onClick={() => setSortDir((d) => (d === "asc" ? "desc" : "asc"))} title="Toggle sort direction" className="px-2.5">
              <ArrowUpDown className={cn("h-4 w-4", sortDir === "desc" && "rotate-180")} />
            </Button>
          </div>
        </div>

        {visible.length === 0 ? (
          <EmptyState title="No articles match" message="Adjust the search or filters to see results from this report." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[880px] text-left text-sm">
              <thead>
                <tr className="border-b border-terminal-border text-xs uppercase tracking-wide text-terminal-faint">
                  <th className="px-5 py-3 font-medium">Company</th>
                  <th className="px-3 py-3 font-medium">Headline</th>
                  <th className="px-3 py-3 font-medium">Source</th>
                  <th className="px-3 py-3 font-medium">Published</th>
                  <th className="px-3 py-3 font-medium">Sentiment</th>
                  <th className="px-3 py-3 font-medium">Impact</th>
                  <th className="px-3 py-3 font-medium">Conf.</th>
                  <th className="px-3 py-3 font-medium">Links</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((a) => (
                  <tr key={`${a.ticker}-${a.url}`} onClick={() => setSelected(a)}
                    className="cursor-pointer border-b border-terminal-border/50 transition-colors last:border-0 hover:bg-white/[0.02]">
                    <td className="px-5 py-3">
                      <div className="font-medium text-white">{a.company}</div>
                      <div className="font-mono text-xs text-terminal-faint">{a.ticker}</div>
                    </td>
                    <td className="max-w-[340px] px-3 py-3">
                      <div className="truncate font-medium text-terminal-text" title={a.title}>{a.title}</div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-xs text-terminal-muted">{a.source || hostOf(a.url)}</td>
                    <td className="whitespace-nowrap px-3 py-3 text-xs text-terminal-muted">{formatDate(a.published_at)}</td>
                    <td className="px-3 py-3"><Badge tone={sentimentTone(a.sentiment)}>{a.sentiment}</Badge></td>
                    <td className="px-3 py-3"><Badge tone={impactTone(a.impact)}>{a.impact}</Badge></td>
                    <td className="px-3 py-3 tabular-nums text-terminal-muted">{formatConfidence(a.confidence)}</td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-1">
                        <button className="rounded p-1.5 text-terminal-muted hover:bg-white/5 hover:text-white" title="View details"
                          onClick={(e) => { e.stopPropagation(); setSelected(a); }}>
                          <FileText className="h-4 w-4" />
                        </button>
                        <a href={a.url} target="_blank" rel="noopener noreferrer" title="Open original article"
                          className="rounded p-1.5 text-terminal-muted hover:bg-white/5 hover:text-white"
                          onClick={(e) => e.stopPropagation()}>
                          <ExternalLink className="h-4 w-4" />
                        </a>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <ArticleDialog article={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

/* ---------------------------------------------------------------------------
 * Query-driven report view
 * ------------------------------------------------------------------------- */

function QueryReportView({ report }: { report: ReportDetail }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<ArticleWithCompany | null>(null);
  const meta = report.metadata as {
    run_id?: string;
    started_at?: string;
    search_config?: Record<string, unknown>;
    warnings?: string[];
    counts?: Record<string, unknown>;
    plan_summary?: Record<string, unknown>;
  };
  const cfg = (meta.search_config ?? {}) as Record<string, unknown>;
  const counts = (meta.counts ?? {}) as Record<string, unknown>;
  const warnings = (meta.warnings ?? []) as string[];
  const planSummary = (meta.plan_summary ?? null) as Record<string, unknown> | null;

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return report.findings;
    return report.findings.filter((f) =>
      `${f.title} ${f.key_finding} ${f.significance} ${f.source}`.toLowerCase().includes(q)
    );
  }, [report.findings, query]);

  const toDialog = (f: (typeof report.findings)[number]): ArticleWithCompany => ({
    title: f.title,
    url: f.url,
    source: f.source,
    published_at: f.published_at,
    impact: f.impact,
    sentiment: f.sentiment,
    confidence: f.confidence,
    reason: f.reason,
    evidence_excerpt: f.evidence_excerpt,
    source_quality: f.source_quality,
    source_quality_tier: null,
    company: "",
    ticker: "",
    report_id: report.summary.id,
    relevance: f.relevance,
    key_finding: f.key_finding,
    significance: f.significance,
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/reports" className="inline-flex items-center gap-1.5 text-sm text-terminal-muted hover:text-white">
          <ArrowLeft className="h-4 w-4" /> Reports
        </Link>
        <Badge tone="info">query research</Badge>
        <div className="ml-auto flex gap-2">
          <a href={api.downloadUrl(report.summary.id, "json")} download>
            <Button variant="secondary" className="text-xs"><Download className="h-3.5 w-3.5" /> JSON</Button>
          </a>
          {report.has_markdown && (
            <a href={api.downloadUrl(report.summary.id, "md")} download>
              <Button variant="secondary" className="text-xs"><Download className="h-3.5 w-3.5" /> Markdown</Button>
            </a>
          )}
        </div>
      </div>

      <div>
        <h2 className="text-xl font-semibold text-white">{report.query}</h2>
        <p className="mt-1 font-mono text-xs text-terminal-faint">{report.summary.id}</p>
      </div>

      {report.answer && (
        <AnswerSection answer={report.answer} />
      )}

      {warnings.length > 0 && (
        <Alert>
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}

      <Card>
        <CardHeader title="Research metadata" />
        <dl className="grid gap-3 px-5 py-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <Meta label="Run ID" value={String(meta.run_id ?? "—")} mono />
          {cfg.task_type ? (
            <>
              <Meta label="Task type" value={String(cfg.task_type).replaceAll("_", " ")} />
              <Meta label="Tool calls" value={String(counts.tool_calls ?? "—")} />
              <Meta label="Evidence items" value={String(counts.evidence_items ?? "—")} />
              <Meta label="Sources cited" value={String(counts.sources_cited ?? "—")} />
            </>
          ) : (
            <>
              <Meta label="Time range" value={String(cfg.time_range ?? "—")} />
              <Meta
                label="Search freshness"
                value={cfg.fresh_search ? "Fresh search" : cfg.from_cache ? "From cache" : "—"}
              />
              <Meta
                label="Providers"
                value={(cfg.providers_used as string[])?.join(", ") || "—"}
              />
              <Meta label="Collected" value={String(counts.collected ?? "—")} />
              <Meta label="Extracted" value={String(counts.extracted ?? "—")} />
              <Meta label="Duplicates removed" value={String(counts.duplicates_removed ?? "—")} />
              <Meta label="Analysed" value={String(counts.analyzed ?? "—")} />
            </>
          )}
        </dl>
      </Card>

      {planSummary && (
        <details className="rounded-xl border border-terminal-border bg-terminal-surface">
          <summary className="cursor-pointer px-5 py-4 text-sm font-medium text-terminal-muted hover:text-white">
            Research details — plan, tool trace and search metadata
          </summary>
          <div className="space-y-3 border-t border-terminal-border px-5 py-4 text-sm text-terminal-muted">
            {Boolean(planSummary.intent_summary) && (
              <p><span className="text-white">Plan:</span> {String(planSummary.intent_summary)}</p>
            )}
            {Array.isArray(planSummary.tool_trace) && planSummary.tool_trace.length > 0 && (
              <div>
                <p className="mb-1 text-white">Tool trace</p>
                <ul className="list-disc space-y-1 pl-4 font-mono text-xs">
                  {(planSummary.tool_trace as { tool: string; arguments_summary: string; observation_summary: string; ok: boolean }[]).map((t, i) => (
                    <li key={i}>
                      <span className={t.ok ? "text-green-300" : "text-red-300"}>{t.tool}</span>
                      <span className="text-terminal-faint"> {t.arguments_summary}</span>
                      {" → "}{t.observation_summary}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      )}

      {report.findings.length > 0 && (
      <Card>
        <CardHeader
          title={`Ranked findings (${visible.length})`}
          subtitle="Ordered by relevance to your question"
        />
        <div className="border-b border-terminal-border px-5 py-4">
          <div className="relative max-w-md">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-terminal-faint" />
            <TextInput
              placeholder="Filter findings…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="pl-9"
            />
          </div>
        </div>
        {visible.length === 0 ? (
          <EmptyState
            title={report.findings.length === 0 ? "No findings" : "No matches"}
            message={
              report.findings.length === 0
                ? "The agent could not extract analysable articles for this query. Check the warnings above."
                : "No findings match your filter."
            }
          />
        ) : (
          <ol className="divide-y divide-terminal-border/60">
            {visible.map((f, i) => (
              <li key={f.url} className="px-5 py-4">
                <div className="flex items-start gap-4">
                  <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-indigo-500/10 font-mono text-xs text-indigo-300">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <button
                      onClick={() => setSelected(toDialog(f))}
                      className="text-left font-medium text-white hover:underline"
                    >
                      {f.title}
                    </button>
                    <p className="mt-1 text-sm text-terminal-text">
                      <span className="text-terminal-faint">Key finding: </span>
                      {f.key_finding}
                    </p>
                    {f.significance && (
                      <p className="mt-0.5 text-sm text-terminal-muted">
                        <span className="text-terminal-faint">Why it matters: </span>
                        {f.significance}
                      </p>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1.5 text-xs text-terminal-muted">
                        Relevance
                        <span className="h-1.5 w-20 overflow-hidden rounded-full bg-white/10">
                          <span
                            className="block h-full rounded-full bg-indigo-400"
                            style={{ width: `${Math.round(f.relevance * 100)}%` }}
                          />
                        </span>
                        <span className="tabular-nums">{Math.round(f.relevance * 100)}%</span>
                      </span>
                      <Badge tone={impactTone(f.impact)}>{f.impact}</Badge>
                      <Badge tone={sentimentTone(f.sentiment)}>{f.sentiment}</Badge>
                      <span className="text-xs text-terminal-faint">
                        {formatConfidence(f.confidence)} confidence · {f.source || hostOf(f.url)} ·{" "}
                        {formatDate(f.published_at)}
                      </span>
                      <a
                        href={f.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-indigo-300 hover:underline"
                      >
                        <ExternalLink className="h-3.5 w-3.5" /> Source
                      </a>
                    </div>
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>
      )}

      <ArticleDialog article={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">{label}</dt>
      <dd className={cn("mt-0.5 text-terminal-text", mono && "font-mono text-xs")}>{value}</dd>
    </div>
  );
}

function AnswerSection({ answer }: { answer: QueryAnswer }) {
  const pct = Math.round(answer.confidence * 100);
  return (
    <div className="space-y-4">
      <Card className="border-indigo-500/30">
        <div className="px-5 py-5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] font-semibold uppercase tracking-widest text-indigo-300">
              Answer
            </span>
            <Badge tone={answer.verified ? "up" : "down"}>
              {answer.verified ? "verified" : "unverified"}
            </Badge>
            <span className="ml-auto text-xs text-terminal-faint">
              {answer.task_type.replaceAll("_", " ")} · {pct}% confidence
            </span>
          </div>
          <p className="mt-3 text-lg leading-relaxed text-white">{answer.answer}</p>
        </div>
      </Card>

      {answer.key_points.length > 0 && (
        <Card>
          <CardHeader title="Key points" />
          <ol className="list-decimal space-y-2 px-5 py-4 pl-10 text-sm text-terminal-text">
            {answer.key_points.map((kp, i) => (
              <li key={i}>{kp}</li>
            ))}
          </ol>
        </Card>
      )}

      {answer.sources.length > 0 && (
        <Card>
          <CardHeader title={`Sources (${answer.sources.length})`} subtitle="Citations supporting the answer" />
          <ul className="divide-y divide-terminal-border">
            {answer.sources.map((s, i) => (
              <li key={i} className="px-5 py-3">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-sm font-medium text-indigo-300 hover:underline"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  {s.title || s.url}
                </a>
                <p className="mt-0.5 text-xs text-terminal-faint">
                  {s.source_name || hostOf(s.url)}
                  {s.published_at ? ` · ${formatDate(s.published_at)}` : ""}
                </p>
                {s.supporting_excerpt && (
                  <p className="mt-1 text-xs italic text-terminal-muted">
                    “{s.supporting_excerpt}”
                  </p>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {(answer.limitations.length > 0 || !answer.verified) && (
        <Card>
          <CardHeader title="Limitations" />
          <ul className="list-disc space-y-1 px-5 py-4 pl-10 text-sm text-terminal-muted">
            {answer.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
