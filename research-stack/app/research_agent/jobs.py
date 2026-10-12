"""Background research jobs: Celery app (RabbitMQ broker, Valkey result backend) + submit/status helpers.

Layout
  * API container  - imports only the helpers below (submit_job / get_job); it never loads the ML models.
  * worker container - runs `celery -A research_agent.jobs worker`; each worker process loads the embedder and
    reranker once and reuses them for every job it executes.

States exposed to clients: pending | running | completed | failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict
from typing import Any
from urllib.parse import quote

import httpx
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

from .config import Settings, settings
from .logutil import event, job_id_var

log = logging.getLogger(__name__)
QUEUE = "research"
IDEMPOTENCY_TTL = 30 * 60

try:  # redis-py is installed in the image; keep the module importable without it for unit tests
    from redis.exceptions import ConnectionError as RedisConnectionError
    _REDIS_ERRORS: tuple[type[BaseException], ...] = (RedisConnectionError,)
except ImportError:  # pragma: no cover
    _REDIS_ERRORS = ()

# Infrastructure hiccups worth retrying. A slow LLM (ReadTimeout) or bad input is not: retrying will not help.
RETRYABLE: tuple[type[BaseException], ...] = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, ConnectionError, *_REDIS_ERRORS)


def broker_url(cfg: Settings) -> str:
    """amqp URL built from env credentials. Never log this string (it contains the password)."""
    if not cfg.rabbitmq_password:
        raise RuntimeError("RABBITMQ_DEFAULT_PASS is not set (see research-stack/.env.example)")
    return f"amqp://{quote(cfg.rabbitmq_user, safe='')}:{quote(cfg.rabbitmq_password, safe='')}@{cfg.rabbitmq_host}:5672//"


def make_celery(cfg: Settings = settings, *, broker: str | None = None, backend: str | None = None) -> Celery:
    try:
        url = broker or broker_url(cfg)
    except RuntimeError:  # import-time (e.g. docs/tests): fail on first use, not on import
        url = "memory://"
    app = Celery("research", broker=url, backend=backend or cfg.result_backend_url)
    app.conf.update(
        task_default_queue=QUEUE,
        task_serializer="json", result_serializer="json", accept_content=["json"],
        task_track_started=True,                 # exposes the "running" state
        task_acks_late=True,                     # a task lost with its worker is redelivered, not dropped
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,            # long tasks: never hoard queued jobs behind a busy worker
        result_expires=cfg.job_result_ttl,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=5,
        broker_transport_options={"confirm_publish": True},
        task_publish_retry=True,
        task_publish_retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5},
        enable_utc=True, timezone="UTC",
    )
    return app


celery_app = make_celery()

# Models are heavy (~GBs): load once per worker process, reuse for all jobs.
_models: dict[str, Any] = {}


def _shared_models(cfg: Settings) -> tuple[Any, Any]:
    if not _models:
        from .models_ml import Embedder, Reranker
        _models["embedder"] = Embedder(cfg.embed_model, cfg.device)
        _models["reranker"] = Reranker(cfg.rerank_model, cfg.device)
    return _models["embedder"], _models["reranker"]


async def execute(question: str, cfg: Settings = settings, agent_factory: Any = None) -> dict[str, Any]:
    """Run one research job end to end and return the JSON-serialisable report."""
    if agent_factory is None:
        from .agent import ResearchAgent
        embedder, reranker = _shared_models(cfg)
        agent = ResearchAgent(cfg, embedder=embedder, reranker=reranker)
    else:
        agent = agent_factory(cfg)
    await agent.startup()
    try:
        return asdict(await agent.run(question))
    finally:
        await agent.shutdown()


@celery_app.task(
    bind=True, name="research.run", acks_late=True,
    autoretry_for=RETRYABLE, max_retries=settings.job_max_retries,
    retry_backoff=10, retry_backoff_max=300, retry_jitter=True,   # 10s, 20s, ... capped at 5 min
    soft_time_limit=settings.job_soft_time_limit, time_limit=settings.job_time_limit,
)
def run_research(self, question: str) -> dict[str, Any]:
    token = job_id_var.set(self.request.id or "-")
    started = time.monotonic()
    event(log, "job_started", attempt=self.request.retries + 1, max_attempts=settings.job_max_retries + 1)
    try:
        result = asyncio.run(execute(question))
        event(log, "job_completed", seconds=round(time.monotonic() - started, 1), cited=len(result["citations"]),
              warnings=len(result.get("warnings", [])))
        return result
    except SoftTimeLimitExceeded:
        event(log, "job_timeout", level=logging.ERROR, limit_s=settings.job_soft_time_limit)
        raise RuntimeError(f"research exceeded the {settings.job_soft_time_limit}s time limit") from None
    except RETRYABLE as exc:
        event(log, "job_retry" if self.request.retries < self.max_retries else "job_failed",
              level=logging.WARNING, attempt=self.request.retries + 1, error=type(exc).__name__)
        raise  # autoretry_for schedules the retry with backoff
    except Exception as exc:
        event(log, "job_failed", level=logging.ERROR, error=type(exc).__name__)
        raise
    finally:
        job_id_var.reset(token)


# ------------------------------------------------------------------ client side (API container)

def _redis():
    import redis
    return redis.Redis.from_url(settings.result_backend_url, decode_responses=True)


def _norm(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().lower()


def _idem_key(question: str) -> str:
    return "job:idem:" + hashlib.sha256(_norm(question).encode()).hexdigest()


def _state(job_id: str) -> dict[str, Any]:
    """Blocking lookup (call via to_thread). Returns the public job document."""
    r = _redis()
    meta_raw = r.get(f"job:meta:{job_id}")
    if not meta_raw:
        return {"job_id": job_id, "status": "unknown"}
    meta = json.loads(meta_raw)
    res = celery_app.AsyncResult(job_id)
    st = res.state
    doc: dict[str, Any] = {"job_id": job_id, "question": meta["question"], "created_at": meta["created_at"]}
    if st == "SUCCESS":
        doc.update(status="completed", result=res.result)
    elif st in ("FAILURE", "REVOKED"):
        err = res.result
        doc.update(status="failed", error=(f"{type(err).__name__}: {err}"[:500] if err else st.lower()))
    elif st in ("STARTED", "RETRY"):
        doc["status"] = "running"
        if st == "RETRY":
            doc["retrying"] = True
    else:
        doc["status"] = "pending"
    return doc


def list_jobs_sync(limit: int = 50) -> list[dict[str, Any]]:
    """Recent jobs, newest first: id, question, created_at and status only (no report bodies).

    Reads the same `job:meta:*` records `_state` uses, so history lasts as long as results do (job_result_ttl).
    """
    r = _redis()
    metas: list[tuple[str, dict[str, Any]]] = []
    for key in r.scan_iter(match="job:meta:*", count=200):
        raw = r.get(key)
        if raw:
            metas.append((key.removeprefix("job:meta:"), json.loads(raw)))
    metas.sort(key=lambda m: m[1].get("created_at", ""), reverse=True)
    out = []
    for job_id, meta in metas[:limit]:
        st = celery_app.AsyncResult(job_id).state
        status = ("completed" if st == "SUCCESS" else "failed" if st in ("FAILURE", "REVOKED")
                  else "running" if st in ("STARTED", "RETRY") else "pending")
        out.append({"job_id": job_id, "question": meta["question"], "created_at": meta["created_at"], "status": status})
    return out


def submit_job_sync(question: str, *, fresh: bool = False) -> dict[str, Any]:
    """Enqueue a research job and return immediately with its id (blocking; call via to_thread).

    Idempotent: an identical question submitted within 30 minutes returns the existing job unless
    it failed or `fresh` is set.
    """
    r = _redis()
    key = _idem_key(question)
    if not fresh:
        existing = r.get(key)
        if existing:
            doc = _state(existing)
            if doc["status"] not in ("failed", "unknown"):
                return {**doc, "deduplicated": True}
    job_id = uuid.uuid4().hex
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r.set(f"job:meta:{job_id}", json.dumps({"question": question, "created_at": created}),
          ex=settings.job_result_ttl)
    r.set(key, job_id, ex=IDEMPOTENCY_TTL)
    run_research.apply_async(args=[question], task_id=job_id, queue=QUEUE)
    token = job_id_var.set(job_id)
    event(log, "job_submitted", question_chars=len(question))
    job_id_var.reset(token)
    return {"job_id": job_id, "status": "pending", "question": question, "created_at": created}


async def submit_job(question: str, *, fresh: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(submit_job_sync, question, fresh=fresh)


async def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_jobs_sync, limit)


async def get_job(job_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_state, job_id)


def broker_ok_sync() -> bool:
    try:
        with celery_app.connection_for_write() as conn:
            conn.ensure_connection(max_retries=1, timeout=3)
        return True
    except Exception:
        return False


def workers_online_sync() -> int:
    try:
        return len(celery_app.control.inspect(timeout=3.0).ping() or {})
    except Exception:
        return 0
