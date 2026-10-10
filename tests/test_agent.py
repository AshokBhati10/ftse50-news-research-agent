"""Tests for the agentic research core: planner, tools, agent loop, answers.

All model calls and network access are stubbed. What is real: task
classification, plan validation/sanitization, tool validation and bounds,
search-result filtering, evidence handling, answer parsing, and the
answer-first report contract.
"""

from __future__ import annotations

import pytest

from agent.llm_client import LLMAuthError, LLMClient, LLMError, _is_auth_error
from agent.planner import QueryPlanner, classify_task_type, heuristic_plan
from agent.research_agent import (
    ResearchAgent,
    ResearchAnalyzer,
    ResponseParser,
)
from agent.tools import (
    ArticleExtractor,
    SearchTool,
    ToolExecutor,
    ToolRegistry,
    ToolValidationError,
    filter_and_rank,
)
from config.settings import Settings
from utils.models import (
    AgentStep,
    QueryAnswer,
    ResearchPlan,
    SourceRecord,
    TaskType,
    ToolTraceEntry,
)


def _settings(**overrides):
    base = dict(
        ollama_api_key="test-key-DO-NOT-LEAK",
        ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b",
        searxng_base_url="http://localhost:8888",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Task classification + heuristic plans
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("Who is the CEO of HSBC Holdings?", TaskType.FACTUAL_LOOKUP),
    ("When did the company announce the acquisition?", TaskType.FACTUAL_LOOKUP),
    ("Why did HSBC shares fall this week?", TaskType.NEWS_RESEARCH),
    ("Latest news on semiconductor stocks", TaskType.NEWS_RESEARCH),
    ("Compare HSBC and Barclays.", TaskType.COMPARISON),
    ("HSBC vs Barclays dividend yield", TaskType.COMPARISON),
    ("Summarize this article: https://example.com/a", TaskType.SUMMARIZATION),
    ("Explain the risks of AI regulation", TaskType.GENERAL),
])
def test_classify_task_type(query, expected):
    assert classify_task_type(query) == expected


def test_heuristic_plan_summarization_avoids_broad_search():
    plan = heuristic_plan("Summarize this article: https://example.com/a", _settings())
    assert plan.task_type == TaskType.SUMMARIZATION
    assert plan.tools_allowed == ["fetch_article"]
    assert plan.search_queries == []
    assert not plan.needs_web_research


def test_heuristic_plan_factual_lookup():
    plan = heuristic_plan("Who is the CEO of HSBC?", _settings())
    assert plan.task_type == TaskType.FACTUAL_LOOKUP
    assert "web_search" in plan.tools_allowed
    assert plan.search_queries == ["Who is the CEO of HSBC?"]


# ---------------------------------------------------------------------------
# Planner sanitization (mocked LLM)
# ---------------------------------------------------------------------------

class _FakePlannerLLM:
    def __init__(self, plan):
        self._plan = plan

    async def run(self, prompt):
        return self._plan


def test_planner_sanitizes_llm_plan(monkeypatch):
    settings = _settings()
    planner = QueryPlanner(settings, run_id="t1")
    wild = ResearchPlan(
        task_type=TaskType.NEWS_RESEARCH,
        intent_summary="x",
        tools_allowed=["web_search", "fetch_article"],
        search_queries=["q1", "q2"],
        max_tool_calls=3,
    )
    # Inject hostile values bypassing pydantic validation.
    object.__setattr__(wild, "tools_allowed",
                       ["web_search", "fetch_article", "run_shell"])
    object.__setattr__(wild, "search_queries",
                       ["q1", "q2", "q3", "q4", "q5", "q6"])
    object.__setattr__(wild, "max_tool_calls", 99)
    monkeypatch.setattr(planner, "_llm", lambda: _FakePlannerLLM(wild))
    import asyncio
    plan = asyncio.get_event_loop().run_until_complete(planner.plan("news"))
    assert plan.tools_allowed == ["web_search", "fetch_article"]  # shell removed
    assert len(plan.search_queries) <= 5
    assert plan.max_tool_calls == settings.max_tool_calls  # clamped


