"""Phase 1: LLM query planner validation, category routing and fallback (research-stack)."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research-stack" / "app"))

from research_agent.agent import ResearchAgent  # noqa: E402
from research_agent.config import Settings  # noqa: E402
from research_agent.planner import (  # noqa: E402
    classify_category, fallback_plan, infer_time_range, parse_plan)
from research_agent.searx_client import SearxClient  # noqa: E402


def test_valid_llm_plan_is_accepted_with_categories_and_time_ranges():
    plan = parse_plan({"sub_questions": [
        {"question": "What did NVIDIA announce?", "category": "news", "time_range": "month",
         "queries": ["nvidia announcement", "nvidia buyback"]},
        {"question": "How is faithfulness measured?", "category": "science", "queries": ["rag faithfulness"]},
        {"question": "Which python library does X?", "category": "it", "queries": ["python x library"]},
    ]}, "q")
    assert plan.source == "llm" and not plan.notes
    assert [s.category for s in plan.sub_questions] == ["news", "science", "it"]
    assert plan.sub_questions[0].time_range == "month"
    sqs = plan.all_queries()
    assert {(s.q, s.categories, s.time_range) for s in sqs} >= {
        ("nvidia announcement", "news", "month"), ("rag faithfulness", "science", None)}


def test_subquestions_are_capped_at_five():
    plan = parse_plan({"sub_questions": [{"question": f"q{i}", "queries": [f"s{i}"]} for i in range(9)]}, "q")
    assert len(plan.sub_questions) == 5


def test_invalid_category_and_range_are_repaired():
    plan = parse_plan({"sub_questions": [
        {"question": "Latest earnings announcement news", "category": "finance", "time_range": "decade",
         "queries": ["earnings"]},
        {"question": "Background on topic", "category": "general", "queries": []}]}, "q")
    first, second = plan.sub_questions
    assert first.category == "news" and first.time_range is None  # heuristic category; bad range dropped
    assert second.queries == ["Background on topic"]               # unsearchable sub-question gets its own text
    assert plan.source == "llm-repaired" and any("invalid category" in n for n in plan.notes)


def test_legacy_flat_query_format_is_still_understood():
    plan = parse_plan({"sub_questions": ["What happened?", "Why?"],
                       "queries": [{"q": "nvidia news", "category": "news", "time_range": "week"}]}, "q")
    assert any(s.q == "nvidia news" and s.categories == "news" and s.time_range == "week"
               for s in plan.all_queries())


@pytest.mark.parametrize("bad", [None, [], "text", {}, {"sub_questions": []}, {"sub_questions": "x"},
                                 {"sub_questions": [{"category": "news"}, 5, None]}])
def test_invalid_planner_output_falls_back_safely(bad):
    plan = parse_plan(bad, "Latest NVIDIA news this week")
    assert plan.source == "fallback"
    assert len(plan.sub_questions) == 1
    sq = plan.all_queries()[0]
    assert sq.q == "Latest NVIDIA news this week" and sq.categories == "news" and sq.time_range == "week"


@pytest.mark.parametrize("text,cat", [
    ("NVIDIA announces record buyback shares", "news"),
    ("peer-reviewed study on citation faithfulness benchmark", "science"),
    ("python docker error message when installing library", "it"),
    ("history of the roman empire", "general")])
def test_category_heuristic(text, cat):
    assert classify_category(text) == cat


def test_time_range_only_when_stated():
    assert infer_time_range("what happened in the past month") == "month"
    assert infer_time_range("history of rome") is None
    assert fallback_plan("history of rome").sub_questions[0].time_range is None


def test_engine_hints_are_validated_against_the_instance_config():
    raw = {"sub_questions": [{"question": "papers", "category": "science", "queries": ["x"],
                              "engines": ["arxiv", "madeup-engine"]}]}
    assert parse_plan(raw, "q", known_engines=["arxiv", "pubmed"]).sub_questions[0].engines == ["arxiv"]
    assert parse_plan(raw, "q", known_engines=None).sub_questions[0].engines is None  # cannot verify: ignore
    assert parse_plan(raw, "q", known_engines=["pubmed"]).sub_questions[0].engines is None


def test_query_budget_keeps_every_subquestion_represented():
    raw = {"sub_questions": [{"question": f"q{i}", "queries": ["a", "b", "c"]} for i in range(4)]}
    plan = parse_plan(raw, "q", max_queries=5)
    assert all(s.queries for s in plan.sub_questions)
    assert sum(len(s.queries) for s in plan.sub_questions) == 5


def test_interleaved_queries_touch_every_subquestion_under_a_small_limit():
    plan = parse_plan({"sub_questions": [
        {"question": "a", "category": "news", "queries": ["a1", "a2"]},
        {"question": "b", "category": "science", "queries": ["b1", "b2"]}]}, "q")
    assert [s.q for s in plan.all_queries(limit=2)] == ["a1", "b1"]


# ---- routing to SearXNG: categories param, engines override, cache keys ----

BASE = "http://searx.test"


@pytest.mark.asyncio
async def test_routing_sends_categories_and_time_range_to_searxng():
    plan = parse_plan({"sub_questions": [
        {"question": "n", "category": "news", "time_range": "week", "queries": ["nvidia"]},
        {"question": "s", "category": "science", "queries": ["faithfulness"]},
        {"question": "i", "category": "it", "queries": ["asyncio bug"]}]}, "q")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    with respx.mock(base_url=BASE) as router:
        router.get("/search").mock(side_effect=handler)
        client = SearxClient(BASE, None)
        for sq in plan.all_queries():
            await client.search(sq)
        await client.close()
    by_q = {p["q"]: p for p in seen}
    assert by_q["nvidia"]["categories"] == "news" and by_q["nvidia"]["time_range"] == "week"
    assert by_q["faithfulness"]["categories"] == "science" and "time_range" not in by_q["faithfulness"]
    assert by_q["asyncio bug"]["categories"] == "it"
    assert all("engines" not in p for p in seen)  # category routing reuses the instance's own engine config


@pytest.mark.asyncio
async def test_explicit_engines_replace_categories_and_known_engines_come_from_config():
    with respx.mock(base_url=BASE) as router:
        router.get("/config").mock(return_value=httpx.Response(200, json={"engines": [
            {"name": "arxiv", "enabled": True}, {"name": "pubmed", "enabled": True},
            {"name": "off", "enabled": False}]}))
        route = router.get("/search").mock(return_value=httpx.Response(200, json={"results": []}))
        client = SearxClient(BASE, None)
        assert await client.known_engines() == ["arxiv", "pubmed"]
        plan = parse_plan({"sub_questions": [{"question": "p", "category": "science", "queries": ["x"],
                                              "engines": ["arxiv"]}]}, "q",
                          known_engines=await client.known_engines())
        await client.search(plan.all_queries()[0])
        await client.close()
    params = dict(route.calls[0].request.url.params)
    assert params["engines"] == "arxiv" and "categories" not in params


def test_cache_key_depends_on_query_category_and_time_range():
    from research_agent.cache import Cache
    from research_agent.models import SearchQuery
    c = SearxClient(BASE, None)
    keys = {Cache.key("sx", c._params(sq, 1, "en")) for sq in [
        SearchQuery("nvidia", "news", "week"), SearchQuery("nvidia", "news", "month"),
        SearchQuery("nvidia", "general", "week"), SearchQuery("nvidia2", "news", "week")]}
    assert len(keys) == 4


# ---- planner in the agent: LLM failures never stop a run ----

class _LLM:
    def __init__(self, data=None, exc=None):
        self.data, self.exc = data, exc

    async def chat_json(self, *_a, **_k):
        if self.exc:
            raise self.exc
        return self.data


class _Searx:
    async def known_engines(self):
        return ["arxiv"]


def _agent(llm) -> ResearchAgent:
    return ResearchAgent(Settings(), cache=object(), searx=_Searx(), extractor=object(), llm=llm,
                         embedder=object(), reranker=object(), store=object())


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ValueError("not json"), httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
async def test_agent_plan_falls_back_when_llm_fails(exc):
    plan = await _agent(_LLM(exc=exc)).plan("latest nvidia news this week")
    assert plan.source == "fallback" and plan.sub_questions[0].category == "news"


@pytest.mark.asyncio
async def test_agent_plan_uses_valid_llm_output():
    data = {"sub_questions": [{"question": "a", "category": "science", "queries": ["x"], "engines": ["arxiv"]},
                              {"question": "b", "category": "news", "queries": ["y"]}]}
    plan = await _agent(_LLM(data=data)).plan("q")
    assert plan.source == "llm" and plan.sub_questions[0].engines == ["arxiv"]
