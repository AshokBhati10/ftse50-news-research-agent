"""Node 5 — Report generation.

Produces:
  data/output/report_YYYY-MM-DD.json  — machine-readable
  data/output/report_YYYY-MM-DD.md    — human-readable daily digest

Every finding carries its evidence: title, URL, source, publication date and
a supporting excerpt.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from config.settings import Settings
from utils.models import CompanyReport, FullReport, QueryFinding, QueryReport

log = logging.getLogger(__name__)

_IMPACT_BADGE = {"High": "🔴 High", "Medium": "🟠 Medium", "Low": "🟡 Low"}
_SENTIMENT_BADGE = {"Positive": "🟢 Positive", "Negative": "🔴 Negative", "Neutral": "⚪ Neutral"}


def _fmt_cap(market_cap: float | None) -> str:
    if not market_cap:
        return "n/a"
    if market_cap >= 1e9:
        return f"£{market_cap / 1e9:.1f}B"
    return f"£{market_cap / 1e6:.0f}M"


def _fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "unknown date"


def build_full_report(
    company_reports: list[CompanyReport],
    settings: Settings,
) -> FullReport:
    return FullReport(
        company_count=len(company_reports),
        companies=company_reports,
        config_snapshot={
            "top_n_articles": settings.top_n_articles,
            "max_articles_per_company": settings.max_articles_per_company,
            "dedup_similarity_threshold": settings.dedup_similarity_threshold,
            "news_lookback_days": settings.news_lookback_days,
            "embedding_model": settings.embedding_model,
            "ollama_model": settings.ollama_model or "(mock)",
        },
    )


def render_markdown(report: FullReport) -> str:
    date_str = report.generated_at.strftime("%Y-%m-%d")
    lines = [
        f"# FTSE 50 Market News Digest — {date_str}",
        "",
        f"_Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{report.company_count} companies · "
        f"model: {report.config_snapshot.get('ollama_model')}_",
        "",
    ]
    for i, c in enumerate(report.companies, start=1):
        lines.append(f"## {i}. {c.company} ({c.ticker}) | {_fmt_cap(c.market_cap)}")
        lines.append("")
        if not c.articles:
            lines.append(f"_No market-moving news identified. {c.notes}_".strip())
            lines.append("")
            continue
        lines.append("| # | Headline | Impact | Sentiment |")
        lines.append("|---|----------|--------|-----------|")
        for j, a in enumerate(c.articles, start=1):
            impact = _IMPACT_BADGE.get(a.impact.value, a.impact.value)
            sentiment = _SENTIMENT_BADGE.get(a.sentiment.value, a.sentiment.value)
            lines.append(f"| {j} | [{a.title}]({a.url}) | {impact} | {sentiment} |")
        lines.append("")
        for j, a in enumerate(c.articles, start=1):
            lines.append(f"**{j}. {a.title}**")
            lines.append("")
            lines.append(f"- Impact: **{a.impact.value}** · Sentiment: **{a.sentiment.value}** "
                         f"(confidence {a.confidence:.2f})")
            lines.append(f"- Why: {a.reason}")
            lines.append(f"- Source: {a.source or 'unknown'} · {_fmt_date(a.published_at)} · "
                         f"source-quality {a.source_quality:.2f}")
            if a.evidence_excerpt:
                excerpt = " ".join(a.evidence_excerpt.split())[:280]
                lines.append(f"- Evidence: _{excerpt}…_")
            lines.append(f"- Link: {a.url}")
            lines.append("")
        if c.fallback_used:
            lines.append(f"_Note: fallback search was used for this company._")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _unique_report_paths(output_dir: Path, date_str: str) -> tuple[Path, Path]:
    """JSON + Markdown paths; never silently clobber a same-day report."""
    json_path = output_dir / f"report_{date_str}.json"
    md_path = output_dir / f"report_{date_str}.md"
    if json_path.exists() or md_path.exists():
        n = 2
        while (output_dir / f"report_{date_str}_{n}.json").exists() or (
            output_dir / f"report_{date_str}_{n}.md"
        ).exists():
            n += 1
        json_path = output_dir / f"report_{date_str}_{n}.json"
        md_path = output_dir / f"report_{date_str}_{n}.md"
    return json_path, md_path


def write_reports(report: FullReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Write JSON + Markdown reports. Returns (json_path, md_path)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = report.generated_at.strftime("%Y-%m-%d")

    json_path, md_path = _unique_report_paths(output_dir, date_str)

    payload = report.model_dump(mode="json")
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    log.info("Wrote %s and %s", json_path, md_path)
    return json_path, md_path


# ---------------------------------------------------------------------------
# Query-driven research reports
# ---------------------------------------------------------------------------

def build_query_report(
    *,
    query: str,
    run_id: str,
    started_at: datetime,
    outcome,  # TopicSearchOutcome
    findings: list[QueryFinding],
    counts: dict,
    warnings: list[str],
    settings: Settings,
) -> QueryReport:
    """Assemble the machine-readable query report with full research metadata."""
    return QueryReport(
        query=query.strip(),
        run_id=run_id,
        started_at=started_at,
        search_config={
            "queries_used": outcome.queries_used,
            "time_range": outcome.time_range,
            "max_results": counts.get("max_results"),
            "fresh_search": outcome.fresh_search,
            "from_cache": outcome.from_cache,
            "providers_used": outcome.providers_used,
            "fallback_used": outcome.fallback_used,
        },
        warnings=warnings,
        counts=counts,
        findings=findings,
        config_snapshot={
            "max_results": counts.get("max_results"),
            "time_range": outcome.time_range,
            "dedup_similarity_threshold": settings.dedup_similarity_threshold,
            "embedding_model": settings.embedding_model,
            "ollama_model": settings.ollama_model or "(mock)",
        },
    )


def _executive_summary(report: QueryReport) -> str:
    """Deterministic summary derived from the ranked findings (no extra LLM)."""
    c = report.counts
    n = len(report.findings)
    if n == 0:
        return (
            f"No relevant articles could be analysed for this query. "
            f"{c.get('collected', 0)} search results were collected, "
            f"{c.get('extracted', 0)} extracted. "
            "Try a broader query or disable fresh search to reuse cached results."
        )
    high = sum(1 for f in report.findings if f.impact.value == "High")
    top = report.findings[0]
    return (
        f"{n} relevant finding{'s' if n != 1 else ''} from "
        f"{c.get('extracted', 0)} extracted articles "
        f"({c.get('collected', 0)} collected, {c.get('duplicates_removed', 0)} "
        f"duplicates removed{f', {high} high-impact' if high else ''}). "
        f"Top finding: {top.key_finding}"
    )


def render_query_markdown(report: QueryReport) -> str:
    date_str = report.generated_at.strftime("%Y-%m-%d")
    cfg = report.search_config
    c = report.counts
    lines = [
        f"# Research Report — {date_str}",
        "",
        f"> **Query:** {report.query}",
        "",
        "## Research metadata",
        "",
        f"- Run ID: `{report.run_id}`",
        f"- Started: {report.started_at.strftime('%Y-%m-%d %H:%M UTC') if report.started_at else 'n/a'}",
        f"- Completed: {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Search queries used: {len(cfg.get('queries_used', []))}",
        f"- Time range: {cfg.get('time_range', 'n/a')}",
        f"- Fresh search: {'yes' if cfg.get('fresh_search') else 'no'}"
        f" (results {'came from cache' if cfg.get('from_cache') else 'were freshly retrieved'})",
        f"- Providers: {', '.join(cfg.get('providers_used', [])) or 'none'}"
        + (" (fallback used)" if cfg.get("fallback_used") else ""),
        f"- Articles: {c.get('collected', 0)} collected → "
        f"{c.get('extracted', 0)} extracted → "
        f"{c.get('analyzed', 0)} analyzed "
        f"({c.get('duplicates_removed', 0)} duplicates removed)",
        f"- Model: {report.config_snapshot.get('ollama_model')}",
        "",
        "## Executive summary",
        "",
        _executive_summary(report),
        "",
        "## Key findings",
        "",
    ]
    if not report.findings:
        lines.append("_No findings. See limitations below._")
        lines.append("")
    else:
        lines.append("| # | Finding | Relevance | Impact |")
        lines.append("|---|---------|-----------|--------|")
        for j, f in enumerate(report.findings, start=1):
            lines.append(
                f"| {j} | {f.key_finding} | {f.relevance:.2f} | "
                f"{_IMPACT_BADGE.get(f.impact.value, f.impact.value)} |"
            )
        lines.append("")

    if report.findings:
        lines.append("## Ranked results")
        lines.append("")
        for j, f in enumerate(report.findings, start=1):
            sentiment = _SENTIMENT_BADGE.get(f.sentiment.value, f.sentiment.value)
            lines.append(f"### {j}. {f.title}")
            lines.append("")
            lines.append(f"- Relevance: **{f.relevance:.2f}** · "
                         f"Impact: **{f.impact.value}** · Sentiment: {sentiment} "
                         f"(confidence {f.confidence:.2f})")
            lines.append(f"- Key finding: {f.key_finding}")
            lines.append(f"- Significance: {f.significance}")
            lines.append(f"- Why this ranking: {f.reason}")
            lines.append(f"- Source: {f.source or 'unknown'} · {_fmt_date(f.published_at)}")
            if f.evidence_excerpt:
                excerpt = " ".join(f.evidence_excerpt.split())[:280]
                lines.append(f"- Evidence: _{excerpt}…_")
            lines.append(f"- Link: {f.url}")
            lines.append("")

    lines.append("## Source links")
    lines.append("")
    for f in report.findings:
        lines.append(f"- [{f.title}]({f.url}) — {f.source or 'unknown'}")
    if not report.findings:
        lines.append("_No sources collected._")
    lines.append("")
    lines.append("## Limitations and warnings")
    lines.append("")
    if report.warnings:
        for w in report.warnings:
            lines.append(f"- {w}")
    else:
        lines.append("- None recorded.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_query_reports(report: QueryReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Write JSON + Markdown for a query report. Returns (json_path, md_path)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = report.generated_at.strftime("%Y-%m-%d")
    json_path, md_path = _unique_report_paths(output_dir, date_str)

    payload = report.model_dump(mode="json")
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_query_markdown(report), encoding="utf-8")

    log.info("Wrote %s and %s", json_path, md_path)
    return json_path, md_path
