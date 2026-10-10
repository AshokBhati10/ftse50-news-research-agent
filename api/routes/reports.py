from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from api.schemas import ReportDetail, ReportsResponse, ReportSummary
from api.services.reports import (
    MalformedReportError,
    ReportNotFoundError,
    get_report_detail,
    list_reports,
    report_download_path,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["reports"])


@router.get("/reports", response_model=ReportsResponse)
def get_reports(request: Request) -> ReportsResponse:
    settings = request.app.state.settings
    reports = list_reports(settings)
    return ReportsResponse(count=len(reports), reports=reports)


@router.get("/reports/latest", response_model=ReportDetail)
def get_latest_report(request: Request) -> ReportDetail:
    settings = request.app.state.settings
    reports = list_reports(settings)
    if not reports:
        raise HTTPException(status_code=404, detail="No research reports found yet.")
    return _detail_or_502(reports[0].id, settings)


@router.get("/reports/{report_id}", response_model=ReportDetail)
def get_report(report_id: str, request: Request) -> ReportDetail:
    return _detail_or_502(report_id, request.app.state.settings)


@router.get("/reports/{report_id}/download")
def download_report(
    report_id: str,
    request: Request,
    format: str = Query(default="json", pattern="^(json|md)$"),
) -> FileResponse:
    settings = request.app.state.settings
    try:
        path = report_download_path(report_id, format, settings)
    except ReportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media = "application/json" if format == "json" else "text/markdown"
    return FileResponse(path, media_type=media, filename=path.name)


def _detail_or_502(report_id: str, settings) -> ReportDetail:
    try:
        return get_report_detail(report_id, settings)
    except ReportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MalformedReportError as exc:
        log.warning("Malformed report requested: %s", exc)
        raise HTTPException(status_code=502,
                            detail=f"Report {report_id!r} exists but could not be parsed.") from exc
