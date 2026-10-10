/**
 * Companies directory: the real FTSE 50 universe from the backend,
 * enriched with coverage stats from the latest report when available.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpDown, Building2, Search } from "lucide-react";
import { api } from "../../lib/api";
import type { Company, ReportDetail } from "../../types";
import { formatMarketCap } from "../../lib/format";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingState,
  sentimentTone,
  TextInput,
} from "../../components/ui";
import { cn } from "../../lib/cn";

interface Row extends Company {
  covered: boolean;
  articleCount: number;
  positive: number;
  negative: number;
}

export default function CompaniesPage() {
  const [companies, setCompanies] = useState<Company[] | null>(null);
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sortDesc, setSortDesc] = useState(true);

  const load = async () => {
    setError(null);
    try {
      const [res, latest] = await Promise.all([
        api.companies(),
        api.latestReport().catch(() => null),
      ]);
      setCompanies(res.companies);
      setReport(latest);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load companies.");
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rows: Row[] = useMemo(() => {
    if (!companies) return [];
    const byTicker = new Map(report?.companies.map((c) => [c.ticker, c]) ?? []);
    return companies.map((c) => {
      const rc = byTicker.get(c.ticker);
      const arts = rc?.articles ?? [];
      return {
        ...c,
        covered: !!rc,
        articleCount: arts.length,
        positive: arts.filter((a) => a.sentiment === "positive").length,
        negative: arts.filter((a) => a.sentiment === "negative").length,
      };
    });
  }, [companies, report]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? rows.filter(
          (r) => r.name.toLowerCase().includes(q) || r.ticker.toLowerCase().includes(q)
        )
      : rows;
    return [...filtered].sort((a, b) =>
      sortDesc
        ? (b.market_cap ?? 0) - (a.market_cap ?? 0)
        : (a.market_cap ?? 0) - (b.market_cap ?? 0)
    );
  }, [rows, query, sortDesc]);

  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!companies) return <LoadingState message="Loading company universe…" />;

  return (
    <Card>
      <CardHeader
        title={`FTSE 50 companies (${companies.length})`}
        subtitle={
          report
            ? `Coverage from ${report.summary.id}`
            : "No report available yet — coverage data will appear after the first run"
        }
      />
      <div className="border-b border-terminal-border px-5 py-4">
        <div className="relative max-w-md">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-terminal-faint" />
          <TextInput
            placeholder="Search by name or ticker…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>
      {visible.length === 0 ? (
        <EmptyState title="No companies match" message={`Nothing matches “${query}”.`} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead>
              <tr className="border-b border-terminal-border text-xs uppercase tracking-wide text-terminal-faint">
                <th className="px-5 py-3 font-medium">Company</th>
                <th className="px-3 py-3 font-medium">
                  <button
                    className="inline-flex items-center gap-1 uppercase tracking-wide hover:text-white"
                    onClick={() => setSortDesc((d) => !d)}
                  >
                    Market cap
                    <ArrowUpDown className={cn("h-3 w-3", !sortDesc && "rotate-180")} />
                  </button>
                </th>
                <th className="px-3 py-3 font-medium">Coverage</th>
                <th className="px-3 py-3 font-medium">Articles</th>
                <th className="px-3 py-3 font-medium">Signal</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <tr key={r.ticker} className="border-b border-terminal-border/50 last:border-0 hover:bg-white/[0.02]">
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-2.5">
                      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/5 text-terminal-muted">
                        <Building2 className="h-4 w-4" />
                      </span>
                      <div>
                        <div className="font-medium text-white">{r.name}</div>
                        <div className="font-mono text-xs text-terminal-faint">{r.ticker}</div>
                      </div>
                    </div>
                  </td>
                  <td className="whitespace-nowrap px-3 py-3 tabular-nums text-terminal-muted">
                    {formatMarketCap(r.market_cap)}
                  </td>
                  <td className="px-3 py-3">
                    {r.covered ? (
                      <Badge tone="info">researched</Badge>
                    ) : (
                      <Badge tone="neutral">not yet</Badge>
                    )}
                  </td>
                  <td className="px-3 py-3 tabular-nums text-terminal-muted">{r.articleCount}</td>
                  <td className="px-3 py-3">
                    {r.articleCount === 0 ? (
                      <span className="text-xs text-terminal-faint">—</span>
                    ) : (
                      <span className="inline-flex gap-1.5 text-xs">
                        <Badge tone={sentimentTone("positive")}>+{r.positive}</Badge>
                        <Badge tone={sentimentTone("negative")}>−{r.negative}</Badge>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="border-t border-terminal-border px-5 py-3">
        <p className="text-xs text-terminal-faint">
          Company universe comes from the backend (FTSE 100 ingestion → top 50 by
          market cap). Historical per-company research is not retained — coverage
          reflects the latest report only.{" "}
          <Link to="/runs" className="text-indigo-300 hover:underline">
            Start a run
          </Link>{" "}
          to refresh it.
        </p>
      </div>
    </Card>
  );
}
