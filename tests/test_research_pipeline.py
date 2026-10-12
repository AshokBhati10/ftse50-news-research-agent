"""Phase 2/4: iterative search, dedup, provenance, snippet-vs-fulltext, gap detection, stopping, partial failures.

All external services (SearXNG, extractor, embedder, reranker, Qdrant, LLM) are replaced with in-memory fakes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research-stack" / "app"))

from research_agent.agent import ResearchAgent  # noqa: E402
from research_agent.citations import CitationTracker  # noqa: E402
from research_agent.config import Settings  # noqa: E402
from research_agent.models import Document, Passage, SearchHit, SearchResponse  # noqa: E402
from research_agent.searx_client import SearxError  # noqa: E402
from research_agent.urls import hostname, normalize_url  # noqa: E402

PLAN = {"sub_questions": [
    {"question": "What was announced?", "category": "news", "time_range": "month", "queries": ["announce"]},
    {"question": "How is it evaluated?", "category": "science", "queries": ["evaluate"]}]}


def hit(url, title="T", snippet="snip", engines=("google",), query="q", published=None, score=1.0):
    return SearchHit(url=url, canonical_url=normalize_url(url), title=title, snippet=snippet, engine=engines[0],
                     engines=list(engines), score=score, category="", published=published, query=query)


def long_text(tag: str) -> str:
    return "\n".join(f"{tag} sentence number {i} with distinct content about subject {tag}{i}." for i in range(40))


class FakeSearx:
    def __init__(self, results: dict, unresponsive=None, delay=0.0):
        self.results, self.unresponsive, self.delay = results, unresponsive or [], delay
        self.calls: list = []

    async def known_engines(self):
        return None

    async def search(self, sq):
        self.calls.append(sq)
        if self.delay:
            await asyncio.sleep(self.delay)
        r = self.results.get(sq.q, [])
        if isinstance(r, Exception):
            raise r
        return SearchResponse(query=sq, hits=r, suggestions=[], unresponsive_engines=self.unresponsive)

    async def close(self): ...


class FakeExtractor:
    def __init__(self, texts: dict):
        self.texts, self.fetched = texts, []

    async def fetch(self, h):
        self.fetched.append(h.canonical_url)
        t = self.texts.get(h.canonical_url)
        if t is None:
            return None
        return Document(url=h.url, canonical_url=h.canonical_url, title=h.title, text=t, date="2026-10-01",
                        source=hostname(h.url), engine=h.engine, engines=h.engines, query=h.query,
                        doc_type="html", content_hash="x")

    async def close(self): ...


class FakeEmbedder:
    dim = 3

    async def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeReranker:
    def __init__(self, score=0.9):
        self.score = score

    async def rerank(self, query, passages, top_k, min_score=0.05):
        for p in passages:
            p.score = self.score
        return passages[:top_k]


class FakeStore:
    def __init__(self):
        self.items: list = []

    async def ensure_collection(self): ...

    async def upsert(self, chunks, vectors, run_id):
        self.items += [(run_id, c) for c in chunks]

    async def search(self, vector, limit=40, *, sources=None, run_id=None):
        return [Passage(text=c.text, url=c.doc.url, title=c.doc.title, date=c.doc.date, source=c.doc.source,
                        engine=c.doc.engine, score=0.5)
                for rid, c in self.items if run_id is None or rid == run_id][:limit]

    async def close(self): ...


class FakeLLM:
    def __init__(self, plan=PLAN, gaps=None):
        self.plan, self.gaps, self.gap_calls, self.synth_context = plan, gaps, 0, ""

    async def chat_json(self, system, user, **_k):
        if "query planner" in system:
            return self.plan
        self.gap_calls += 1
        return self.gaps or {"gaps": ["missing"], "follow_up_queries": [], "done": False}

    async def chat(self, system, user, **_k):
        self.synth_context = user
        return "Answer [1]."

    async def close(self): ...


def build(searx, extractor, llm=None, **cfg) -> tuple[ResearchAgent, FakeStore, FakeLLM]:
    store, llm = FakeStore(), llm or FakeLLM()
    settings = Settings(**{"max_rounds": 3, "queries_per_round": 5, "max_pages_per_round": 15, **cfg})
    agent = ResearchAgent(settings, cache=object(), searx=searx, extractor=extractor, llm=llm,
                          embedder=FakeEmbedder(), reranker=FakeReranker(), store=store)
    return agent, store, llm


@pytest.mark.asyncio
async def test_uncited_answer_is_flagged_and_sources_are_not_presented_as_citations():
    class NoCite(FakeLLM):
        async def chat(self, system, user, **_k):
            return "An answer without any citation markers."

    searx = FakeSearx({"announce": [hit("https://a.com/1")], "evaluate": [hit("https://b.com/2")]})
    agent, *_ = build(searx, FakeExtractor({"https://a.com/1": long_text("a"), "https://b.com/2": long_text("b")}),
                      llm=NoCite(), max_rounds=1)
    rep = await agent.run("question")
    assert rep.citations == []
    assert any("no valid inline citations" in w for w in rep.warnings)
    assert "## Sources consulted" in rep.answer_markdown and "## Sources\n" not in rep.answer_markdown


# ------------------------------------------------------------------ dedup + provenance

@pytest.mark.asyncio
async def test_urls_are_normalised_and_syndicated_text_is_deduplicated():
    shared = long_text("shared")
    searx = FakeSearx({
        "announce": [hit("https://a.com/x?utm_source=tw", engines=("google", "bing"), published="2026-10-02"),
                     hit("https://wire.com/copy")],
        "evaluate": [hit("https://www.a.com/x#frag"), hit("https://b.com/y")]})
    ext = FakeExtractor({"https://a.com/x": shared, "https://wire.com/copy": shared,
                         "https://b.com/y": long_text("other")})
    agent, store, _ = build(searx, ext)
    rep = await agent.run("question")
    assert sorted(ext.fetched) == ["https://a.com/x", "https://b.com/y", "https://wire.com/copy"]  # one a.com fetch
    status = {s.url: s.extraction for s in rep.sources}
    assert status["https://wire.com/copy"] == "duplicate_text"          # syndicated copy not counted twice
    assert status["https://a.com/x?utm_source=tw"] == "extracted"
    a = next(s for s in rep.sources if s.url.startswith("https://a.com/x"))
    assert a.engines == ["bing", "google"] and a.categories == ["news", "science"]
    assert set(a.queries) == {"announce", "evaluate"} and a.published == "2026-10-01"  # page date wins


@pytest.mark.asyncio
async def test_report_carries_plan_sources_and_citations():
    searx = FakeSearx({"announce": [hit("https://a.com/x")], "evaluate": [hit("https://b.com/y")]})
    agent, *_ = build(searx, FakeExtractor({"https://a.com/x": long_text("a"), "https://b.com/y": long_text("b")}))
    rep = await agent.run("question")
    assert [p["category"] for p in rep.plan] == ["news", "science"] and rep.plan[0]["time_range"] == "month"
    assert rep.plan_source == "llm" and rep.citations and rep.citations[0].url.startswith("https://")
    assert {s.extraction for s in rep.sources} == {"extracted"}


# ------------------------------------------------------------------ snippet vs full text

@pytest.mark.asyncio
async def test_failed_extraction_is_not_full_text_evidence():
    searx = FakeSearx({"announce": [hit("https://blocked.com/a", snippet="Only a teaser snippet.")],
                       "evaluate": [hit("https://blocked.com/b", snippet="Another snippet.")]})
    agent, store, llm = build(searx, FakeExtractor({}), max_rounds=1)
    rep = await agent.run("question")
    assert store.items == []                                         # nothing stored as article text
    assert rep.coverage == {"What was announced?": False, "How is it evaluated?": False}
    assert {s.extraction for s in rep.sources} == {"snippet_only"}   # used only as labelled snippets
    assert "search snippet only" in llm.synth_context
    assert any("could not be extracted" in w for w in rep.warnings)


@pytest.mark.asyncio
async def test_snippet_passages_are_labelled_and_never_full_text():
    p = Passage(text="t", url="https://x.com", title="X", date=None, source="x.com", engine="g", kind="snippet")
    block = CitationTracker().context_block([p])
    assert "search snippet only" in block
    agent, *_ = build(FakeSearx({}), FakeExtractor({}))
    assert agent.coverage({"q": [Passage("a", "u1", "t", None, "s1", "e", 0.9, "snippet"),
                                  Passage("b", "u2", "t", None, "s2", "e", 0.9, "snippet")]}) == {"q": False}


# ------------------------------------------------------------------ coverage, gap detection, stopping

@pytest.mark.asyncio
async def test_coverage_requires_enough_passages_from_distinct_sources():
    agent, *_ = build(FakeSearx({}), FakeExtractor({}))
    ok = [Passage("a", "u1", "t", None, "s1", "e", 0.9), Passage("b", "u2", "t", None, "s2", "e", 0.9)]
    one_source = [Passage("a", "u1", "t", None, "s1", "e", 0.9), Passage("b", "u1b", "t", None, "s1", "e", 0.9)]
    weak = [Passage("a", "u1", "t", None, "s1", "e", 0.01), Passage("b", "u2", "t", None, "s2", "e", 0.01)]
    assert agent.coverage({"ok": ok, "one": one_source, "weak": weak, "none": []}) == {
        "ok": True, "one": False, "weak": False, "none": False}


@pytest.mark.asyncio
async def test_stops_after_one_round_when_covered_without_an_llm_gap_call():
    searx = FakeSearx({"announce": [hit("https://a.com/1"), hit("https://b.com/2")],
                       "evaluate": [hit("https://c.com/3")]})
    ext = FakeExtractor({f"https://{h}.com/{n}": long_text(h) for h, n in [("a", 1), ("b", 2), ("c", 3)]})
    agent, _, llm = build(searx, ext)
    rep = await agent.run("question")
    assert rep.rounds == 1 and llm.gap_calls == 0 and rep.open_gaps == []
    assert all(rep.coverage.values())


@pytest.mark.asyncio
async def test_gap_triggers_targeted_follow_up_search_then_stops_when_covered():
    # Round 1 finds one source only (not enough); the follow-up finds the second.
    searx = FakeSearx({"announce": [hit("https://a.com/1")], "evaluate": [hit("https://a.com/2")],
                       "targeted follow up": [hit("https://z.com/9"), hit("https://y.com/8")]})
    ext = FakeExtractor({"https://a.com/1": long_text("a1"), "https://a.com/2": long_text("a2"),
                         "https://z.com/9": long_text("z"), "https://y.com/8": long_text("y")})
    llm = FakeLLM(gaps={"gaps": ["need more"], "done": False,
                        "follow_up_queries": [{"q": "targeted follow up", "category": "news", "time_range": "week"}]})
    agent, _, llm = build(searx, ext, llm=llm)
    rep = await agent.run("question")
    assert rep.rounds == 2 and llm.gap_calls == 1
    follow = [sq for sq in searx.calls if sq.q == "targeted follow up"][0]
    assert follow.categories == "news" and follow.time_range == "week"
    assert rep.queries_run == ["announce", "evaluate", "targeted follow up"]
    assert all(rep.coverage.values()) and rep.open_gaps == []


@pytest.mark.asyncio
async def test_follow_up_falls_back_to_subquestion_text_when_llm_gap_review_fails():
    class BrokenGapLLM(FakeLLM):
        async def chat_json(self, system, user, **k):
            if "query planner" in system:
                return self.plan
            raise ValueError("bad json")

    searx = FakeSearx({})
    agent, *_ = build(searx, FakeExtractor({}), llm=BrokenGapLLM(), max_rounds=2)
    rep = await agent.run("question")
    texts = {sq.q: sq for sq in searx.calls}
    assert "What was announced?" in texts and texts["What was announced?"].categories == "news"
    assert texts["How is it evaluated?"].categories == "science"
    assert rep.rounds == 2


@pytest.mark.asyncio
async def test_round_budget_is_respected():
    searx = FakeSearx({})  # never finds anything
    agent, _, llm = build(searx, FakeExtractor({}), max_rounds=2)
    rep = await agent.run("question")
    assert rep.rounds <= 2 and any("no usable evidence" in w for w in rep.warnings)


@pytest.mark.asyncio
async def test_total_query_budget_is_respected():
    plan = {"sub_questions": [{"question": f"s{i}", "queries": [f"q{i}a", f"q{i}b"]} for i in range(4)]}
    searx = FakeSearx({})
    agent, *_ = build(searx, FakeExtractor({}), llm=FakeLLM(plan=plan), max_rounds=5, max_total_queries=5,
                      queries_per_round=3)
    rep = await agent.run("question")
    assert len(rep.queries_run) <= 5 and len(set(rep.queries_run)) == len(rep.queries_run)


# ------------------------------------------------------------------ partial failures

@pytest.mark.asyncio
async def test_failed_search_and_unresponsive_engines_yield_a_partial_report():
    searx = FakeSearx({"announce": SearxError("boom"), "evaluate": [hit("https://b.com/y")]},
                      unresponsive=[("google", "CAPTCHA")])
    agent, *_ = build(searx, FakeExtractor({"https://b.com/y": long_text("b")}), max_rounds=1)
    rep = await agent.run("question")
    assert rep.answer_markdown.startswith("Answer")                 # run still completed
    assert any("announce" in w and "boom" in w for w in rep.warnings)
    assert any("google unresponsive" in w for w in rep.warnings)
    assert any(s.url == "https://b.com/y" for s in rep.sources)


@pytest.mark.asyncio
async def test_search_timeout_is_bounded_and_reported():
    searx = FakeSearx({"announce": [hit("https://a.com/1")]}, delay=0.5)
    agent, *_ = build(searx, FakeExtractor({}), max_rounds=1, search_timeout=0.05)
    rep = await agent.run("question")
    assert any("timed out" in w for w in rep.warnings) and rep.sources == []


@pytest.mark.asyncio
async def test_fetch_timeout_marks_page_failed_without_failing_the_run():
    class Slow(FakeExtractor):
        async def fetch(self, h):
            await asyncio.sleep(1)

    agent, *_ = build(FakeSearx({"announce": [hit("https://a.com/1")]}), Slow({}), max_rounds=1, fetch_timeout=0.05)
    rep = await agent.run("question")
    assert [s.extraction for s in rep.sources] == ["snippet_only"]


@pytest.mark.asyncio
async def test_retrieval_failure_keeps_the_run_alive():
    agent, store, _ = build(FakeSearx({"announce": [hit("https://a.com/1")]}),
                            FakeExtractor({"https://a.com/1": long_text("a")}), max_rounds=1)

    async def boom(*_a, **_k):
        raise ConnectionError("qdrant down")
    store.search = boom
    rep = await agent.run("question")
    assert any("evidence retrieval failed" in w for w in rep.warnings)


@pytest.mark.asyncio
async def test_concurrency_is_bounded_by_the_searx_client_semaphore():
    from research_agent.searx_client import SearxClient
    import httpx, respx
    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return httpx.Response(200, json={"results": []})

    with respx.mock(base_url="http://s.test") as router:
        router.get("/search").mock(side_effect=handler)
        c = SearxClient("http://s.test", None, concurrency=2)
        from research_agent.models import SearchQuery
        await asyncio.gather(*(c.search(SearchQuery(q=f"q{i}")) for i in range(6)))
        await c.close()
    assert peak == 2