def test_planner_summarization_plan_always_gets_fetch_article(monkeypatch):
    """A summarization plan without fetch_article would be unable to fetch
    the target article; sanitization must add it."""
    settings = _settings()
    planner = QueryPlanner(settings, run_id="t3")
    weak = ResearchPlan(
        task_type=TaskType.SUMMARIZATION,
        intent_summary="Summarize the article",
        needs_web_research=False,
        tools_allowed=["web_search"],
        search_queries=[],
    )
    monkeypatch.setattr(planner, "_llm", lambda: _FakePlannerLLM(weak))
    import asyncio
    plan = asyncio.get_event_loop().run_until_complete(
        planner.plan("Summarize this article: https://example.com/a"))
    assert plan.tools_allowed == ["fetch_article"]
    assert plan.search_queries == []
    assert not plan.needs_web_research


def test_planner_falls_back_when_llm_down(monkeypatch):
    from agent import planner as planner_mod

    class _Boom:
        async def run(self, prompt):
            raise LLMError("down")

    settings = _settings()
    p = QueryPlanner(settings, run_id="t2")
    monkeypatch.setattr(p, "_llm", lambda: _Boom())
    import asyncio
    plan = asyncio.get_event_loop().run_until_complete(
        p.plan("Who is the CEO of HSBC?"))
    assert plan.task_type == TaskType.FACTUAL_LOOKUP  # heuristic fallback


# ---------------------------------------------------------------------------
# Tool validation and bounds
# ---------------------------------------------------------------------------

def test_registry_only_permits_known_tools():
    reg = ToolRegistry(_settings())
    assert set(reg.names()) == {"web_search", "fetch_article"}
    assert reg.get("nope") is None


@pytest.mark.asyncio
async def test_executor_rejects_unknown_tool():
    ex = ToolExecutor(ToolRegistry(_settings()))
    with pytest.raises(ToolValidationError):
        await ex.execute("run_shell", {"cmd": "rm -rf /"})


@pytest.mark.asyncio
async def test_executor_rejects_bad_args():
    ex = ToolExecutor(ToolRegistry(_settings()))
    with pytest.raises(ToolValidationError):
        await ex.execute("web_search", {"query": "   "})
    with pytest.raises(ToolValidationError):
        await ex.execute("fetch_article", {"url": "not-a-url"})
    with pytest.raises(ToolValidationError):
        await ex.execute("web_search", {"query": "x", "time_range": "decade"})


@pytest.mark.parametrize("url", [
    "http://localhost:8000/admin",
    "http://127.0.0.1:9000/secret",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.5/x",
    "http://[::1]/x",
    "http://user:pass@internal.example/x",
    "https://example.com:8443/x",
    "file:///etc/passwd",
])
@pytest.mark.asyncio
async def test_executor_rejects_ssrf_fetch_targets(url):
    ex = ToolExecutor(ToolRegistry(_settings()))
    with pytest.raises(ToolValidationError):
        await ex.execute("fetch_article", {"url": url})


@pytest.mark.asyncio
async def test_executor_accepts_public_fetch_target(monkeypatch):
    ex = ToolExecutor(ToolRegistry(_settings()))
    ran = {"n": 0}

    async def fake_fetch(self, args):
        ran["n"] += 1
        return {"ok": True, "url": args.url, "content": "text",
                "extraction_status": "ok"}

    monkeypatch.setattr(ArticleExtractor, "run", fake_fetch)
    obs = await ex.execute("fetch_article", {"url": "https://example.com/article"})
    assert obs.get("ok") is True
    assert ran["n"] == 1


@pytest.mark.asyncio
async def test_executor_suppresses_duplicate_search(monkeypatch):
    calls = {"n": 0}

    async def fake_run(self, args):
        calls["n"] += 1
        return {"ok": True, "results": []}

    reg = ToolRegistry(_settings())
    monkeypatch.setattr(SearchTool, "run", fake_run)
    ex = ToolExecutor(reg)
    await ex.execute("web_search", {"query": "HSBC CEO"})
    dup = await ex.execute("web_search", {"query": "  hsbc ceo "})
    assert dup.get("duplicate") is True
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Search-result filtering
# ---------------------------------------------------------------------------

def _art(title, url, snippet="some snippet here", quality=0.5):
    from utils.models import Article
    return Article(company="", ticker="", title=title, url=url,
                   snippet=snippet, source_quality=quality)


