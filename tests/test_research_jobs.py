"""Phase 3: Celery/RabbitMQ background jobs - lifecycle, retries, idempotency, API, secrets (research-stack).

The broker and result backend are replaced by in-memory equivalents and Celery runs tasks eagerly, so these
tests need no RabbitMQ/Valkey; the live stack is verified separately.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research-stack" / "app"))

from research_agent import jobs, main  # noqa: E402
from research_agent.config import Settings  # noqa: E402

REPORT = {"question": "q", "answer_markdown": "A [1]", "citations": [{"number": 1}], "warnings": []}


class FakeRedis:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, ex=None):
        self.d[k] = v

    def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        return iter([k for k in list(self.d) if k.startswith(prefix)])


@pytest.fixture(autouse=True)
def eager(monkeypatch):
    jobs.celery_app.conf.update(task_always_eager=True, task_eager_propagates=False, task_store_eager_result=True,
                                result_backend="cache+memory://", broker_url="memory://")
    jobs.celery_app._local.__dict__.pop("backend", None)  # rebuild the backend from the conf above
    fake = FakeRedis()
    monkeypatch.setattr(jobs, "_redis", lambda: fake)
    return fake


def patch_execute(monkeypatch, fn):
    calls = []

    async def _exec(question, cfg=None, agent_factory=None):
        calls.append(question)
        return await fn(len(calls), question)
    monkeypatch.setattr(jobs, "execute", _exec)
    return calls


@pytest.fixture
def api(monkeypatch):
    class _Cache:
        def __init__(self, *_a): ...
        async def ping(self): return True
        async def close(self): ...

    class _Searx:
        def __init__(self, *_a, **_k): ...
        async def healthy(self): return True
        async def close(self): ...
    monkeypatch.setattr(main, "Cache", _Cache)
    monkeypatch.setattr(main, "SearxClient", _Searx)
    with TestClient(main.app) as c:
        yield c


# ------------------------------------------------------------------ lifecycle through the API

def test_submit_returns_a_job_id_promptly_without_waiting_for_the_run(api, monkeypatch):
    monkeypatch.setattr(jobs.run_research, "apply_async", lambda **_k: None)  # worker not running
    r = api.post("/research/jobs", json={"question": "What is X?"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending" and len(body["job_id"]) == 32
    st = api.get(f"/research/jobs/{body['job_id']}")
    assert st.status_code == 200 and st.json()["status"] == "pending" and "result" not in st.json()
    assert api.get(f"/research/jobs/{body['job_id']}/result").status_code == 409


def test_completed_job_exposes_status_and_result(api, monkeypatch):
    async def ok(n, q):
        return REPORT
    patch_execute(monkeypatch, ok)
    job_id = api.post("/research/jobs", json={"question": "What is X?"}).json()["job_id"]
    doc = api.get(f"/research/jobs/{job_id}").json()
    assert doc["status"] == "completed" and doc["result"]["answer_markdown"] == "A [1]"
    assert api.get(f"/research/jobs/{job_id}/result").json() == REPORT


def test_history_lists_jobs_newest_first_without_report_bodies(api, monkeypatch):
    async def ok(n, q):
        return REPORT
    patch_execute(monkeypatch, ok)
    first = api.post("/research/jobs", json={"question": "First question?"}).json()["job_id"]
    jobs._redis().set(f"job:meta:{first}", '{"question": "First question?", "created_at": "2026-01-01T00:00:00Z"}')
    second = api.post("/research/jobs", json={"question": "Second question?"}).json()["job_id"]
    rows = api.get("/research/jobs").json()
    assert [r["job_id"] for r in rows] == [second, first]
    assert rows[0]["question"] == "Second question?" and rows[0]["status"] == "completed"
    assert all("result" not in r for r in rows)


def test_failed_job_reports_error(api, monkeypatch):
    async def bad(n, q):
        raise ValueError("synthesis exploded")
    patch_execute(monkeypatch, bad)
    job_id = api.post("/research/jobs", json={"question": "What is X?"}).json()["job_id"]
    doc = api.get(f"/research/jobs/{job_id}").json()
    assert doc["status"] == "failed" and "synthesis exploded" in doc["error"]
    r = api.get(f"/research/jobs/{job_id}/result")
    assert r.status_code == 500 and "synthesis exploded" in r.json()["detail"]


def test_running_state_is_reported(api, monkeypatch):
    monkeypatch.setattr(jobs.run_research, "apply_async", lambda **_k: None)
    job_id = api.post("/research/jobs", json={"question": "What is X?"}).json()["job_id"]
    jobs.celery_app.backend.store_result(job_id, None, "STARTED")
    assert api.get(f"/research/jobs/{job_id}").json()["status"] == "running"
    jobs.celery_app.backend.store_result(job_id, None, "RETRY")
    doc = api.get(f"/research/jobs/{job_id}").json()
    assert doc["status"] == "running" and doc["retrying"] is True


def test_unknown_job_is_404_and_bad_input_is_422(api):
    assert api.get("/research/jobs/" + "0" * 32).status_code == 404
    assert api.post("/research/jobs", json={"question": "x"}).status_code == 422
    assert api.post("/research/jobs", json={}).status_code == 422


def test_broker_outage_returns_503_not_a_hang(api, monkeypatch):
    def down(**_k):
        raise ConnectionError("broker down")
    monkeypatch.setattr(jobs.run_research, "apply_async", down)
    r = api.post("/research/jobs", json={"question": "What is X?"})
    assert r.status_code == 503 and "queue unavailable" in r.json()["detail"]


def test_legacy_blocking_endpoint_still_returns_the_report(api, monkeypatch):
    async def ok(n, q):
        return REPORT
    patch_execute(monkeypatch, ok)
    r = api.post("/research", json={"question": "What is X?"})
    assert r.status_code == 200 and r.json() == REPORT


def test_legacy_endpoint_returns_202_with_job_id_when_wait_budget_is_exhausted(api, monkeypatch):
    monkeypatch.setattr(jobs.run_research, "apply_async", lambda **_k: None)
    monkeypatch.setattr(main.settings.__class__, "sync_wait_seconds", 0, raising=False)
    monkeypatch.setattr(main, "settings", Settings(sync_wait_seconds=0))
    r = api.post("/research", json={"question": "What is X?"})
    assert r.status_code == 202 and r.json()["status"] == "pending" and r.json()["job_id"]


def test_healthz_reports_all_dependencies(api, monkeypatch):
    monkeypatch.setattr(jobs, "broker_ok_sync", lambda: True)
    monkeypatch.setattr(jobs, "workers_online_sync", lambda: 1)
    assert api.get("/healthz").json() == {"searxng": True, "valkey": True, "rabbitmq": True, "workers": 1}


# ------------------------------------------------------------------ idempotency

def test_identical_question_is_deduplicated_unless_failed_or_fresh(api, monkeypatch):
    monkeypatch.setattr(jobs.run_research, "apply_async", lambda **_k: None)
    first = api.post("/research/jobs", json={"question": "What is  X?"}).json()
    again = api.post("/research/jobs", json={"question": "what is x?"}).json()
    assert again["job_id"] == first["job_id"] and again["deduplicated"] is True
    fresh = api.post("/research/jobs", json={"question": "What is X?", "fresh": True}).json()
    assert fresh["job_id"] != first["job_id"]
    jobs.celery_app.backend.store_result(fresh["job_id"], ValueError("x"), "FAILURE")
    retry = api.post("/research/jobs", json={"question": "What is X?"}).json()
    assert retry["job_id"] != fresh["job_id"]  # failed jobs are not reused


# ------------------------------------------------------------------ retries / limits

def test_transient_errors_are_retried_then_succeed(monkeypatch):
    async def flaky(n, q):
        if n < 3:
            raise httpx.ConnectError("ollama restarting")
        return REPORT
    calls = patch_execute(monkeypatch, flaky)
    res = jobs.run_research.apply(args=["q"])
    assert res.get() == REPORT and len(calls) == 3


def test_retries_are_bounded(monkeypatch):
    async def always(n, q):
        raise httpx.ConnectError("down")
    calls = patch_execute(monkeypatch, always)
    res = jobs.run_research.apply(args=["q"])
    assert res.failed() and len(calls) == jobs.run_research.max_retries + 1


def test_non_transient_errors_are_not_retried(monkeypatch):
    async def bad(n, q):
        raise ValueError("bad input")
    calls = patch_execute(monkeypatch, bad)
    assert jobs.run_research.apply(args=["q"]).failed() and len(calls) == 1


def test_task_is_configured_for_reliability():
    t = jobs.run_research
    assert t.acks_late and t.max_retries == jobs.settings.job_max_retries
    assert t.retry_backoff and t.retry_jitter and t.retry_backoff_max == 300
    assert 0 < t.soft_time_limit < t.time_limit
    c = jobs.celery_app.conf
    assert c.task_reject_on_worker_lost and c.worker_prefetch_multiplier == 1 and c.task_track_started
    assert c.task_default_queue == "research" and c.result_expires == jobs.settings.job_result_ttl


def test_job_logs_are_structured_with_job_id(monkeypatch, caplog):
    async def ok(n, q):
        return REPORT
    patch_execute(monkeypatch, ok)
    with caplog.at_level(logging.INFO, logger="research_agent.jobs"):
        jobs.run_research.apply(args=["q"], task_id="abc123")
    lines = [r.getMessage() for r in caplog.records]
    assert any("event=job_started job_id=abc123" in m for m in lines)
    assert any("event=job_completed job_id=abc123" in m for m in lines)


# ------------------------------------------------------------------ secrets

def test_broker_url_requires_a_password_and_never_appears_in_settings_repr():
    with pytest.raises(RuntimeError):
        jobs.broker_url(Settings(rabbitmq_password=""))
    cfg = Settings(rabbitmq_user="research", rabbitmq_password="p@ss/w:rd")
    assert jobs.broker_url(cfg) == "amqp://research:p%40ss%2Fw%3Ard@rabbitmq:5672//"
    assert "p@ss" not in repr(cfg)


def test_compose_does_not_publish_rabbitmq_or_embed_credentials():
    import yaml
    compose = Path(__file__).resolve().parents[1] / "research-stack" / "docker-compose.yml"
    text = compose.read_text()
    cfg = yaml.safe_load(text)
    rabbit = cfg["services"]["rabbitmq"]
    assert "ports" not in rabbit and "management" not in rabbit["image"]
    assert rabbit["env_file"] == ".env.rabbitmq" and "PASS" not in str(rabbit.get("environment", ""))
    assert "RABBITMQ_DEFAULT_PASS" not in text
    assert cfg["services"]["worker"]["command"].count("--concurrency") == 1
    assert cfg["services"]["rabbitmq"]["healthcheck"] and cfg["services"]["worker"]["healthcheck"]
