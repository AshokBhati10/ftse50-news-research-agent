/**
 * Reusable article detail dialog. Article content is treated as untrusted
 * external text and rendered as plain text only — never as HTML.
 */
import { ExternalLink } from "lucide-react";
import type { ArticleWithCompany } from "../types";
import { formatConfidence, formatDate, hostOf } from "../lib/format";
import { Badge, Dialog, impactTone, sentimentTone } from "./ui";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[130px_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-xs font-medium uppercase tracking-wide text-terminal-faint">
        {label}
      </dt>
      <dd className="min-w-0 text-terminal-text">{children}</dd>
    </div>
  );
}

export default function ArticleDialog({
  article,
  onClose,
}: {
  article: ArticleWithCompany | null;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={article !== null}
      onClose={onClose}
      wide
      title={
        article ? (
          <span className="block truncate">{article.title}</span>
        ) : (
          "Article"
        )
      }
    >
      {article && (
        <div className="animate-fade-slide-in">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <Badge tone={sentimentTone(article.sentiment)}>{article.sentiment}</Badge>
            <Badge tone={impactTone(article.impact)}>{article.impact} impact</Badge>
            <span className="text-xs text-terminal-muted">
              {formatConfidence(article.confidence)} confidence
            </span>
          </div>

          <dl className="divide-y divide-terminal-border/60">
            <Row label="Company">
              {article.company}{" "}
              <span className="font-mono text-xs text-terminal-muted">{article.ticker}</span>
            </Row>
            <Row label="Source">
              {article.source || hostOf(article.url)}{" "}
              {article.source_quality_tier && (
                <span className="text-xs text-terminal-faint">
                  · {article.source_quality_tier.replace(/_/g, " ")}
                  {article.source_quality != null &&
                    ` (${Math.round(article.source_quality * 100)}%)`}
                </span>
              )}
            </Row>
            <Row label="Published">{formatDate(article.published_at)}</Row>
            <Row label="Report">
              <span className="font-mono text-xs">{article.report_id}</span>
            </Row>
          </dl>

          {article.reason && (
            <div className="mt-4">
              <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-terminal-faint">
                Analysis
              </h4>
              <p className="text-sm leading-relaxed text-terminal-text">{article.reason}</p>
            </div>
          )}

          {article.evidence_excerpt && (
            <div className="mt-4">
              <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-terminal-faint">
                Evidence excerpt
              </h4>
              <blockquote className="rounded-lg border-l-2 border-indigo-500/60 bg-white/[0.03] px-4 py-3 text-sm leading-relaxed text-terminal-muted">
                {article.evidence_excerpt}
              </blockquote>
            </div>
          )}

          <div className="mt-5 flex justify-end">
            <a
              href={article.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-md bg-indigo-500 px-3.5 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-400"
            >
              <ExternalLink className="h-4 w-4" />
              Open original article
            </a>
          </div>
        </div>
      )}
    </Dialog>
  );
}