def test_filter_drops_unusable_and_deprioritizes_homepages():
    arts = [
        _art("HSBC names new CEO", "https://www.hsbc.com/news/ceo-announcement",
             "HSBC Holdings has appointed Georges Elhedery as Group Chief Executive",
             quality=0.6),
        _art("HSBC", "https://www.hsbc.com/", "Welcome to HSBC", quality=0.9),
        _art("", "https://example.com/empty", "x", quality=0.9),  # no title
        _art("No text", "https://example.com/notext", "", quality=0.9),  # no text
    ]
    ranked = filter_and_rank(arts, "Who is the CEO of HSBC?", max_results=10)
    urls = [a.url for a in ranked]
    assert "https://example.com/empty" not in urls
    assert "https://example.com/notext" not in urls
    # Specific article outranks the homepage despite lower source quality.
    assert urls[0] == "https://www.hsbc.com/news/ceo-announcement"


# ---------------------------------------------------------------------------
# Agent loop with stubbed model + tools
# ---------------------------------------------------------------------------

def _fake_search_results():
    return {
        "ok": True,
        "results": [
            {
                "title": "HSBC confirms Georges Elhedery as Group Chief Executive",
                "url": "https://www.hsbc.com/news/2024/elhedery-ceo",
                "source": "hsbc.com",
                "published_at": "2024-09-06T00:00:00+00:00",
                "snippet": "HSBC Holdings plc announces Georges Elhedery as Group Chief Executive.",
                "source_quality": 0.95,
                "is_homepage": False,
            },
            {
                "title": "HSBC homepage",
                "url": "https://www.hsbc.com/",
                "source": "hsbc.com",
                "published_at": None,
                "snippet": "Welcome to HSBC",
                "source_quality": 0.9,
                "is_homepage": True,
            },
        ],
        "providers": ["searxng"],
        "notes": [],
    }


def _ceo_answer():
    return QueryAnswer(
        query="Who is the CEO of HSBC Holdings?",
        task_type=TaskType.FACTUAL_LOOKUP,
        answer="Georges Elhedery is the Group Chief Executive of HSBC Holdings plc.",
        key_points=["HSBC appointed Georges Elhedery as Group Chief Executive."],
        sources=[SourceRecord(
            title="HSBC confirms Georges Elhedery as Group Chief Executive",
            url="https://www.hsbc.com/news/2024/elhedery-ceo",
            source_name="hsbc.com",
            supporting_excerpt="HSBC Holdings plc announces Georges Elhedery as Group Chief Executive.",
            supports_claims=[0],
        )],
        confidence=0.95,
        limitations=[],
        verified=True,
    )


@pytest.mark.asyncio
async def test_agent_ceo_lookup_answers_directly(monkeypatch):
    settings = _settings()
    agent = ResearchAgent(settings, cache=None, run_id="ceo1")

    plan = ResearchPlan(
        task_type=TaskType.FACTUAL_LOOKUP,
        intent_summary="Find HSBC's current CEO.",
        tools_allowed=["web_search", "fetch_article"],
        search_queries=["HSBC Holdings Group Chief Executive"],
        evidence_needed="Official announcement of the CEO.",
        expected_answer_format="Name and role first.",
        completion_criteria="Authoritative source confirms the name.",
        max_tool_calls=3,
    )
    monkeypatch.setattr(QueryPlanner, "plan",
                        lambda self, q: _coro(plan))

    async def fake_search_tool(self, args):
        return _fake_search_results()

    monkeypatch.setattr(SearchTool, "run", fake_search_tool)
    monkeypatch.setattr(ResearchAgent, "_next_step",
                        lambda self, q, p, t, c: _coro(
                            AgentStep(action="finish", reason="enough")))
    monkeypatch.setattr(ResearchAnalyzer, "synthesize",
                        lambda self, q, p, e: _coro(_ceo_answer()))

    result = await agent.research("Who is the CEO of HSBC Holdings?")
    assert "Georges Elhedery" in result.answer.answer
    assert result.answer.verified
    # The answer is primary; sources support it.
    assert result.answer.sources[0].url == \
        "https://www.hsbc.com/news/2024/elhedery-ceo"
    assert result.answer.sources[0].supports_claims == [0]
    # Bounded: 1 planned search, then finish.
    assert result.counts["tool_calls"] == 1
    assert len(result.tool_trace) == 1
    assert result.tool_trace[0].tool == "web_search"


