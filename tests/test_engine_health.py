"""Tests for research-stack/tools/engine_health.py: the per-engine canary `check()`.

SearXNG is mocked with respx, so no network access or running SearXNG is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
import respx

_TOOL = Path(__file__).resolve().parents[1] / "research-stack" / "tools" / "engine_health.py"
_spec = importlib.util.spec_from_file_location("engine_health", _TOOL)
engine_health = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(engine_health)

BASE = "http://searxng.test"


@pytest.fixture
def mock_searx():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


async def _check(engine: str, q: str) -> dict:
    async with httpx.AsyncClient(base_url=BASE, timeout=5) as client:
        return await engine_health.check(client, engine, q)


async def test_check_ok_counts_results_and_sends_engine_param(mock_searx):
    route = mock_searx.get("/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://a"}, {"url": "https://b"}], "infoboxes": [], "unresponsive_engines": []},
        )
    )
    row = await _check("arxiv", "retrieval augmented generation")

    assert row == {"engine": "arxiv", "status": 200, "results": 2, "errors": []}
    params = route.calls.last.request.url.params
    assert params["engines"] == "arxiv"
    assert params["format"] == "json"
    assert params["q"] == "retrieval augmented generation"


async def test_check_counts_infoboxes_as_results(mock_searx):
    # Wikipedia answers mostly come back as infoboxes, not results (guide section 2.2 / 12.5).
    mock_searx.get("/search").mock(
        return_value=httpx.Response(200, json={"results": [], "infoboxes": [{"infobox": "Transformer"}]})
    )
    row = await _check("wikipedia", "transformer neural network")
    assert row["results"] == 1


async def test_check_fail_when_engine_unresponsive(mock_searx):
    mock_searx.get("/search").mock(
        return_value=httpx.Response(
            200, json={"results": [], "unresponsive_engines": [["google", "access denied"]]}
        )
    )
    row = await _check("google", "python asyncio tutorial")

    assert row["status"] == 200
    assert row["results"] == 0  # main() prints FAIL for a zero count
    assert row["errors"] == [["google", "access denied"]]


async def test_check_reports_forbidden_when_json_format_disabled(mock_searx):
    # SearXNG returns 403 (HTML body) when "json" is missing from search.formats.
    mock_searx.get("/search").mock(return_value=httpx.Response(403, text="<html>Forbidden</html>"))
    row = await _check("duckduckgo", "python asyncio tutorial")

    assert row["status"] == "error"  # body is not JSON -> ValueError path
    assert row["results"] == 0
    assert row["errors"]


async def test_check_handles_connection_error(mock_searx):
    mock_searx.get("/search").mock(side_effect=httpx.ConnectError("connection refused"))
    row = await _check("bing", "python asyncio tutorial")

    assert row == {"engine": "bing", "status": "error", "results": 0, "errors": ["connection refused"]}


async def test_check_handles_timeout(mock_searx):
    mock_searx.get("/search").mock(side_effect=httpx.ReadTimeout("timed out"))
    row = await _check("pubmed", "crispr gene editing")

    assert row["status"] == "error"
    assert row["results"] == 0


def test_canaries_cover_each_guide_category():
    names = set(engine_health.CANARIES)
    # at least two engines in each of general, news, science, it (guide section 13, week 1)
    assert {"duckduckgo", "bing"} <= names
    assert {"google news", "bing news"} <= names
    assert {"arxiv", "pubmed"} <= names
    assert {"github", "stackoverflow"} <= names
