/** Reports library: lists every generated report, newest first. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Download, FileText } from "lucide-react";
import { api } from "../../lib/api";
import type { ReportsResponse } from "../../types";
import { formatDateTime } from "../../lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingState,
} from "../../components/ui";

export default function ReportsPage() {
  const [data, setData] = useState<ReportsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      setData(await api.reports());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load reports.");
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!data) return <LoadingState message="Loading reports…" />;
  if (data.count === 0)
    return (
      <EmptyState
        title="No reports yet"
        message="Generated reports (report_YYYY-MM-DD.json/.md) will appear here once a research run completes."
        icon={<FileText className="h-6 w-6" />}
        action={
          <Link to="/runs">
            <Button variant="primary">Start a research run</Button>
          </Link>
        }
      />
    );

  return (
    <Card>
      <CardHeader
        title="Research reports"
        subtitle={`${data.count} report${data.count === 1 ? "" : "s"} · newest first`}
      />
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead>
            <tr className="border-b border-terminal-border text-xs uppercase tracking-wide text-terminal-faint">
              <th className="px-5 py-3 font-medium">Report</th>
              <th className="px-3 py-3 font-medium">Generated</th>
              <th className="px-3 py-3 font-medium">Items</th>
              <th className="px-3 py-3 font-medium">Download</th>
            </tr>
          </thead>
          <tbody>
            {data.reports.map((r) => (
              <tr key={r.id} className="border-b border-terminal-border/50 last:border-0 hover:bg-white/[0.02]">
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <Link to={`/reports/${r.id}`} className="font-medium text-white hover:underline">
                      {r.title || r.id}
                    </Link>
                    <Badge tone={r.report_type === "query" ? "info" : "neutral"}>
                      {r.report_type === "query" ? "query" : "FTSE 50"}
                    </Badge>
                  </div>
                  <div className="mt-0.5 max-w-[320px] truncate font-mono text-xs text-terminal-faint">
                    {r.report_type === "query" ? r.id : (
                      <>{r.companies.slice(0, 6).join(", ")}
                      {r.companies.length > 6 ? ` +${r.companies.length - 6} more` : ""}</>
                    )}
                  </div>
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-xs text-terminal-muted">
                  {formatDateTime(r.generated_at)}
                </td>
                <td className="px-3 py-3 tabular-nums text-terminal-muted">
                  {r.article_count} {r.report_type === "query" ? "findings" : "articles"}
                </td>
                <td className="px-3 py-3">
                  <div className="flex gap-1">
                    <a
                      href={api.downloadUrl(r.id, "json")}
                      download
                      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-terminal-muted hover:bg-white/5 hover:text-white"
                    >
                      <Download className="h-3.5 w-3.5" /> JSON
                    </a>
                    <a
                      href={api.downloadUrl(r.id, "md")}
                      download
                      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-terminal-muted hover:bg-white/5 hover:text-white"
                    >
                      <Download className="h-3.5 w-3.5" /> MD
                    </a>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