@pytest.mark.asyncio
async def test_agent_summarization_skips_broad_search(monkeypatch):
    settings = _settings()
    agent = ResearchAgent(settings, cache=None, run_id="sum1")

    plan = heuristic_plan("Summarize this article: https://example.com/a", settings)
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, q: _coro(plan))

    fetched = {"n": 0}

    async def fake_fetch(self, args):
        fetched["n"] += 1
        return {"ok": True, "url": "https://example.com/a", "title": "A",
                "published_at": None, "extraction_status": "ok",
                "content": "Full article text here.", "source": "example.com"}

    monkeypatch.setattr(ArticleExtractor, "run", fake_fetch)
    monkeypatch.setattr(ResearchAgent, "_next_step",
                        lambda self, q, p, t, c: _coro(
                            AgentStep(action="finish", reason="done")))
    monkeypatch.setattr(
        ResearchAnalyzer, "synthesize",
        lambda self, q, p, e: _coro(QueryAnswer(
            query=q, task_type=TaskType.SUMMARIZATION,
            answer="The article reports X.", key_points=["X"], sources=[],
            confidence=0.8, verified=True)))

    result = await agent.research("Summarize this article: https://example.com/a")
    tools_used = [t.tool for t in result.tool_trace]
    assert "web_search" not in tools_used  # no broad search for a URL summary
    assert fetched["n"] == 1
    assert result.answer.answer == "The article reports X."


@pytest.mark.asyncio
async def test_agent_tool_calls_bounded(monkeypatch):
    settings = _settings(max_tool_calls=3)
    agent = ResearchAgent(settings, cache=None, run_id="b1")
    plan = ResearchPlan(task_type=TaskType.GENERAL, intent_summary="x",
                        tools_allowed=["web_search"],
                        search_queries=["q1", "q2", "q3", "q4"],
                        max_tool_calls=3)
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, q: _coro(plan))
    async def empty_search(self, args):
        return {"ok": True, "results": [], "providers": [], "notes": []}
    monkeypatch.setattr(SearchTool, "run", empty_search)
    # Model keeps asking for more; the bound must stop it.
    monkeypatch.setattr(ResearchAgent, "_next_step",
                        lambda self, q, p, t, c: _coro(
                            AgentStep(action="call_tool", tool_name="web_search",
                                      arguments={"query": f"extra {c}"})))
    monkeypatch.setattr(ResearchAnalyzer, "synthesize",
                        lambda self, q, p, e: _coro(
                            QueryAnswer.unverified(q, TaskType.GENERAL, ["none"])))
    result = await agent.research("some general question")
    assert result.counts["tool_calls"] <= 3
    assert not result.answer.verified  # honest about insufficient evidence


@pytest.mark.asyncio
async def test_agent_disallowed_tool_request_not_executed_and_bounded(monkeypatch):
    """Regression: a model request for a tool outside the plan's allowlist is
    never executed, warns, and cannot spin the loop forever."""
    settings = _settings()
    agent = ResearchAgent(settings, cache=None, run_id="d1")
    plan = ResearchPlan(task_type=TaskType.GENERAL, intent_summary="x",
                        tools_allowed=["web_search"], search_queries=[],
                        max_tool_calls=5)
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, q: _coro(plan))
    fetches = {"n": 0}

    async def fake_fetch(self, args):
        fetches["n"] += 1
        return {"ok": True, "url": args.url}

    monkeypatch.setattr(ArticleExtractor, "run", fake_fetch)
    monkeypatch.setattr(ResearchAgent, "_next_step",
                        lambda self, q, p, t, c: _coro(
                            AgentStep(action="call_tool", tool_name="fetch_article",
                                      arguments={"url": "https://example.com/x"})))
    monkeypatch.setattr(ResearchAnalyzer, "synthesize",
                        lambda self, q, p, e: _coro(
                            QueryAnswer.unverified(q, TaskType.GENERAL, ["none"])))
    result = await agent.research("q")
    assert any("disallowed tool" in w for w in result.warnings)
    assert fetches["n"] == 0  # the disallowed tool never executed
    assert all(t.tool != "fetch_article" or not t.ok
               for t in result.tool_trace)
    assert result.counts["tool_calls"] == 0
    # Bounded: repeated disallowed requests stop the loop instead of spinning.
    assert len(result.tool_trace) <= 2


