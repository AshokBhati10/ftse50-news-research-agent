"""API tests: health, settings, companies, reports.

No live external services are touched: the company-ingestion function is
stubbed, and all file I/O goes to tmp directories.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.runs import RunManager
from config.settings import Settings
from utils.models import Company


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    settings = Settings(
        output_dir=out,
        cache_dir=cache,
        ollama_api_key="test-key-DO-NOT-LEAK",
        ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b",
        searxng_base_url="http://127.0.0.1:9",  # unroutable -> probe fails fast
    )
    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.run_manager = RunManager(cache / "runs")
        monkeypatch.setattr(
            "api.services.health.check_searxng",
            lambda s: {"ok": False, "detail": "stubbed"},
        )
        yield client, settings


def _companies():
    return [
        Company(name="AstraZeneca", ticker="AZN.L", market_cap=1.8e11),
        Company(name="HSBC Holdings", ticker="HSBA.L", market_cap=2.3e11),
    ]


# ---------------------------------------------------------------------------
# Health / settings — including secret-leak guards
# ---------------------------------------------------------------------------

def test_health_shape(api_client):
    client, _ = api_client
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body) >= {"searxng", "ollama", "you_fallback", "cache"}
    assert body["ollama"]["ok"] is True  # key present in test settings


def test_health_never_leaks_api_key(api_client):
    client, _ = api_client
    resp = client.get("/api/health")
    assert "test-key-DO-NOT-LEAK" not in resp.text


def test_settings_status_hides_secrets(api_client):
    client, _ = api_client
    resp = client.get("/api/settings/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama_configured"] is True
    assert body["ollama_model"] == "qwen3:8b"
    assert body["ollama_host"] == "ollama.example.com"
    assert "test-key-DO-NOT-LEAK" not in resp.text
    # Warning texts may name the *variable* (e.g. YOU_API_KEY) so operators
    # know what to set — but must never carry a value.
    assert "DO-NOT-LEAK" not in resp.text


def test_settings_status_warns_when_llm_missing(tmp_path):
    settings = Settings(output_dir=tmp_path, cache_dir=tmp_path,
                        ollama_api_key="", searxng_base_url="")
    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.run_manager = RunManager(tmp_path / "runs")
        body = client.get("/api/settings/status").json()
    assert body["ollama_configured"] is False
    assert any("OLLAMA_API_KEY" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def test_companies_uses_backend_universe(api_client, monkeypatch):
    client, _ = api_client

    async def fake_build(settings, cache):
        return _companies()

    monkeypatch.setattr("nodes.ingestion.build_ftse50", fake_build)
    resp = client.get("/api/companies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["companies"][0]["ticker"] == "AZN.L"
    assert body["companies"][1]["name"] == "HSBC Holdings"


def test_companies_ingestion_failure_is_502(api_client, monkeypatch):
    client, _ = api_client

    async def boom(settings, cache):
        raise RuntimeError("wikipedia down")

    monkeypatch.setattr("nodes.ingestion.build_ftse50", boom)
    resp = client.get("/api/companies")
    assert resp.status_code == 502
    assert "traceback" not in resp.text.lower()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _write_report(out_dir, name="report_2026-10-10"):
    doc = {
        "generated_at": datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc).isoformat(),
        "companies": [
            {
                "company": "AstraZeneca", "ticker": "AZN.L", "market_cap": 1.8e11,
                "candidates_considered": 4, "providers_used": ["mock"],
                "fallback_used": False, "notes": "",
                "articles": [
                    {
                        "title": "Trial win", "url": "https://example.com/a",
                        "source": "example.com",
                        "published_at": "2026-10-09T10:00:00+00:00",
                        "impact": "high", "sentiment": "positive", "confidence": 0.9,
                        "reason": "Material readout.", "evidence_excerpt": "Excerpt…",
                        "source_quality": 0.9, "source_quality_tier": "tier_1",
                    }
                ],
            }
        ],
    }
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    (out_dir / f"{name}.md").write_text("# digest\n", encoding="utf-8")
    return p


def test_reports_empty(api_client):
    client, _ = api_client
    body = client.get("/api/reports").json()
    assert body == {"count": 0, "reports": []}


def test_reports_list_and_detail(api_client):
    client, settings = api_client
    _write_report(settings.output_dir)
    body = client.get("/api/reports").json()
    assert body["count"] == 1
    summary = body["reports"][0]
    assert summary["id"] == "report_2026-10-10"
    assert summary["company_count"] == 1
    assert summary["article_count"] == 1

    detail = client.get("/api/reports/report_2026-10-10").json()
    assert detail["has_markdown"] is True
    article = detail["companies"][0]["articles"][0]
    assert article["title"] == "Trial win"
    assert article["impact"] == "high"
    assert article["source_quality_tier"] == "tier_1"


def test_reports_malformed_are_skipped_not_fatal(api_client):
    client, settings = api_client
    (settings.output_dir / "report_2026-10-09.json").write_text("{oops", encoding="utf-8")
    _write_report(settings.output_dir)
    body = client.get("/api/reports").json()
    assert body["count"] == 1  # malformed one skipped
    resp = client.get("/api/reports/report_2026-10-09")
    assert resp.status_code == 502


def test_report_unknown_id_is_404(api_client):
    client, _ = api_client
    assert client.get("/api/reports/report_1999-01-01").status_code == 404


def test_report_id_path_traversal_is_404(api_client):
    client, _ = api_client
    assert client.get("/api/reports/..%2F..%2Fsecret").status_code == 404
    assert client.get("/api/reports/report_2026-10-10%2F..%2F..").status_code == 404


def test_report_download_json_and_md(api_client):
    client, settings = api_client
    _write_report(settings.output_dir)
    r = client.get("/api/reports/report_2026-10-10/download?format=json")
    assert r.status_code == 200
    assert r.json()["companies"][0]["ticker"] == "AZN.L"
    r = client.get("/api/reports/report_2026-10-10/download?format=md")
    assert r.status_code == 200
    assert r.text.startswith("# digest")
    assert client.get("/api/reports/report_2026-10-10/download?format=xml").status_code == 422
    assert client.get("/api/reports/report_2000-01-01/download").status_code == 404


def test_latest_report_404_when_none(api_client):
    client, _ = api_client
    assert client.get("/api/reports/latest").status_code == 404


def test_reports_with_numeric_suffix_are_listed(api_client):
    client, settings = api_client
    _write_report(settings.output_dir, name="report_2026-10-10_2")
    body = client.get("/api/reports").json()
    ids = [r["id"] for r in body["reports"]]
    assert "report_2026-10-10_2" in ids
    summary = next(r for r in body["reports"] if r["id"] == "report_2026-10-10_2")
    assert summary["date"] == "2026-10-10"
    detail = client.get("/api/reports/report_2026-10-10_2").json()
    assert detail["companies"][0]["articles"][0]["impact"] == "high"
    assert detail["companies"][0]["articles"][0]["sentiment"] == "positive"
