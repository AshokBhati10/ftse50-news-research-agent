"""Agentic query research: plan -> tool loop -> synthesize -> validate -> report.

:class:`ResearchAgent` coordinates the lifecycle. The model *requests* tools;
the application validates and executes them. Every run produces a direct,
evidence-backed :class:`QueryAnswer` — search results support the answer,
they never replace it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone

from config.settings import Settings
from utils.cache import CacheManager
from utils.models import (
    AgentStep,
    QueryAnswer,
    ResearchPlan,
    SourceRecord,
    TaskType,
    ToolTraceEntry,
)
from .llm_client import LLMClient, LLMError
from .planner import BASE_SYSTEM_PROMPT, QueryPlanner
from .tools import ToolExecutor, ToolRegistry, ToolValidationError, filter_and_rank
from .tools import SearchTool as _SearchTool  # noqa: F401 (re-export for tests)

log = logging.getLogger(__name__)

_STEP_SYSTEM = BASE_SYSTEM_PROMPT + """\
You decide the next step of a research task. For this task you may call only \
these tools:
{tools}

Reply with structured output:
- action "call_tool" to use one of the tools above (give tool_name and arguments), or
- action "finish" when the evidence is sufficient to answer, or when further
  calls are unlikely to help.

Rules: only request tools from the list above; never repeat an identical call;
prefer fetching a promising article over another broad search; stop early when
the completion criteria are met.
"""


class ResearchAnalyzer:
    """Synthesizes retrieved evidence into a direct, cited answer."""

    def __init__(self, settings: Settings, run_id: str = "") -> None:
        self.settings = settings
        self.run_id = run_id

    def _llm(self) -> LLMClient:
        return LLMClient(
            self.settings,
            system_prompt=BASE_SYSTEM_PROMPT + """\
You synthesize research evidence into a direct answer. Output structured data:
- answer: resolve the user's question directly and concisely. For factual
  questions give the verified fact FIRST (name, date, number), then brief
  context. Never output a bare list of search results.
- key_points: the supporting facts, each traceable to a source.
- sources: only sources you actually used, each with supporting_excerpt and
  supports_claims (indices into key_points the source supports).
- confidence: 0.0-1.0 reflecting evidence strength.
- limitations: what could not be verified, conflicts, date caveats.
- verified: false when the evidence is insufficient — then answer must say
  the answer could not be verified.
""",
            output_type=QueryAnswer,
            run_id=self.run_id,
        )

    def _prompt(self, query: str, plan: ResearchPlan, evidence: list[dict]) -> str:
        blocks = []
        for i, e in enumerate(evidence):
            date = e.get("published_at") or "unknown date"
            blocks.append(
                f"[{i}] {e.get('title', 'untitled')}\n"
                f"    URL: {e.get('url', '')}\n"
                f"    Source: {e.get('source', 'unknown')} | Date: {date}\n"
                f"    {e.get('text', '')[:1500]}"
            )
        return (
            f"User's question: {query}\n"
            f"Task type: {plan.task_type.value}\n"
            f"Expected format: {plan.expected_answer_format or 'direct answer first'}\n"
            f"Completion criteria: {plan.completion_criteria}\n\n"
            f"Evidence ({len(evidence)} items):\n"
            + ("\n\n".join(blocks) if blocks else "(no evidence retrieved)")
            + "\n\nSynthesize the answer now."
        )

    async def synthesize(self, query: str, plan: ResearchPlan,
                         evidence: list[dict]) -> QueryAnswer:
        if not evidence:
            return QueryAnswer.unverified(
                query, plan.task_type,
                ["No evidence could be retrieved for this query."])
        draft = await self._llm().run(self._prompt(query, plan, evidence))
        draft.query = query.strip()
        draft.task_type = plan.task_type
        return draft


class ResponseParser:
    """Validates and normalizes the analyzer's structured output."""

    @staticmethod
    def validate(answer: QueryAnswer, evidence_urls: set[str]) -> QueryAnswer:
        """Enforce answer/source integrity; degrade honestly on violations."""
        # Sources must come from retrieved evidence — never fabricated.
        kept: list[SourceRecord] = []
        dropped = 0
        for s in answer.sources:
            if s.url in evidence_urls:
                kept.append(s)
            else:
                dropped += 1
        if dropped:
            log.warning("ResponseParser: dropped %d fabricated source(s)", dropped)
        answer.sources = kept
        # supports_claims must index into key_points.
        n = len(answer.key_points)
        for s in answer.sources:
            s.supports_claims = [i for i in s.supports_claims if 0 <= i < n]
        # An unverified answer must not claim verification.
        if not answer.verified:
            answer.confidence = 0.0
        if not kept and answer.verified and answer.key_points:
            # Claims without any surviving citation: lower confidence, flag it.
            answer.confidence = min(answer.confidence, 0.4)
            answer.limitations.append(
                "Some claims could not be matched to retrieved sources.")
        return answer