@pytest.mark.asyncio
async def test_agent_recovers_after_disallowed_request(monkeypatch):
    """Regression for 'Agent requested disallowed tool; stopped': one bad
    tool choice must not end research — the loop continues and executes a
    later allowed tool within the remaining budget."""
    settings = _settings()
    agent = ResearchAgent(settings, cache=None, run_id="d2")
    plan = ResearchPlan(task_type=TaskType.GENERAL, intent_summary="x",
                        tools_allowed=["web_search"], search_queries=["q1"],
                        max_tool_calls=5)
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, q: _coro(plan))

    fetches = {"n": 0}

    async def fake_fetch(self, args):
        fetches["n"] += 1
        return {"ok": True, "url": args.url}

    monkeypatch.setattr(ArticleExtractor, "run", fake_fetch)

    async def fake_search(self, args):
        return _fake_search_results()

    monkeypatch.setattr(SearchTool, "run", fake_search)

    steps = [
        AgentStep(action="call_tool", tool_name="fetch_article",
                  arguments={"url": "https://example.com/x"}),
        AgentStep(action="call_tool", tool_name="web_search",
                  arguments={"query": "follow-up"}),
        AgentStep(action="finish", reason="done"),
    ]
    seq = {"i": 0}

    async def scripted_next_step(self, q, p, t, c):
        step = steps[min(seq["i"], len(steps) - 1)]
        seq["i"] += 1
        return step

    monkeypatch.setattr(ResearchAgent, "_next_step", scripted_next_step)
    monkeypatch.setattr(ResearchAnalyzer, "synthesize",
                        lambda self, q, p, e: _coro(_ceo_answer()))

    result = await agent.research("recoverable tool-selection mistake")
    assert fetches["n"] == 0
    rejected = [t for t in result.tool_trace
                if t.tool == "fetch_article" and not t.ok]
    assert len(rejected) == 1
    assert any("disallowed tool" in w for w in result.warnings)
    # The allowed follow-up search still executed after the rejection.
    assert any(t.tool == "web_search" and t.ok for t in result.tool_trace)
    assert result.counts["tool_calls"] == 2
    assert len(result.evidence) >= 2


@pytest.mark.asyncio
async def test_agent_allowed_fetch_article_executes(monkeypatch):
    """When the plan allows fetch_article, a model request for it must be
    advertised, executed, and its content added to the evidence pool."""
    settings = _settings()
    agent = ResearchAgent(settings, cache=None, run_id="f1")
    plan = ResearchPlan(task_type=TaskType.NEWS_RESEARCH, intent_summary="x",
                        tools_allowed=["web_search", "fetch_article"],
                        search_queries=["q1"], max_tool_calls=5)
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, q: _coro(plan))

    fetches = {"n": 0}

    async def fake_fetch(self, args):
        fetches["n"] += 1
        return {"ok": True, "url": args.url, "title": "Fetched",
                "published_at": "2024-09-06T00:00:00+00:00",
                "extraction_status": "ok",
                "content": "Full text confirms the claim in detail.",
                "source": "hsbc.com"}

    monkeypatch.setattr(ArticleExtractor, "run", fake_fetch)

    async def fake_search(self, args):
        return _fake_search_results()

    monkeypatch.setattr(SearchTool, "run", fake_search)

    steps = [
        AgentStep(action="call_tool", tool_name="fetch_article",
                  arguments={"url":
                             "https://www.hsbc.com/news/2024/elhedery-profile"}),
        AgentStep(action="finish", reason="enough"),
    ]
    seq = {"i": 0}

    async def scripted_next_step(self, q, p, t, c):
        step = steps[min(seq["i"], len(steps) - 1)]
        seq["i"] += 1
        return step

    monkeypatch.setattr(ResearchAgent, "_next_step", scripted_next_step)
    monkeypatch.setattr(ResearchAnalyzer, "synthesize",
                        lambda self, q, p, e: _coro(_ceo_answer()))

    result = await agent.research("Who is the CEO of HSBC Holdings?")
    assert fetches["n"] == 1
    assert any(t.tool == "fetch_article" and t.ok for t in result.tool_trace)
    assert any(e.get("kind") == "article" for e in result.evidence)


@pytest.mark.asyncio
async def test_next_step_prompt_advertises_only_plan_tools(monkeypatch):
    """Regression for 'Agent requested disallowed tool fetch_article': the
    follow-up prompt must advertise only the tools the plan allows, so the
    model's tool choices cannot exceed the plan."""
    captured: dict = {}

    class _FakeStepLLM:
        def __init__(self, settings, *, system_prompt="", output_type=None,
                     run_id=""):
            captured["prompt"] = system_prompt

        async def run(self, prompt):
            return AgentStep(action="finish", reason="ok")

    monkeypatch.setattr("agent.research_agent.LLMClient", _FakeStepLLM)
    agent = ResearchAgent(_settings(), cache=None, run_id="p1")
    plan = ResearchPlan(task_type=TaskType.GENERAL, intent_summary="x",
                        tools_allowed=["web_search"], max_tool_calls=3)
    step = await agent._next_step("q", plan, [], 0)
    assert step.action == "finish"
    text = captured["prompt"]
    assert "web_search(" in text
    assert "fetch_article" not in text

    plan_both = ResearchPlan(task_type=TaskType.GENERAL, intent_summary="x",
                             tools_allowed=["web_search", "fetch_article"],
                             max_tool_calls=3)
    await agent._next_step("q", plan_both, [], 0)
    text_both = captured["prompt"]
    assert "web_search(" in text_both and "fetch_article(" in text_both


