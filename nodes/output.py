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
from utils.models import CompanyReport, FullReport

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


def write_reports(report: FullReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Write JSON + Markdown reports. Returns (json_path, md_path)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = report.generated_at.strftime("%Y-%m-%d")

    json_path = output_dir / f"report_{date_str}.json"
    md_path = output_dir / f"report_{date_str}.md"

    payload = report.model_dump(mode="json")
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    log.info("Wrote %s and %s", json_path, md_path)
    return json_path, md_path