class AgentResult:
    """Everything a ResearchAgent run produced."""

    def __init__(self, *, query: str, plan: ResearchPlan, answer: QueryAnswer,
                 findings: list, counts: dict, warnings: list[str],
                 tool_trace: list[ToolTraceEntry],
                 evidence: list[dict] | None = None) -> None:
        self.query = query
        self.plan = plan
        self.answer = answer
        self.findings = findings
        self.counts = counts
        self.warnings = warnings
        self.tool_trace = tool_trace
        self.evidence = evidence or []


_URL_RE = re.compile(r"https?://\S+")


class ResearchAgent:
    """Coordinates the research lifecycle for one user query."""

    def __init__(self, settings: Settings,
                 cache: CacheManager | None = None,
                 run_id: str = "") -> None:
        self.settings = settings
        self.cache = cache
        self.run_id = run_id or "-"
        self.planner = QueryPlanner(settings, run_id=self.run_id)
        self.registry = ToolRegistry(settings, cache)
        self.analyzer = ResearchAnalyzer(settings, run_id=self.run_id)
        self.parser = ResponseParser()

    # -- public API ------------------------------------------------------
    async def research(
        self,
        query: str,
        *,
        max_results: int = 20,
        time_range: str = "month",
        fresh: bool = False,
        on_stage: Callable[[str, dict], None] | None = None,
    ) -> AgentResult:
        query = query.strip()
        if not query:
            raise ValueError("Query must not be empty")
        started = datetime.now(timezone.utc)
        warnings: list[str] = []
        tool_trace: list[ToolTraceEntry] = []
        evidence: list[dict] = []
        evidence_urls: set[str] = set()

        def _emit(stage: str, info: dict) -> None:
            if on_stage:
                on_stage(stage, info)

        # --- plan -------------------------------------------------------
        plan = await self.planner.plan(query)
        _emit("plan", {"task_type": plan.task_type.value,
                       "tools": plan.tools_allowed,
                       "max_tool_calls": plan.max_tool_calls})

        executor = ToolExecutor(self.registry)
        # Give the search tool this run's cache/freshness behavior.
        search_def = self.registry.get("web_search")
        if search_def is not None:
            search_def.instance.cache = self.cache
            search_def.instance.fresh = fresh

        calls = 0

        async def _call(tool_name: str, arguments: dict) -> dict:
            nonlocal calls
            calls += 1
            try:
                obs = await executor.execute(tool_name, arguments)
                ok = bool(obs.get("ok"))
                summary = self._obs_summary(tool_name, obs)
            except ToolValidationError as exc:
                ok, obs, summary = False, {"ok": False}, str(exc)
                log.warning("[%s] tool validation: %s", self.run_id, exc)
            tool_trace.append(ToolTraceEntry(
                tool=tool_name,
                arguments_summary=self._args_summary(tool_name, arguments),
                observation_summary=summary, ok=ok))
            _emit("tool", {"tool": tool_name, "call": calls,
                           "max_calls": plan.max_tool_calls, "ok": ok})
            return obs

        def _ingest(ev_list: list[dict]) -> None:
            for e in ev_list:
                url = e.get("url", "")
                if url and url not in evidence_urls:
                    evidence_urls.add(url)
                    evidence.append(e)

        # --- direct fetch for summarization tasks (no broad search) -----
        url_m = _URL_RE.search(query)
        if (plan.task_type == TaskType.SUMMARIZATION and url_m
                and "fetch_article" in plan.tools_allowed
                and calls < plan.max_tool_calls):
            url = url_m.group(0).rstrip(").,")
            obs = await _call("fetch_article", {"url": url})
            if obs.get("ok"):
                _ingest([self._evidence_from_fetch(obs)])

        # --- planned searches -------------------------------------------
        if "web_search" in plan.tools_allowed:
            for q in plan.search_queries:
                if calls >= plan.max_tool_calls:
                    break
                obs = await _call("web_search", {
                    "query": q, "max_results": min(10, max_results),
                    "time_range": time_range})
                if obs.get("notes"):
                    warnings.extend(obs["notes"])
                _ingest([self._evidence_from_search(r) for r in obs.get("results", [])])
                if calls >= 2 and len(evidence) >= max_results:
                    break  # enough material; save the remaining budget

        # --- model-driven follow-ups ------------------------------------
        # A tool request outside the plan's allowlist is a recoverable model
        # mistake: it is not executed, but the loop continues with the
        # remaining budget (bounded rejections prevent spinning in place).
        rejections = 0
        while calls < plan.max_tool_calls and plan.needs_web_research:
            step = await self._next_step(query, plan, tool_trace, calls)
            if step.action == "finish":
                log.info("[%s] agent finished after %d tool call(s): %s",
                         self.run_id, calls, step.reason)
                break
            if step.tool_name not in plan.tools_allowed \
                    or self.registry.get(step.tool_name) is None:
                rejections += 1
                warnings.append(f"Agent requested disallowed tool "
                                f"{step.tool_name!r}; request skipped.")
                tool_trace.append(ToolTraceEntry(
                    tool=step.tool_name,
                    arguments_summary=self._args_summary(step.tool_name,
                                                         step.arguments or {}),
                    observation_summary="rejected: tool not allowed by plan",
                    ok=False))
                log.warning("[%s] rejected disallowed tool request %r",
                            self.run_id, step.tool_name)
                if rejections >= 2:
                    warnings.append("Stopped after repeated disallowed tool "
                                    "requests.")
                    break
                continue
            obs = await _call(step.tool_name, step.arguments or {})
            if step.tool_name == "web_search":
                _ingest([self._evidence_from_search(r)
                         for r in obs.get("results", [])])
            elif step.tool_name == "fetch_article" and obs.get("ok"):
                _ingest([self._evidence_from_fetch(obs)])

        # --- synthesize + validate --------------------------------------
        _emit("synthesize", {"evidence_items": len(evidence)})
        try:
            draft = await self.analyzer.synthesize(query, plan, evidence)
        except LLMError as exc:
            warnings.append(f"Answer synthesis failed: {exc}")
            draft = QueryAnswer.unverified(
                query, plan.task_type,
                ["The language model failed while synthesizing the answer."])
        draft.tool_trace = tool_trace
        _emit("validate", {})
        answer = self.parser.validate(draft, evidence_urls)

        counts = {
            "max_results": max_results,
            "tool_calls": calls,
            "evidence_items": len(evidence),
            "sources_cited": len(answer.sources),
        }
        _emit("report", {"sources": len(answer.sources)})
        log.info("[%s] research done: %d tool calls, %d evidence, verified=%s",
                 self.run_id, calls, len(evidence), answer.verified)
        return AgentResult(
            query=query, plan=plan, answer=answer, findings=[],
            counts=counts, warnings=warnings, tool_trace=tool_trace,
            evidence=evidence,
        )

    # -- model-driven step ----------------------------------------------
    async def _next_step(self, query: str, plan: ResearchPlan,
                         trace: list[ToolTraceEntry], calls: int) -> AgentStep:
        # Advertise only the tools the plan allows, so the model's tool choice
        # and the plan's allowlist cannot diverge.
        allowed = [t for t in plan.tools_allowed if self.registry.get(t)]
        llm = LLMClient(
            self.settings,
            system_prompt=_STEP_SYSTEM.format(
                tools=self.registry.describe(allowed or list(plan.tools_allowed))),
            output_type=AgentStep,
            run_id=self.run_id,
        )
        history = "\n".join(
            f"- {t.tool}({t.arguments_summary}) -> {t.observation_summary}"
            for t in trace[-6:]
        ) or "(no tool calls yet)"
        prompt = (
            f"User's question: {query}\n"
            f"Task: {plan.task_type.value} — {plan.intent_summary}\n"
            f"Completion criteria: {plan.completion_criteria}\n"
            f"Tool calls used: {calls}/{plan.max_tool_calls}\n\n"
            f"History:\n{history}\n\n"
            f"Decide the next step."
        )
        try:
            return await llm.run(prompt)
        except LLMError as exc:
            log.warning("[%s] step decision failed (%s); finishing", self.run_id, exc)
            return AgentStep(action="finish", reason="Step planner unavailable.")

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _args_summary(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return f"query={arguments.get('query', '')!r:.80}"
        if tool_name == "fetch_article":
            return f"url={arguments.get('url', '')[:80]}"
        return str(arguments)[:120]

    @staticmethod
    def _obs_summary(tool_name: str, obs: dict) -> str:
        if obs.get("duplicate"):
            return "duplicate call skipped"
        if tool_name == "web_search":
            n = len(obs.get("results", []))
            prov = ",".join(obs.get("providers", []))
            return f"{n} results ({prov})"
        if tool_name == "fetch_article":
            if obs.get("ok"):
                return f"fetched {len(obs.get('content', ''))} chars"
            return f"failed: {obs.get('error', 'unknown')}"
        return "ok" if obs.get("ok") else "failed"

    @staticmethod
    def _evidence_from_search(r: dict) -> dict:
        return {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": r.get("source", ""),
            "published_at": r.get("published_at"),
            "text": r.get("snippet", ""),
            "source_quality": r.get("source_quality", 0.0),
            "kind": "search",
        }

    @staticmethod
    def _evidence_from_fetch(obs: dict) -> dict:
        return {
            "title": obs.get("title", "") or obs.get("url", ""),
            "url": obs.get("url", ""),
            "source": obs.get("source", ""),
            "published_at": obs.get("published_at"),
            "text": obs.get("content", ""),
            "source_quality": 0.5,
            "kind": "article",
        }
