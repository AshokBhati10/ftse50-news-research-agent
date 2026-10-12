/**
 * Query-driven report presentation: a scannable, financial-brief style layout.
 *
 * Structure: Executive Summary (the answer, reflowed into short paragraphs)
 * with Caveats, then a Key Findings section (numbered major developments with
 * source and date) and a Findings section (detailed analysis and business
 * impact). Duplicate stories/explanations are removed across the sections.
 * Downloads sit at the bottom. Shared by the standalone report page and the
 * run page (embedded) so a completed run shows its answer inline.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Download, Search } from "lucide-react";
import { api } from "../../lib/api";
import type { ArticleWithCompany, QueryAnswer, ReportDetail } from "../../types";
import { formatDate, hostOf } from "../../lib/format";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  impactTone,
  TextInput,
} from "../../components/ui";
import ArticleDialog from "../../components/ArticleDialog";
import { InlineMarkdown, Markdown } from "../../lib/markdown";
import { buildExecutiveSummary } from "../../lib/executiveSummary";
import {
  claimStatus,
  dateLabel,
  dedupeKeyPoints,
  extractHighlights,
  mergeFindings,
  prioritizeFindings,
} from "../../lib/report";
import type { ClaimStatus, MergedFinding } from "../../lib/report";

const STATUS_TONE: Record<ClaimStatus, "up" | "mixed" | "down"> = {
  confirmed: "up",
  reported: "mixed",
  unverified: "down",
};

export default function QueryReportView({
  report,
  embedded = false,
}: {
  report: ReportDetail;
  embedded?: boolean;
}) {
  const [selected, setSelected] = useState<ArticleWithCompany | null>(null);
  const warnings = Array.isArray(report.metadata?.warnings)
    ? (report.metadata.warnings as string[])
    : [];
  const highlights = useMemo(() => extractHighlights(report.query), [report.query]);
  const findings = useMemo(
    () => prioritizeFindings(mergeFindings(report.findings)),
    [report.findings]
  );
  const keyPoints = useMemo(
    () => dedupeKeyPoints(report.answer?.key_points ?? [], findings),
    [report.answer, findings]
  );

  return (
    <div className="space-y-6">
      {!embedded && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <Link to="/reports" className="inline-flex items-center gap-1.5 text-sm text-terminal-muted hover:text-white">
              <ArrowLeft className="h-4 w-4" /> Reports
            </Link>
            <p className="ml-auto font-mono text-xs text-terminal-faint">{report.summary.id}</p>
          </div>
          <h2 className="text-xl font-semibold text-white">{report.query}</h2>
        </>
      )}

      {report.answer && (
        <AnswerCard answer={report.answer} highlights={highlights} />
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

      <KeyFindingsSection
        keyPoints={keyPoints}
        sources={report.answer?.sources ?? []}
      />

      <RankedFindings
        findings={findings}
        reportId={report.summary.id}
        onOpen={setSelected}
      />

      <ReportDownloads report={report} />

      <ArticleDialog article={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

/* ------------------------------- Answer ---------------------------------- */

// Compact section headings (Overview, Key Developments, ...) inside the card,
// plus tight list spacing; the shared renderer's heading sizes are page-scale.
const SUMMARY_MD_CLASS = [
  "text-[15px] leading-7 text-white [&_p]:text-white [&_p]:mb-3 [&>p:last-child]:mb-0",
  "[&_h1]:mt-4 [&_h1]:mb-1 [&_h1]:text-sm [&_h1]:uppercase [&_h1]:tracking-wide [&_h1]:text-indigo-200",
  "[&_h2]:mt-4 [&_h2]:mb-1 [&_h2]:text-sm [&_h2]:uppercase [&_h2]:tracking-wide [&_h2]:text-indigo-200",
  "[&_h3]:mt-3 [&_h3]:mb-1 [&_h3]:text-sm [&_h3]:uppercase [&_h3]:tracking-wide [&_h3]:text-indigo-200",
  "[&_h4]:mt-3 [&_h4]:mb-1 [&_h4]:text-xs",
  "[&_ul]:my-1 [&_ol]:my-1 [&_li]:leading-6",
].join(" ");

function AnswerCard({
  answer,
  highlights,
}: {
  answer: QueryAnswer;
  highlights: string[];
}) {
  const summary = useMemo(
    () => buildExecutiveSummary(answer, highlights),
    [answer, highlights]
  );
  return (
    <Card className="border-indigo-500/30">
      <div className="space-y-3 px-6 py-5">
        <h3 className="text-sm font-semibold text-white">Executive Summary</h3>
        <Markdown
          text={summary}
          className={SUMMARY_MD_CLASS}
        />
      </div>
    </Card>
  );
}

/* --------------------------- Key findings -------------------------------- */

