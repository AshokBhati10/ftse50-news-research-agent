/**
 * Report detail: query reports render the answer-first presentation; legacy
 * FTSE company reports render a searchable, filterable ranked-article table.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ArrowUpDown, ExternalLink, FileText, Search } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { ArticleWithCompany, ReportDetail } from "../../types";
import { formatDate, hostOf } from "../../lib/format";
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
import QueryReportView, { ReportDownloads } from "./QueryReportView";

type SortKey = "impact" | "company" | "published";

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
    setReport(null);
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

      <ReportDownloads report={report} />

      <ArticleDialog article={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
