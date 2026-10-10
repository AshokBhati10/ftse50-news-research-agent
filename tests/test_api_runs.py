"""API tests: research-run lifecycle.

The real pipeline is stubbed at the ``nodes.runner`` / ``nodes.ingestion``
boundary so tests never touch live search or LLM services. What *is* real:
request validation, the single-active-run policy, cancellation, SSE event
flow, persistence/recovery, and report hand-off.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.runs import RunManager, RunStatus, _RunState
from config.settings import Settings
from utils.models import Company, CompanyReport


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    settings = Settings(
        output_dir=out,
        cache_dir=cache,
        ollama_api_key="test-key",
        ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b",
        searxng_base_url="http://localhost:8888",
    )
    # The run executor re-reads Settings() from the environment; point it
    # at the tmp settings for the whole test.
    monkeypatch.setattr("config.settings.Settings", lambda *a, **k: settings)

    async def fake_build(settings, cache):
        return [
            Company(name="AstraZeneca", ticker="AZN.L", market_cap=1.8e11),
            Company(name="HSBC Holdings", ticker="HSBA.L", market_cap=2.3e11),
        ]

    monkeypatch.setattr("nodes.ingestion.build_ftse50", fake_build)

    def fake_write_reports(full, output_dir):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        jp = output_dir / f"report_{stamp}.json"
        jp.write_text(full.model_dump_json(), encoding="utf-8")
        mp = output_dir / f"report_{stamp}.md"
        mp.write_text("# digest\n", encoding="utf-8")
        return jp, mp

    monkeypatch.setattr("nodes.output.write_reports", fake_write_reports)

    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.run_manager = RunManager(cache / "runs")
        yield client, settings, monkeypatch


def _fake_pipeline(monkeypatch, delay: float = 0.0):
    async def fake_run(companies, settings, cache, *, agentic=False,
                       on_company_complete=None, mock_articles=None):
        out = []
        for c in companies:
            if delay:
                await asyncio.sleep(delay)
            rep = CompanyReport(company=c.name, ticker=c.ticker,
                                market_cap=c.market_cap, articles=[],
                                candidates_considered=0, notes="")
            out.append(rep)
            if on_company_complete:
                on_company_complete(rep)
        return out

    monkeypatch.setattr("nodes.runner.execute_research_run", fake_run)


def _wait_for(client, run_id, terminal=True, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/research-runs/{run_id}").json()
        if terminal and body["status"] not in ("queued", "running"):
            return body
        if not terminal and body["status"] in ("running",):
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not settle in time")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_create_run_requires_llm_config(api_client):
    client, settings, _ = api_client
    settings.ollama_api_key = ""
    resp = client.post("/api/research-runs", json={})
    assert resp.status_code == 400
    assert "OLLAMA_API_KEY" in resp.json()["detail"]


def test_create_run_rejects_invalid_ticker(api_client):
    client, _, _ = api_client
    _fake_pipeline(api_client[2])
    resp = client.post("/api/research-runs", json={"tickers": ["not a ticker!!!"]})
    assert resp.status_code == 400


def test_create_run_rejects_unknown_ticker(api_client):
    client, _, _ = api_client
    _fake_pipeline(api_client[2])
    manager = client.app.state.run_manager
    manager.set_company_cache([
        Company(name="AstraZeneca", ticker="AZN.L"),
        Company(name="HSBC Holdings", ticker="HSBA.L"),
    ])
    resp = client.post("/api/research-runs", json={"tickers": ["NOPE.L"]})
    assert resp.status_code == 400
    assert "NOPE.L" in resp.json()["detail"]


def test_create_run_accepts_short_epic_and_normalizes(api_client):
    client, _, _ = api_client
    _fake_pipeline(api_client[2])
    resp = client.post("/api/research-runs", json={"tickers": ["azn"]})
    assert resp.status_code == 201, resp.text
    body = _wait_for(client, resp.json()["run_id"])
    assert body["status"] == "completed"
    assert body["total_companies"] == 1
    assert body["companies"][0]["ticker"] == "AZN.L"


# ---------------------------------------------------------------------------
# Full lifecycle with a stubbed pipeline
# ---------------------------------------------------------------------------

def test_run_completes_and_hands_off_report(api_client):
    client, settings, _ = api_client
    _fake_pipeline(api_client[2])
    resp = client.post("/api/research-runs", json={"limit_companies": 2})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]

    body = _wait_for(client, run_id)
    assert body["status"] == "completed"
    assert body["total_companies"] == 2
    assert body["completed"] + body["failed"] == 2
    assert body["report_id"] is not None
    assert body["finished_at"] is not None

    # The report the run produced is listed and readable.
    reports = client.get("/api/reports").json()
    assert any(r["id"] == body["report_id"] for r in reports["reports"])

    # History endpoint includes it.
    history = client.get("/api/research-runs").json()
    assert any(r["run_id"] == run_id for r in history["runs"])


def test_second_run_while_active_is_conflict(api_client):
    client, _, _ = api_client
    _fake_pipeline(api_client[2], delay=30.0)  # blocks the single slot
    r1 = client.post("/api/research-runs", json={})
    assert r1.status_code == 201
    _wait_for(client, r1.json()["run_id"], terminal=False)

    r2 = client.post("/api/research-runs", json={})
    assert r2.status_code == 409

    client.post(f"/api/research-runs/{r1.json()['run_id']}/cancel")
    body = _wait_for(client, r1.json()["run_id"])
    assert body["status"] == "cancelled"


def test_cancel_marks_run_cancelled(api_client):
    client, _, _ = api_client
    _fake_pipeline(api_client[2], delay=30.0)
    r1 = client.post("/api/research-runs", json={}).json()
    _wait_for(client, r1["run_id"], terminal=False)
    resp = client.post(f"/api/research-runs/{r1['run_id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_unknown_run_is_404(api_client):
    client, _, _ = api_client
    assert client.get("/api/research-runs/nope").status_code == 404
    assert client.post("/api/research-runs/nope/cancel").status_code == 404


# ---------------------------------------------------------------------------
# Persistence / recovery and SSE event flow (manager-level)
# ---------------------------------------------------------------------------

def test_interrupted_runs_recovered_on_startup(tmp_path):
    mgr = RunManager(tmp_path)
    state = _RunState(run_id="run_stale", status=RunStatus.RUNNING)
    mgr._runs["run_stale"] = state
    mgr._persist(state)

    mgr2 = RunManager(tmp_path)
    mgr2.recover()
    recovered = mgr2._runs["run_stale"]
    assert recovered.status == RunStatus.INTERRUPTED
    assert recovered.error


@pytest.mark.asyncio
async def test_events_stream_snapshot_then_terminal(tmp_path):
    mgr = RunManager(tmp_path)
    state = _RunState(run_id="run_evt")
    mgr._runs["run_evt"] = state

    async def _feed():
        await asyncio.sleep(0.05)
        state.queue.put_nowait({"type": "company_complete", "ticker": "AZN.L"})
        await asyncio.sleep(0.05)
        state.queue.put_nowait({"type": "completed", "run": {}})

    feed = asyncio.create_task(_feed())
    seen = [e async for e in mgr.events("run_evt")]
    await feed
    assert seen[0]["type"] == "snapshot"
    assert seen[1]["type"] == "company_complete"
    assert seen[-1]["type"] == "completed"


def test_run_state_persisted_to_disk(api_client):
    client, settings, _ = api_client
    _fake_pipeline(api_client[2])
    run_id = client.post("/api/research-runs", json={}).json()["run_id"]
    _wait_for(client, run_id)
    persisted = json.loads((settings.cache_dir / "runs" / f"{run_id}.json").read_text())
    assert persisted["status"] == "completed"
    assert "queue" not in persisted and "task" not in persisted
