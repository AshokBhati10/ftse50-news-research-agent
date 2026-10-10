"""Research-run orchestration for the API.

A run executes the *real* pipeline (``nodes.runner.execute_research_run`` —
the same function the CLI uses) as a background asyncio task. Only one run
may be active at a time, which prevents concurrent runs from corrupting
report files or the shared cache.

Runs survive a browser refresh (state is persisted to disk on every
transition) and stream live progress over SSE. Cancellation is cooperative
via ``asyncio.Task.cancel()``; companies already finished keep their
results as honest partial output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.schemas import (
    CompanyProgress,
    ResearchRunCreate,
    ResearchRunOut,
    RunStatus,
)

log = logging.getLogger(__name__)

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]*\.L$")
TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}
ACTIVE = {RunStatus.QUEUED, RunStatus.RUNNING}


class RunConflictError(Exception):
    """Another research run is already active."""


class RunNotFoundError(Exception):
    pass


class InvalidRunRequestError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_ticker(raw: str) -> str:
    t = raw.strip().upper()
    if t and "." not in t:
        t = f"{t}.L"  # allow short EPICs, same as the CLI
    if not TICKER_RE.match(t):
        raise InvalidRunRequestError(f"Invalid ticker: {raw!r}")
    return t


@dataclass
class _RunState:
    run_id: str
    status: RunStatus = RunStatus.QUEUED
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_companies: int = 0
    completed: int = 0
    failed: int = 0
    current_company: str | None = None
    articles_found: int = 0
    error: str | None = None
    report_id: str | None = None
    companies: list[dict[str, Any]] = field(default_factory=list)
    request: dict[str, Any] = field(default_factory=dict)
    # Query-driven research:
    mode: str = "companies"              # "companies" | "query"
    query: str | None = None
    stage: str | None = None            # current pipeline stage (query mode)
    stats: dict[str, Any] = field(default_factory=dict)
    # Runtime-only (never persisted):
    queue: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_out(self) -> ResearchRunOut:
        return ResearchRunOut(
            run_id=self.run_id,
            status=self.status,
            mode=self.mode,
            query=self.query,
            stage=self.stage,
            stats=self.stats,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            total_companies=self.total_companies,
            completed=self.completed,
            failed=self.failed,
            current_company=self.current_company,
            articles_found=self.articles_found,
            error=self.error,
            report_id=self.report_id,
            companies=[CompanyProgress(**c) for c in self.companies],
            request=self.request,
        )

    def to_persisted(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total_companies": self.total_companies,
            "completed": self.completed,
            "failed": self.failed,
            "current_company": self.current_company,
            "articles_found": self.articles_found,
            "error": self.error,
            "report_id": self.report_id,
            "companies": self.companies,
            "request": self.request,
            "mode": self.mode,
            "query": self.query,
            "stage": self.stage,
            "stats": self.stats,
        }
        return d

    @classmethod
    def from_persisted(cls, data: dict[str, Any]) -> "_RunState":
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        return cls(
            run_id=data["run_id"],
            status=RunStatus(data["status"]),
            created_at=_dt(data["created_at"]) or utcnow(),
            started_at=_dt(data.get("started_at")),
            finished_at=_dt(data.get("finished_at")),
            total_companies=data.get("total_companies", 0),
            completed=data.get("completed", 0),
            failed=data.get("failed", 0),
            current_company=data.get("current_company"),
            articles_found=data.get("articles_found", 0),
            error=data.get("error"),
            report_id=data.get("report_id"),
            companies=data.get("companies", []),
            request=data.get("request", {}),
            mode=data.get("mode", "companies"),
            query=data.get("query"),
            stage=data.get("stage"),
            stats=data.get("stats", {}),
        )


class RunManager:
    def __init__(self, runs_dir: Path):
        self._runs_dir = runs_dir
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, _RunState] = {}
        self._lock = asyncio.Lock()
        self._company_cache: list | None = None  # warmed by lifespan / companies endpoint

    # -- startup / recovery -------------------------------------------------
    def recover(self) -> None:
        """Mark runs left active by a previous process as interrupted."""
        for path in sorted(self._runs_dir.glob("run_*.json")):
            try:
                state = _RunState.from_persisted(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError) as exc:
                log.warning("Ignoring unreadable run file %s: %s", path.name, exc)
                continue
            if state.status in ACTIVE:
                state.status = RunStatus.INTERRUPTED
                state.finished_at = utcnow()
                state.error = "API restarted while this run was active."
                self._persist(state)
            self._runs[state.run_id] = state

    # -- company universe (for ticker validation) ---------------------------
    def set_company_cache(self, companies: list) -> None:
        self._company_cache = companies

    def _known_tickers(self) -> set[str] | None:
        if self._company_cache is None:
            return None
        return {c.ticker.upper() for c in self._company_cache}

    # -- CRUD ---------------------------------------------------------------
    async def create_run(self, req: ResearchRunCreate, settings) -> _RunState:
        async with self._lock:
            for state in self._runs.values():
                if state.status in ACTIVE:
                    raise RunConflictError(
                        f"Run {state.run_id} is already {state.status.value}; "
                        "wait for it to finish or cancel it first."
                    )
            if not settings.ollama_api_key:
                raise InvalidRunRequestError(
                    "LLM is not configured (OLLAMA_API_KEY is missing). "
                    "Live research runs need an Ollama-compatible endpoint; "
                    "see Settings for details."
                )
            mode = "query" if req.query else "companies"
            tickers = None
            if req.tickers:
                tickers = [normalize_ticker(t) for t in req.tickers]
                known = self._known_tickers()
                if known is not None:
                    unknown = [t for t in tickers if t not in known]
                    if unknown:
                        raise InvalidRunRequestError(
                            f"Unknown ticker(s): {', '.join(unknown)}. "
                            "Tickers must come from the FTSE 50 company list."
                        )
            run_id = f"run_{utcnow().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
            state = _RunState(
                run_id=run_id,
                mode=mode,
                query=req.query,
                request={
                    "query": req.query,
                    "max_results": req.max_results or 20,
                    "time_range": req.time_range or "month",
                    "fresh_search": req.fresh_search,
                    "tickers": tickers,
                    "limit_companies": req.limit_companies,
                    "agentic": req.agentic,
                    "no_cache": req.no_cache,
                },
            )
            self._runs[run_id] = state
            self._persist(state)
            self._emit(state, {"type": "created", "run": state.to_out().model_dump(mode="json")})
            # Fire and forget: the task owns its lifecycle from here.
            state.task = asyncio.create_task(self._execute(state, settings),
                                             name=f"research-{run_id}")
            return state

    async def get(self, run_id: str) -> _RunState:
        state = self._runs.get(run_id)
        if state is None:
            raise RunNotFoundError(run_id)
        return state

    def list_runs(self, limit: int = 20) -> list[ResearchRunOut]:
        ordered = sorted(self._runs.values(), key=lambda s: s.created_at, reverse=True)
        return [s.to_out() for s in ordered[:limit]]

    async def cancel(self, run_id: str) -> _RunState:
        state = await self.get(run_id)
        if state.status not in ACTIVE:
            return state
        # Mark first so a racing _execute completion cannot overwrite.
        state.status = RunStatus.CANCELLED
        state.finished_at = utcnow()
        if state.task and not state.task.done():
            state.task.cancel()
        self._persist(state)
        self._emit(state, {"type": "cancelled", "run": state.to_out().model_dump(mode="json")})
        return state

    # -- SSE ----------------------------------------------------------------
    async def events(self, run_id: str):
        """Yield SSE-ready event dicts until the run reaches a terminal state."""
        state = await self.get(run_id)
        yield {"type": "snapshot", "run": state.to_out().model_dump(mode="json")}
        if state.status in TERMINAL:
            return
        while True:
            try:
                event = await asyncio.wait_for(state.queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                yield {"type": "ping"}
                continue
            yield event
            if event.get("type") in ("completed", "failed", "cancelled", "interrupted"):
                return

    # -- execution ----------------------------------------------------------
    async def _execute(self, state: _RunState, settings) -> None:
        from config.settings import Settings
        from utils.cache import CacheManager

        req = state.request
        try:
            state.status = RunStatus.RUNNING
            state.started_at = utcnow()
            self._persist(state)
            self._emit(state, {"type": "started", "run": state.to_out().model_dump(mode="json")})

            live_settings = Settings()  # fresh read of env/.env
            cache = CacheManager(live_settings.cache_dir,
                                 enabled=not req.get("no_cache"))

            if state.mode == "query":
                await self._execute_query(state, live_settings, cache)
            else:
                await self._execute_companies(state, live_settings, cache)
        except asyncio.CancelledError:
            # cancel() already set status/persisted/emitted; just exit quietly.
            if state.status not in TERMINAL:
                state.status = RunStatus.CANCELLED
                state.finished_at = utcnow()
                self._persist(state)
                self._emit(state, {"type": "cancelled",
                                   "run": state.to_out().model_dump(mode="json")})
            raise
        except Exception as exc:  # noqa: BLE001 — report failures honestly, never leak traces
            log.exception("Run %s failed", state.run_id)
            if state.status == RunStatus.CANCELLED:
                return  # a concurrent cancel won the race; keep its verdict
            state.status = RunStatus.FAILED
            state.finished_at = utcnow()
            state.error = str(exc) or "Research run failed."
            self._persist(state)
            self._emit(state, {"type": "failed", "run": state.to_out().model_dump(mode="json")})

    async def _execute_query(self, state: _RunState, settings, cache) -> None:
        """Query-driven research run with honest stage-level progress."""
        from nodes.output import write_query_reports
        from nodes.runner import execute_query_research

        req = state.request

        def _on_stage(stage: str, info: dict) -> None:
            state.stage = stage
            state.stats = {**state.stats, stage: info}
            if stage == "analyze":
                state.articles_found = info.get("findings", 0)
            self._persist(state)
            self._emit(state, {"type": "stage", "stage": stage,
                               "run": state.to_out().model_dump(mode="json")})

        report = await execute_query_research(
            state.query or "",
            settings,
            cache,
            run_id=state.run_id,
            max_results=int(req.get("max_results") or 20),
            time_range=req.get("time_range") or "month",
            fresh=bool(req.get("fresh_search")),
            on_stage=_on_stage,
        )

        json_path, _md_path = write_query_reports(report, settings.output_dir)
        if state.status == RunStatus.CANCELLED:
            return  # a concurrent cancel won the race; keep its verdict
        state.status = RunStatus.COMPLETED
        state.finished_at = utcnow()
        state.report_id = json_path.stem
        state.stage = "done"
        state.stats = {**state.stats, **report.counts,
                       "warnings": report.warnings,
                       "from_cache": report.search_config.get("from_cache"),
                       "providers": report.search_config.get("providers_used")}
        state.articles_found = len(report.findings)
        self._persist(state)
        self._emit(state, {"type": "completed", "run": state.to_out().model_dump(mode="json")})
        log.info("Query run %s completed: %d findings, report %s",
                 state.run_id, len(report.findings), state.report_id)

    async def _execute_companies(self, state: _RunState, settings, cache) -> None:
        """Legacy FTSE 50 company research run (unchanged behavior)."""
        from nodes.ingestion import build_ftse50
        from nodes.output import build_full_report, write_reports
        from nodes.runner import execute_research_run

        req = state.request
        companies = await build_ftse50(settings, cache)
        self.set_company_cache(companies)
        wanted = req.get("tickers")
        if wanted:
            wanted_set = set(wanted)
            companies = [c for c in companies if c.ticker.upper() in wanted_set]
        if req.get("limit_companies"):
            companies = companies[: req["limit_companies"]]
        if not companies:
            raise InvalidRunRequestError("No companies matched the request.")

        state.total_companies = len(companies)
        self._persist(state)

        def _on_company_complete(report) -> None:
            state.current_company = report.ticker
            ok = bool(report.articles)
            if ok:
                state.completed += 1
            else:
                state.failed += 1
            state.articles_found += len(report.articles)
            state.companies.append({
                "ticker": report.ticker,
                "status": "completed" if ok else "failed",
                "articles": len(report.articles),
                "notes": (report.notes or "")[:300],
            })
            self._persist(state)
            self._emit(state, {
                "type": "company_complete",
                "ticker": report.ticker,
                "completed": state.completed,
                "failed": state.failed,
                "total": state.total_companies,
                "articles_found": state.articles_found,
            })

        reports = await execute_research_run(
            companies, settings, cache,
            agentic=bool(req.get("agentic")),
            on_company_complete=_on_company_complete,
        )

        full = build_full_report(reports, settings)
        json_path, _md_path = write_reports(full, settings.output_dir)
        # If the operator cancelled mid-flight, _execute was aborted above;
        # reaching here means a genuine finish.
        if state.status == RunStatus.CANCELLED:
            return
        state.status = RunStatus.COMPLETED
        state.finished_at = utcnow()
        state.report_id = json_path.stem
        state.current_company = None
        self._persist(state)
        self._emit(state, {"type": "completed", "run": state.to_out().model_dump(mode="json")})
        log.info("Run %s completed: %d companies, report %s",
                 state.run_id, len(reports), state.report_id)

    # -- helpers ------------------------------------------------------------
    def _emit(self, state: _RunState, event: dict) -> None:
        try:
            state.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def _persist(self, state: _RunState) -> None:
        try:
            path = self._runs_dir / f"{state.run_id}.json"
            path.write_text(json.dumps(state.to_persisted(), indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not persist run %s: %s", state.run_id, exc)
