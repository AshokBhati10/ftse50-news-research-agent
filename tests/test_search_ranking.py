"""Phase 1.1 — search result relevance, dedup, diversity and recency.

`agent.tools.filter_and_rank` is a pure function: these tests exercise the real
ranking logic (source quality, query overlap, recency, homepage/section
penalties, story de-duplication) with no network or model calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.tools import SearchTool, WebSearchArgs, filter_and_rank
from config.settings import Settings
from utils.models import Article
from utils.source_quality import is_section_page


def _art(title: str, url: str, *, snippet: str = "some snippet here",
         quality: float = 0.5, published: datetime | None = None,
         content: str | None = None) -> Article:
    return Article(company="", ticker="", title=title, url=url,
                   snippet=snippet, source_quality=quality,
                   published_at=published, content=content)


def _days_ago(n: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


# ---------------------------------------------------------------------------
# is_section_page
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Press releases | AstraZeneca", True),
    ("Annual reports", True),
    ("Investor relations", True),
    ("AstraZeneca to buy Summit Therapeutics in $2bn deal", False),
    ("Company X reports quarterly results", False),
])
def test_is_section_page_detection(title, expected):
    assert is_section_page(title) is expected


# ---------------------------------------------------------------------------
# Story de-duplication
# ---------------------------------------------------------------------------

def test_collapses_syndicated_stories_in_search_batch():
    headline = "AstraZeneca to buy Summit Therapeutics in $2bn deal"
    # The highest-quality copy is deliberately LAST in the input so that a
    # dedup-before-sort regression (which would keep the first copy) is caught.
    arts = [
        _art(headline, "https://news.example/x", quality=0.6),
        _art(headline, "https://blog.example/x", quality=0.4),
        _art(headline, "https://reuters.com/x", quality=0.8),
        _art("AstraZeneca wins FDA approval for new drug",
             "https://ft.com/y", quality=0.7),
    ]
    ranked = filter_and_rank(arts, "AstraZeneca latest developments",
                             max_results=10)
    urls = [a.url for a in ranked]
    assert len(urls) == 2  # three copies of one story collapse to one
    assert "https://reuters.com/x" in urls  # highest-quality copy survives
    assert "https://ft.com/y" in urls


def test_dedup_then_cap_keeps_one_copy_plus_distinct_story():
    # Without dedup the two highest-quality copies of the SAME story would
    # consume both slots; dedup must free a slot for the distinct story.
    shared = "Big pharma acquisition announcement headline"
    arts = [
        _art(shared, "https://reuters.com/x", quality=0.8),
        _art(shared, "https://ft.com/x", quality=0.8),
        _art(shared, "https://bbc.com/x", quality=0.8),
        _art("Company Y reports quarterly results", "https://example.com/r",
             quality=0.4),
    ]
    ranked = filter_and_rank(arts, "recent business news", max_results=2)
    urls = [a.url for a in ranked]
    assert len(urls) == 2
    # Exactly one copy of the duplicated story survives...
    assert len([u for u in urls if u.endswith("/x")]) == 1
    # ...and the distinct story occupies the other slot.
    assert "https://example.com/r" in urls


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

def test_recent_dated_result_outranks_undated_equivalent():
    undated = _art("Company X reports results", "https://a.example/1",
                   snippet="reports results quarterly", quality=0.6)
    recent = _art("Company X reports results", "https://b.example/2",
                  snippet="reports results quarterly", quality=0.6,
                  published=_days_ago(1))
    ranked = filter_and_rank([undated, recent], "Company X reports results",
                             max_results=2)
    assert [a.url for a in ranked] == ["https://b.example/2",
                                       "https://a.example/1"]


def test_timezone_naive_date_does_not_crash_and_is_preferred():
    # The naive dated item is intentionally placed LAST so ordering depends on
    # the recency bonus (stable sort would otherwise keep input order).
    undated = _art("Company X other update", "https://b.example/2",
                   snippet="reports results", quality=0.6)
    naive = _art("Company X reports results", "https://a.example/1",
                 snippet="reports results", quality=0.6,
                 published=datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(days=1))
    ranked = filter_and_rank([undated, naive], "Company X reports results",
                             max_results=2)
    assert ranked[0].url == "https://a.example/1"


def test_all_undated_input_is_safe_and_deterministic():
    arts = [_art(f"Company X update {i}", f"https://x.example/{i}")
            for i in range(4)]
    first = [a.url for a in filter_and_rank(list(arts), "company x", 4)]
    second = [a.url for a in filter_and_rank(list(arts), "company x", 4)]
    assert first == second and len(first) == 4


# ---------------------------------------------------------------------------
# Homepage / section-page penalties
# ---------------------------------------------------------------------------

def test_undated_section_page_is_deprioritized():
    # Two otherwise-identical undated items: only the section-page penalty can
    # order them (equal quality/overlap; stable sort would keep input order).
    section = _art("Press releases | Company X", "https://companyx.com/press",
                   snippet="Company X news", quality=0.5)
    normal = _art("Company X market update", "https://companyx.com/update",
                  snippet="Company X news", quality=0.5)
    ranked = filter_and_rank([section, normal], "Company X news", max_results=2)
    assert [a.url for a in ranked] == ["https://companyx.com/update",
                                       "https://companyx.com/press"]


def test_dated_section_page_is_not_penalized():
    section = _art("Press releases | Company X", "https://companyx.com/press",
                   snippet="Company X news", quality=0.5,
                   published=_days_ago(1))
    undated = _art("Some other company news", "https://other.com/x",
                   snippet="Company X news", quality=0.5)
    ranked = filter_and_rank([undated, section], "Company X news",
                             max_results=2)
    assert ranked[0].url == "https://companyx.com/press"


def test_homepage_still_deprioritized():
    home = _art("Company X", "https://companyx.com/",
                snippet="Welcome to Company X quarterly results", quality=0.9)
    specific = _art("Company X reports results", "https://companyx.com/news/1",
                    snippet="Company X reported quarterly results",
                    quality=0.6)
    ranked = filter_and_rank([home, specific], "Company X results",
                             max_results=2)
    assert ranked[0].url == "https://companyx.com/news/1"


def test_full_text_result_outranks_snippet_only_on_tie():
    # Distinct titles so story-dedup does not collapse them; the only
    # difference is extracted content (+0.10).
    snippet_only = _art("Company X reports results", "https://a.example/1",
                        snippet="company x reports results", quality=0.6)
    full = _art("Company X results published", "https://b.example/2",
                snippet="company x reports results", quality=0.6,
                content="Long extracted article body " * 30)
    ranked = filter_and_rank([snippet_only, full], "Company X results",
                             max_results=2)
    assert ranked[0].url == "https://b.example/2"


# ---------------------------------------------------------------------------
# Cap / unusable handling
# ---------------------------------------------------------------------------

def test_ranking_drops_unusable_results():
    arts = [
        _art("", "https://a.example/empty", snippet="x"),        # no title
        _art("No text", "https://b.example/notext", snippet=""),  # no snippet
        _art("Real story 1", "https://c.example/1"),
        _art("Real story 2", "https://d.example/2"),
        _art("Real story 3", "https://e.example/3"),
    ]
    # Cap high enough that it cannot mask the drop.
    ranked = filter_and_rank(arts, "real story", max_results=10)
    assert len(ranked) == 3
    assert all(a.url.startswith("https://") for a in ranked)


def test_ranking_respects_cap():
    arts = [_art(f"Real story {i}", f"https://x.example/{i}")
            for i in range(5)]
    assert len(filter_and_rank(arts, "real story", max_results=2)) == 2


def test_non_positive_cap_returns_empty():
    # Two usable items so a naive `distinct[:max_results]` slice with a
    # negative value would still return something without the guard.
    arts = [_art("Story one", "https://x.example/1"),
            _art("Story two", "https://x.example/2")]
    assert filter_and_rank(arts, "story", 0) == []
    assert filter_and_rank(arts, "story", -1) == []


# ---------------------------------------------------------------------------
# Integration: SearchTool.populates source_quality before ranking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_tool_populates_source_quality_and_ranks_reliable_first(
        monkeypatch):
    # Equal query overlap for both, different reliability: only a populated
    # source_quality can order them, so this test fails without
    # rank_by_source_quality in SearchTool.run.
    async def fake_searxng(query, settings, max_results=10, time_range="month"):
        return [
            _art("Company X results rumour", "https://sketchyblog.example/x",
                 snippet="company x results"),
            _art("Company X results report", "https://www.reuters.com/x",
                 snippet="company x results"),
        ]

    monkeypatch.setattr("agent.tools.searxng_search", fake_searxng)
    settings = Settings(searxng_base_url="http://localhost:8888")
    tool = SearchTool(settings)
    obs = await tool.run(WebSearchArgs(query="Company X results",
                                       max_results=5, time_range="month"))
    assert obs["ok"] is True
    assert obs["results"][0]["url"] == "https://www.reuters.com/x"
    assert obs["results"][0]["source_quality"] > \
        obs["results"][1]["source_quality"]