def _coro(value):
    async def _inner():
        return value
    return _inner()


# ---------------------------------------------------------------------------
# ResponseParser
# ---------------------------------------------------------------------------

def test_parser_drops_fabricated_sources():
    parser = ResponseParser()
    answer = _ceo_answer()
    answer.sources.append(SourceRecord(
        title="Invented", url="https://invented.example/claim",
        supporting_excerpt="x", supports_claims=[0]))
    out = parser.validate(answer, {"https://www.hsbc.com/news/2024/elhedery-ceo"})
    assert len(out.sources) == 1
    assert out.sources[0].url == "https://www.hsbc.com/news/2024/elhedery-ceo"


def test_parser_fixes_bad_claim_indices():
    parser = ResponseParser()
    answer = _ceo_answer()
    answer.sources[0].supports_claims = [0, 7]
    out = parser.validate(answer, {"https://www.hsbc.com/news/2024/elhedery-ceo"})
    assert out.sources[0].supports_claims == [0]


def test_parser_unverified_forces_zero_confidence():
    parser = ResponseParser()
    answer = QueryAnswer.unverified("q", TaskType.GENERAL, ["insufficient"])
    answer.confidence = 0.9
    out = parser.validate(answer, set())
    assert out.confidence == 0.0
    assert not out.verified


# ---------------------------------------------------------------------------
# Answer-first report rendering
# ---------------------------------------------------------------------------

def test_answer_markdown_leads_with_answer(tmp_path):
    from datetime import datetime, timezone
    from nodes.output import build_answer_report, render_answer_markdown
    from agent.research_agent import AgentResult

    answer = _ceo_answer()
    result = AgentResult(
        query="Who is the CEO of HSBC Holdings?", plan=heuristic_plan("q", _settings()),
        answer=answer, findings=[], counts={"tool_calls": 1, "evidence_items": 2},
        warnings=[], tool_trace=[ToolTraceEntry(tool="web_search",
                                                arguments_summary="q",
                                                observation_summary="2 results")],
        evidence=[],
    )
    report = build_answer_report(
        query="Who is the CEO of HSBC Holdings?", run_id="run_a",
        started_at=datetime.now(timezone.utc), result=result, settings=_settings())
    assert report.answer is not None
    assert report.answer.answer.startswith("Georges Elhedery")
    md = render_answer_markdown(report)
    answer_pos = md.index("## Answer")
    sources_pos = md.index("## Sources")
    details_pos = md.index("## Research details")
    assert answer_pos < sources_pos < details_pos
    assert "Georges Elhedery" in md[:sources_pos]


# ---------------------------------------------------------------------------
# LLMClient safety
# ---------------------------------------------------------------------------

def test_is_auth_error():
    assert _is_auth_error(RuntimeError("401 Unauthorized"))
    assert _is_auth_error(RuntimeError("Incorrect API key provided"))
    assert not _is_auth_error(RuntimeError("connection reset"))


@pytest.mark.asyncio
async def test_llm_client_auth_error_not_retried(monkeypatch):
    settings = _settings(llm_max_retries=3)
    client = LLMClient(settings, run_id="auth1")
    calls = {"n": 0}

    async def boom(prompt, timeout):
        calls["n"] += 1
        raise LLMAuthError("401 Unauthorized: invalid api key")

    monkeypatch.setattr(client, "_single_run", boom)
    with pytest.raises(LLMAuthError):
        await client.run("hello")
    assert calls["n"] == 1  # never retried


@pytest.mark.asyncio
async def test_llm_client_retries_transient_then_raises(monkeypatch):
    settings = _settings(llm_max_retries=2)
    client = LLMClient(settings, run_id="t2")
    calls = {"n": 0}

    async def boom(prompt, timeout):
        calls["n"] += 1
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(client, "_single_run", boom)
    with pytest.raises(LLMError):
        await client.run("hello")
    assert calls["n"] == 3  # 1 + 2 retries