/** Concise, numbered takeaways from the synthesized answer. */
function KeyFindingsSection({
  keyPoints,
  sources,
}: {
  keyPoints: string[];
  sources: QueryAnswer["sources"];
}) {
  if (keyPoints.length === 0) return null;

  return (
    <Card>
      <CardHeader
        title="Key Findings"
        subtitle="Major developments — concise takeaways with source and date"
      />
      <ol className="list-decimal space-y-3 px-5 py-4 pl-10 text-sm leading-relaxed text-terminal-text marker:text-terminal-faint">
        {keyPoints.map((kp, i) => {
          const cites = sources.filter((s) => s.supports_claims.includes(i));
          return (
            <li key={i}>
              <InlineMarkdown text={kp} />
              {cites.length > 0 && (
                <span className="mt-0.5 block text-xs text-terminal-faint">
                  {cites.map((s, ci) => (
                    <span key={ci}>
                      {ci > 0 ? "; " : ""}
                      <a
                        href={s.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-indigo-300 hover:underline"
                      >
                        {s.source_name || s.title || hostOf(s.url)}
                      </a>
                      {s.published_at ? `, ${formatDate(s.published_at)}` : ""}
                    </span>
                  ))}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

/* ------------------------------ Findings --------------------------------- */

/** Detailed analysis: business impact, impact badge, source link and date. */
function RankedFindings({
  findings,
  reportId,
  onOpen,
}: {
  findings: MergedFinding[];
  reportId: string;
  onOpen: (a: ArticleWithCompany) => void;
}) {
  const [filter, setFilter] = useState("");
  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return findings;
    return findings.filter((f) =>
      `${f.title} ${f.key_finding} ${f.significance} ${f.source}`.toLowerCase().includes(q)
    );
  }, [findings, filter]);

  if (findings.length === 0) return null;

  return (
    <Card>
      <CardHeader
        title="Findings"
        subtitle="Detailed analysis and business impact, with source and date"
      />
      <div className="border-b border-terminal-border px-5 py-4">
        <div className="relative max-w-md">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-terminal-faint" />
          <TextInput
            placeholder="Filter findings…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>
      {visible.length === 0 ? (
        <EmptyState title="No matches" message="No findings match your filter." />
      ) : (
        <ol className="divide-y divide-terminal-border/60">
          {visible.map((f, i) => (
            <li key={f.url} className="px-5 py-4">
              <div className="flex items-start gap-4">
                <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-indigo-500/10 font-mono text-xs text-indigo-300">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1 space-y-1.5">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <a
                      href={f.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-semibold text-white hover:underline"
                    >
                      {f.title}
                    </a>
                    <Badge tone={impactTone(f.impact)}>{f.impact} impact</Badge>
                    <Badge tone={STATUS_TONE[claimStatus(f)]}>{claimStatus(f)}</Badge>
                  </div>
                  <p className="text-sm leading-relaxed text-terminal-text">{f.key_finding}</p>
                  {f.significance && (
                    <p className="text-sm leading-relaxed text-terminal-muted">
                      <span className="text-terminal-faint">Why it matters: </span>
                      {f.significance}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-terminal-faint">
                    <span>{f.source || hostOf(f.url)}</span>
                    <span aria-hidden>·</span>
                    <span>{dateLabel(f).text}</span>
                    <span aria-hidden>·</span>
                    <button
                      type="button"
                      onClick={() =>
                        onOpen({
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
                          report_id: reportId,
                          relevance: f.relevance,
                          key_finding: f.key_finding,
                          significance: f.significance,
                        })
                      }
                      className="text-indigo-300 hover:underline"
                    >
                      Details
                    </button>
                  </div>
                  {f.also_reported_by.length > 0 && (
                    <p className="text-xs text-terminal-faint">
                      Also reported by{" "}
                      {f.also_reported_by.map((c, ci) => (
                        <span key={ci}>
                          {ci > 0 ? ", " : ""}
                          <a
                            href={c.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-indigo-300 hover:underline"
                          >
                            {c.source || hostOf(c.url)}
                          </a>
                          {c.published_at ? ` (${formatDate(c.published_at)})` : ""}
                        </span>
                      ))}
                    </p>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

/** JSON + Markdown downloads, shown at the bottom of the report. */
export function ReportDownloads({ report }: { report: ReportDetail }) {
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 px-5 py-4">
        <span className="text-sm text-terminal-muted">Download this report</span>
        <div className="ml-auto flex flex-wrap gap-2">
          <a href={api.downloadUrl(report.summary.id, "json")} download>
            <Button variant="secondary" className="text-xs">
              <Download className="h-3.5 w-3.5" /> Download JSON
            </Button>
          </a>
          {report.has_markdown && (
            <a href={api.downloadUrl(report.summary.id, "md")} download>
              <Button variant="secondary" className="text-xs">
                <Download className="h-3.5 w-3.5" /> Download Markdown
              </Button>
            </a>
          )}
        </div>
      </div>
    </Card>
  );
}
