"""Report-reading service: lists, parses and serves generated reports.

Reports live in the configured output directory as
``report_YYYY-MM-DD.json`` / ``report_YYYY-MM-DD.md``. Report IDs are the
filename stems and are strictly validated — no path traversal is possible.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from api.schemas import ReportArticle, ReportCompany, ReportDetail, ReportSummary

log = logging.getLogger(__name__)

REPORT_ID_RE = re.compile(r"^report_(?P<date>\d{4}-\d{2}-\d{2})(?P<suffix>_\d+)?$")


class ReportNotFoundError(FileNotFoundError):
    pass


class MalformedReportError(ValueError):
    pass


def validate_report_id(report_id: str) -> re.Match:
    m = REPORT_ID_RE.match(report_id or "")
    if not m:
        raise ReportNotFoundError(f"Unknown report: {report_id!r}")
    return m


def _output_dir(settings) -> Path:
    return Path(settings.output_dir)


def list_reports(settings) -> list[ReportSummary]:
    out_dir = _output_dir(settings)
    summaries: list[ReportSummary] = []
    if not out_dir.is_dir():
        return summaries
    for path in sorted(out_dir.glob("report_*.json"), reverse=True):
        try:
            summaries.append(_summarize(path))
        except (MalformedReportError, OSError) as exc:
            log.warning("Skipping unreadable report %s: %s", path.name, exc)
    return summaries


def _summarize(path: Path) -> ReportSummary:
    m = validate_report_id(path.stem)
    data = _load_json(path)
    companies = data.get("companies", [])
    article_count = sum(len(c.get("articles", [])) for c in companies)
    generated_at = _parse_dt(data.get("generated_at"))
    return ReportSummary(
        id=path.stem,
        date=m.group("date"),
        generated_at=generated_at,
        company_count=len(companies),
        article_count=article_count,
        companies=[c.get("ticker", "") for c in companies if c.get("ticker")],
    )


def get_report_detail(report_id: str, settings) -> ReportDetail:
    validate_report_id(report_id)
    path = _output_dir(settings) / f"{report_id}.json"
    if not path.is_file():
        raise ReportNotFoundError(f"Unknown report: {report_id!r}")
    data = _load_json(path)
    companies = [
        ReportCompany(
            company=c.get("company", ""),
            ticker=c.get("ticker", ""),
            market_cap=c.get("market_cap"),
            candidates_considered=c.get("candidates_considered", 0),
            providers_used=c.get("providers_used", []),
            fallback_used=bool(c.get("fallback_used", False)),
            notes=c.get("notes", "") or "",
            articles=[
                ReportArticle(
                    title=a.get("title", ""),
                    url=a.get("url", ""),
                    source=a.get("source", ""),
                    published_at=_parse_dt(a.get("published_at")),
                    # Pipeline enums serialize capitalized ("High"); the API
                    # contract is lowercase.
                    impact=str(a.get("impact", "low")).lower(),
                    sentiment=str(a.get("sentiment", "neutral")).lower(),
                    confidence=float(a.get("confidence", 0.0) or 0.0),
                    reason=a.get("reason", "") or "",
                    evidence_excerpt=a.get("evidence_excerpt", "") or "",
                    source_quality=a.get("source_quality"),
                    source_quality_tier=a.get("source_quality_tier"),
                )
                for a in c.get("articles", [])
            ],
        )
        for c in data.get("companies", [])
    ]
    summary = _summarize(path)
    md_path = _output_dir(settings) / f"{report_id}.md"
    return ReportDetail(summary=summary, companies=companies,
                        has_markdown=md_path.is_file())


def report_download_path(report_id: str, fmt: str, settings) -> Path:
    """Return the on-disk path for a report download (validated)."""
    validate_report_id(report_id)
    if fmt not in ("json", "md"):
        raise ValueError(f"Unsupported format: {fmt!r}")
    path = _output_dir(settings) / f"{report_id}.{fmt}"
    # Belt and braces: resolved path must stay inside the output directory.
    out_dir = _output_dir(settings).resolve()
    if path.resolve().parent != out_dir or not path.is_file():
        raise ReportNotFoundError(f"Unknown report: {report_id!r}")
    return path


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MalformedReportError(f"Cannot parse {path.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("companies"), list):
        raise MalformedReportError(f"{path.name} is not a valid report document")
    return data


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
