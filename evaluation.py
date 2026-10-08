"""Pydantic Evals suite for the FTSE 50 Market News Research Agent.

Covers: article relevance/impact classification, sentiment classification,
duplicate detection, source-quality ranking, structured-output validity,
missing/failed extraction handling, the search-fallback cascade, and
Ollama retry behaviour.

Deterministic cases run locally with no API keys. The duplicate-detection
cases need the (local, free) MiniLM model on first run; the LLM-judged cases
are skipped unless OLLAMA_* is configured — reported as skipped, never faked.

Usage:
    uv run python main.py --evaluate
    uv run python evaluation.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("ftse50.eval")

try:
    from pydantic_evals import Case, Dataset
    from pydantic_evals.evaluators import Evaluator, EvaluatorContext
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"pydantic-evals is not installed: {exc}. Run `uv sync` first.")

from config.settings import Settings  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "eval_data" / "cases.json"


# ---------------------------------------------------------------------------
# Generic evaluators
# ---------------------------------------------------------------------------

@dataclass
class EqualsExpected(Evaluator):
    """Passes when the task output exactly equals the case's expected output."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return ctx.output == ctx.expected_output


@dataclass
class DictEqualsExpected(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return ctx.output == ctx.expected_output


# ---------------------------------------------------------------------------
# Task functions (each takes the case `inputs` dict)
# ---------------------------------------------------------------------------

def _single_article(inputs: dict):
    from utils.models import Article
    return Article(
        company="Eval", ticker="EVAL.L",
        title=inputs["title"], url="https://example.com/eval",
        snippet=inputs.get("snippet", ""), search_provider="eval",
    )


def task_heuristic_impact(inputs: dict) -> str:
    from nodes.analysis import heuristic_analyze
    from utils.models import Company
    company = Company(name="Eval", ticker="EVAL.L")
    ranked = heuristic_analyze(company, [_single_article(inputs)], top_n=1)
    return ranked[0].impact.value


def task_heuristic_sentiment(inputs: dict) -> str:
    from nodes.analysis import heuristic_analyze
    from utils.models import Company
    company = Company(name="Eval", ticker="EVAL.L")
    ranked = heuristic_analyze(company, [_single_article(inputs)], top_n=1)
    return ranked[0].sentiment.value


def task_source_quality_tier(inputs: dict) -> str:
    from utils.source_quality import score_source
    _score, tier, _reason = score_source(inputs["url"])
    return tier


def task_duplicate_detection(inputs: dict) -> bool:
    from nodes.deduplication import SemanticDeduper
    from utils.models import Article
    deduper = SemanticDeduper()  # real MiniLM model; raises if unavailable
    arts = [
        Article(company="E", ticker="E.L", title="t", url="https://a.example/1",
                snippet=inputs["text_a"]),
        Article(company="E", ticker="E.L", title="t", url="https://a.example/2",
                snippet=inputs["text_b"]),
    ]
    # Titles are identical; snippet carries the signal.
    for a, t in zip(arts, (inputs["text_a"], inputs["text_b"])):
        a.title, a.snippet = t, ""
    return len(deduper.deduplicate(arts)) == 1


def task_structured_output_validity(inputs: dict) -> str:
    from pydantic import ValidationError
    from utils.models import AnalysisResult
    try:
        AnalysisResult.model_validate(inputs["payload"])
        return "valid"
    except ValidationError:
        return "invalid"


def task_search_fallback(inputs: dict) -> list[str]:
    """Harness: fake providers -> which providers did the cascade actually use?"""
    from nodes import search as search_node
    from utils.models import Article, Company

    async def fake_searxng(query, settings, max_results=10, time_range="month"):
        if inputs.get("searxng_raises"):
            raise search_node.SearchError("simulated SearXNG outage")
        return [
            Article(company="E", ticker="E.L", title=f"s{i}",
                    url=f"https://searxng.example/{i}", search_provider="searxng")
            for i in range(inputs.get("searxng_count", 0))
        ]

    async def fake_youcom(query, settings, max_results=10):
        return [
            Article(company="E", ticker="E.L", title=f"y{i}",
                    url=f"https://you.example/{i}", search_provider="you.com")
            for i in range(inputs.get("youcom_count", 0))
        ]

    async def fake_yf(company, max_results=10):
        return []

    orig = (search_node.searxng_search, search_node.youcom_search, search_node.yfinance_news)
    search_node.searxng_search, search_node.youcom_search, search_node.yfinance_news = (
        fake_searxng, fake_youcom, fake_yf,
    )
    try:
        settings = Settings()
        if inputs.get("you_key"):
            settings.you_api_key = "eval-key"
        company = Company(name="Eval", ticker="EVAL.L")
        outcome = asyncio.run(search_node.search_company_news(company, settings, cache=None))
        return sorted(outcome.providers_used)
    finally:
        (search_node.searxng_search, search_node.youcom_search,
         search_node.yfinance_news) = orig


def task_extraction_degraded(inputs: dict) -> str:
    """Harness: everything fails -> extraction_status must degrade gracefully."""
    from nodes import crawl as crawl_mod
    from utils.models import Article

    async def boom_crawl(url, settings):
        raise RuntimeError("simulated crawl failure")

    async def empty_fallback(url, settings):
        return None

    orig_crawl, orig_fallback = crawl_mod._crawl4ai_extract, crawl_mod._fallback_fetch
    crawl_mod._crawl4ai_extract, crawl_mod._fallback_fetch = boom_crawl, empty_fallback
    try:
        settings = Settings()
        settings.crawl_max_retries = 0
        article = Article(
            company="E", ticker="E.L", title="t", url="https://example.com/x",
            snippet=inputs.get("snippet", ""), search_provider="eval",
        )
        result = asyncio.run(crawl_mod.extract_article(article, settings, cache=None))
        return result.extraction_status
    finally:
        crawl_mod._crawl4ai_extract, crawl_mod._fallback_fetch = orig_crawl, orig_fallback


def task_ollama_retry(inputs: dict) -> dict:
    """Harness: fake LLM fails N times -> bounded retries, then success or give-up."""
    from nodes import analysis as analysis_mod
    from utils.models import Article, ArticleScore, Company, ImpactLevel, Sentiment
    from utils.models import AnalysisResult

    fail_times = inputs["fail_times"]
    calls = {"n": 0}

    class FakeRun:
        output = AnalysisResult(scores=[
            ArticleScore(index=0, impact=ImpactLevel.HIGH, sentiment=Sentiment.POSITIVE,
                         reason="ok", confidence=0.9),
        ])

    class FakeAgent:
        async def run(self, prompt):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise RuntimeError("simulated transient LLM failure")
            return FakeRun()

    orig_agent, orig_retry = analysis_mod.build_analysis_agent, analysis_mod.retry_async
    real_retry = orig_retry

    async def fast_retry(fn, **kw):
        kw["base_delay_s"] = 0.01
        kw["max_delay_s"] = 0.05
        return await real_retry(fn, **kw)

    analysis_mod.build_analysis_agent = lambda s: FakeAgent()  # noqa: E731
    analysis_mod.retry_async = fast_retry
    try:
        settings = Settings()
        settings.ollama_api_key = "eval-key"
        settings.ollama_base_url = "https://eval.invalid/v1"
        settings.ollama_model = "eval-model"
        settings.llm_max_retries = inputs.get("max_retries", 3)
        company = Company(name="Eval", ticker="EVAL.L")
        articles = [Article(company="E", ticker="E.L", title="t",
                            url="https://example.com/x", snippet="s")]
        try:
            ranked = asyncio.run(
                analysis_mod.analyze_company(company, articles, settings, cache=None))
            return {"attempts": calls["n"], "ok": len(ranked) == 1}
        except analysis_mod.AnalysisError:
            return {"attempts": calls["n"], "ok": False}
    finally:
        analysis_mod.build_analysis_agent, analysis_mod.retry_async = orig_agent, orig_retry


def task_llm_impact(inputs: dict, settings: Settings) -> str:
    """Real Ollama call (only when configured): impact for one article."""
    from nodes.analysis import analyze_company
    from utils.models import Company
    company = Company(name="Eval", ticker="EVAL.L")
    ranked = asyncio.run(analyze_company(company, [_single_article(inputs)], settings,
                                         cache=None))
    return ranked[0].impact.value if ranked else "NONE"


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------

def _load_cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def build_datasets(settings: Settings) -> list[tuple[str, Dataset, object, bool]]:
    """Return (name, dataset, task, runnable) tuples."""
    cases = _load_cases()
    out: list[tuple[str, Dataset, object, bool]] = []

    def _ds(key: str, task, evaluator=None, expected_key: str = "expected"):
        ev = evaluator or EqualsExpected()
        ds = Dataset(
            name=key,
            cases=[Case(name=c["name"], inputs=c["inputs"], expected_output=c[expected_key])
                   for c in cases[key]],
            evaluators=[ev],
        )
        return ds

    # -- deterministic heuristic regression cases (spec-pinned behaviour) ----
    heuristic_impact = [
        ({"title": "X acquires rival for $2bn", "snippet": "acquisition"}, "High"),
        ({"title": "Q3 earnings beat estimates", "snippet": "earnings beat"}, "High"),
        ({"title": "Company issues profit warning", "snippet": "profit warning"}, "High"),
        ({"title": "Quarterly results published", "snippet": "results"}, "Medium"),
        ({"title": "Dividend announced", "snippet": "dividend"}, "Medium"),
        ({"title": "CEO gives interview", "snippet": "general comments"}, "Low"),
    ]
    out.append(("impact_heuristic", Dataset(
        name="impact_heuristic",
        cases=[Case(name=f"h{i}", inputs=inp, expected_output=exp)
               for i, (inp, exp) in enumerate(heuristic_impact)],
        evaluators=[EqualsExpected()]), task_heuristic_impact, True))

    heuristic_sentiment = [
        ({"title": "Beats estimates, raises outlook", "snippet": "beat raises"}, "Positive"),
        ({"title": "Profit warning, cuts guidance", "snippet": "warning cuts"}, "Negative"),
        ({"title": "Appoints new director", "snippet": "appointment"}, "Neutral"),
    ]
    out.append(("sentiment_heuristic", Dataset(
        name="sentiment_heuristic",
        cases=[Case(name=f"h{i}", inputs=inp, expected_output=exp)
               for i, (inp, exp) in enumerate(heuristic_sentiment)],
        evaluators=[EqualsExpected()]), task_heuristic_sentiment, True))

    out.append(("source_quality", _ds("source_quality", task_source_quality_tier,
                                                 expected_key="expected_tier"),
                task_source_quality_tier, True))

    # -- duplicate detection needs the local MiniLM model --------------------
    dup_runnable = True
    try:
        from nodes.deduplication import SemanticDeduper
        SemanticDeduper()._ensure_model()
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping duplicate-detection evals: embedding model unavailable (%s)", exc)
        dup_runnable = False
    out.append(("duplicate_detection",
                _ds("duplicate_detection", task_duplicate_detection,
                    expected_key="expected_duplicate"),
                task_duplicate_detection, dup_runnable))

    # -- structured output validity ------------------------------------------
    out.append(("structured_output", Dataset(name="structured_output", cases=[
        Case(name="wellformed", inputs={"payload": {"scores": [
            {"index": 0, "impact": "High", "sentiment": "Positive",
             "reason": "Major deal.", "confidence": 0.9}]}},
            expected_output="valid"),
        Case(name="bad_enum", inputs={"payload": {"scores": [
            {"index": 0, "impact": "Extreme", "sentiment": "Positive",
             "reason": "x", "confidence": 0.9}]}},
            expected_output="invalid"),
        Case(name="empty_scores", inputs={"payload": {"scores": []}},
            expected_output="invalid"),
        Case(name="missing_field", inputs={"payload": {"scores": [
            {"index": 0, "impact": "High"}]}},
            expected_output="invalid"),
    ], evaluators=[EqualsExpected()]), task_structured_output_validity, True))

    # -- search fallback cascade ----------------------------------------------
    out.append(("search_fallback", Dataset(name="search_fallback", cases=[
        Case(name="searxng_sufficient",
             inputs={"searxng_count": 6, "youcom_count": 5, "you_key": True},
             expected_output=["searxng"]),
        Case(name="searxng_thin_triggers_youcom",
             inputs={"searxng_count": 1, "youcom_count": 5, "you_key": True},
             expected_output=["searxng", "you.com"]),
        Case(name="searxng_down_youcom_rescues",
             inputs={"searxng_raises": True, "youcom_count": 4, "you_key": True},
             expected_output=["you.com"]),
        Case(name="searxng_down_no_key",
             inputs={"searxng_raises": True, "youcom_count": 4, "you_key": False},
             expected_output=[]),
    ], evaluators=[EqualsExpected()]), task_search_fallback, True))

    # -- extraction degradation ------------------------------------------------
    out.append(("extraction_degraded", Dataset(name="extraction_degraded", cases=[
        Case(name="snippet_available", inputs={"snippet": "useful snippet text"},
             expected_output="snippet_only"),
        Case(name="nothing_available", inputs={"snippet": ""},
             expected_output="failed"),
    ], evaluators=[EqualsExpected()]), task_extraction_degraded, True))

    # -- Ollama retry behaviour -------------------------------------------------
    out.append(("ollama_retry", Dataset(name="ollama_retry", cases=[
        Case(name="transient_then_success",
             inputs={"fail_times": 2, "max_retries": 3},
             expected_output={"attempts": 3, "ok": True}),
        Case(name="persistent_failure_gives_up",
             inputs={"fail_times": 99, "max_retries": 2},
             expected_output={"attempts": 3, "ok": False}),
    ], evaluators=[DictEqualsExpected()]), task_ollama_retry, True))

    # -- LLM-judged impact (only with real Ollama config) ----------------------
    llm_configured = bool(settings.ollama_api_key and settings.ollama_model)
    llm_cases = [c for c in cases["impact_classification"]]
    out.append(("impact_llm", Dataset(
        name="impact_llm",
        cases=[Case(name=c["name"], inputs=c["inputs"], expected_output=c["expected"])
               for c in llm_cases],
        evaluators=[EqualsExpected()]),
        lambda inputs: task_llm_impact(inputs, settings), llm_configured))

    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_evaluations(settings: Settings) -> int:
    """Run every dataset; print reports; return 0 iff everything passed."""
    print("\n=== FTSE 50 agent — Pydantic Evals ===\n")
    total_pass, total_fail, skipped = 0, 0, []

    for name, dataset, task, runnable in build_datasets(settings):
        if not runnable:
            skipped.append(name)
            print(f"--- {name}: SKIPPED (see log) ---\n")
            continue
        print(f"--- {name} ({len(dataset.cases)} cases) ---")
        # max_concurrency=1: several task harnesses patch module globals, so
        # cases within one dataset must not interleave.
        report = dataset.evaluate_sync(task, progress=False, max_concurrency=1)
        report.print()
        # Tally from the report's per-case assertion results.
        passed = failed = 0
        for rc in report.cases:
            ok = True
            if rc.assertions:
                ok = all(er.value for er in rc.assertions.values())
            if rc.evaluator_failures:
                ok = False
            passed, failed = passed + ok, failed + (not ok)
        failed += len(report.failures)  # cases whose task raised
        total_pass += passed
        total_fail += failed
        print(f"    -> {passed} passed, {failed} failed\n")

    print("=== Summary ===")
    print(f"Passed: {total_pass}   Failed: {total_fail}"
          + (f"   Skipped datasets: {', '.join(skipped)}" if skipped else ""))
    if total_fail:
        print("Some evaluations FAILED — see details above.")
        return 1
    print("All runnable evaluations passed.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(run_evaluations(Settings()))
