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
    clamp_max_results,
)
from utils.query_scope import requested_universe
from .evidence import (
    evidence_from_fetch,
    evidence_from_search,
    select_evidence,
)
from .llm_client import LLMClient, LLMError
from .planner import BASE_SYSTEM_PROMPT, QueryPlanner
from .tools import ToolExecutor, ToolRegistry, ToolValidationError, filter_and_rank
from .tools import SearchTool as _SearchTool  # noqa: F401 (re-export for tests)
from .verification import verify_answer

log = logging.getLogger(__name__)

_STEP_SYSTEM = BASE_SYSTEM_PROMPT + """\
You decide the next step of a research task. For this task you may call only \
these tools:
{tools}

Reply with structured output:
- action "call_tool" to use one of the tools above (give tool_name and arguments), or
- action "finish" when the evidence is sufficient to answer, or when further
  calls are unlikely to help.

Rules:
- Only request tools from the list above; never repeat an identical call.
- Cover the question from different angles: for recent developments, seek
  distinct aspects (announcements, investments/acquisitions, financial results
  and guidance, material operational changes) rather than re-running the same
  search. Each new query must be meaningfully different from the last.
- If the evidence found so far is thin or only covers one aspect, prefer
  another targeted search over finishing; absence of a result from a single
  search is NOT proof that no such result exists.
- Prefer fetching a promising article over yet another broad search when you
  already have candidates.
- Stop early only when the completion criteria are met or further calls are
  unlikely to help.
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
- answer: resolve the user's question directly in concise plain prose. Lead with
  the verified fact first (name, date, number), then at most one or two short
  sentences of essential context. Do NOT use Markdown formatting markers
  (**bold**, # headings) and do NOT put a numbered list in this field — the
  reader sees it as plain text. Keep it to one or two short paragraphs.
- key_points: the supporting findings, one development per item, each
  traceable to a source. Make each item self-contained and concise: state the
  development, its business/financial impact, and the publication or event date
  where the evidence provides one (write "undated" when it does not). Do not
  paste URLs here — citations are attached automatically from `sources` — and do
  not repeat the answer verbatim.
- sources: only sources you actually used, each with supporting_excerpt and
  supports_claims (indices into key_points the source supports).
- confidence: 0.0-1.0 reflecting evidence strength.
- limitations: concise caveats, one per item (missing dates, conflicts,
  approximate/relative timestamps, incompletely verified claims). Never invent
  dates or figures the evidence does not provide.
- verified: false when the evidence is insufficient — then answer must say
  the answer could not be verified.

Evidence type matters: a "search snippet" is a short excerpt — only use it for
the fact it directly states. A "full article" carries the complete text. When a
date is relative ("5 days ago"), describe it as approximate; never convert a
relative timestamp into a specific calendar date.

Consistency and honesty rules (they override any urge to sound complete):
- Respect any explicit count the user asked for (e.g. "no more than 2
  results"). Never present more distinct results/sources than requested.
- Count *distinct developments*, not sources: several outlets describing the
  same transaction are ONE development. Do not list a milestone of an earlier
  deal as a separate development.
- If fewer distinct results are supported than requested, state that plainly
  and explain the evidence limitation. Say only that no further development
  could be *confirmed from the retrieved evidence* — never assert that no
  other development exists, because search coverage may be incomplete.
- If the question is scoped to a market universe (e.g. FTSE), use only evidence
  about that universe; ignore unrelated global/US rankings that do not answer
  the question.
- For market-cap or other financial rankings: present a value only when the
  evidence states the metric, the currency/unit and the as-of date, and the
  values are comparable. If any of these are missing, do not rank — give a
  partial result and say which evidence is missing. Never invent a unit, date
  or figure, and never describe a source as providing a verified ranking when
  the pipeline could not verify it.
- A source being relevant does not make its claim verified. Keep facts and
  interpretation distinct.
""",
            output_type=QueryAnswer,
            run_id=self.run_id,
        )

    def _prompt(self, query: str, plan: ResearchPlan, evidence: list[dict]) -> str:
        blocks = []
        for i, e in enumerate(evidence):
            date = e.get("published_at") or "unknown date"
            kind = "full article" if e.get("kind") == "article" else "search snippet"
            blocks.append(
                f"[{i}] {e.get('title', 'untitled')}\n"
                f"    URL: {e.get('url', '')}\n"
                f"    Source: {e.get('source', 'unknown')} | Date: {date} | Type: {kind}\n"
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
    def validate(answer: QueryAnswer, evidence_urls: set[str], *,
                 query: str = "", plan: ResearchPlan | None = None,
                 evidence: list[dict] | None = None,
                 max_results: int | None = None) -> QueryAnswer:
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
        # Evidence-consistency checks (market-cap units/dates, requested
        # universe, figures absent from the evidence). Only runs when the
        # caller supplies the evidence context.
        if evidence is not None and query:
            for lim in verify_answer(answer, query=query, evidence=evidence,
                                     max_results=max_results, plan=plan):
                log.info("ResponseParser: %s", lim)
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
        # Global analysis cap: max distinct articles retained for this run.
        # (Not a per-search limit — see the Maximum-results contract in
        # utils/models.py.)
        requested_max = max_results
        max_results = clamp_max_results(max_results)
        if max_results != requested_max:
            log.warning("[%s] max_results=%r out of range; normalized to %d",
                        self.run_id, requested_max, max_results)
        # Per-search retrieval is governed by its own limit, independent of the
        # global analysis cap, so a small cap can still discover several
        # candidates to choose from.
        per_search = max(1, min(10, self.settings.search_results_per_query))
        started = datetime.now(timezone.utc)
        warnings: list[str] = []
        tool_trace: list[ToolTraceEntry] = []
        # Candidate pool: everything retrieved this run (URL-deduped), bounded
        # only by the tool budget. The global cap is applied by select_evidence.
        candidates: list[dict] = []
        candidate_urls: set[str] = set()
        evidence: list[dict] = []

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
            from nodes.search import normalize_url
            for e in ev_list:
                url = e.get("url", "")
                key = normalize_url(url) if url else ""
                if key and key not in candidate_urls:
                    candidate_urls.add(key)
                    candidates.append(e)

        # --- direct fetch for summarization tasks (no broad search) -----
        url_m = _URL_RE.search(query)
        if (plan.task_type == TaskType.SUMMARIZATION and url_m
                and "fetch_article" in plan.tools_allowed
                and calls < plan.max_tool_calls):
            url = url_m.group(0).rstrip(").,")
            obs = await _call("fetch_article", {"url": url})
            if obs.get("ok") and (obs.get("content") or "").strip():
                _ingest([self._evidence_from_fetch(obs)])

        # --- planned searches -------------------------------------------
        # Run every planned query (bounded by the tool budget): search breadth
        # discovers distinct developments, and the global cap is applied later.
        if "web_search" in plan.tools_allowed:
            for q in plan.search_queries:
                if calls >= plan.max_tool_calls:
                    break
                obs = await _call("web_search", {
                    "query": q, "max_results": per_search,
                    "time_range": time_range})
                if obs.get("notes"):
                    warnings.extend(obs["notes"])
                _ingest([self._evidence_from_search(r) for r in obs.get("results", [])])

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
                if obs.get("notes"):
                    warnings.extend(obs["notes"])
                _ingest([self._evidence_from_search(r)
                         for r in obs.get("results", [])])
            elif (step.tool_name == "fetch_article" and obs.get("ok")
                  and (obs.get("content") or "").strip()):
                _ingest([self._evidence_from_fetch(obs)])

        # --- enforce the global analysis cap ---------------------------
        # The run may have collected more candidates than the user allowed;
        # only ``max_results`` distinct articles are analysed and citable.
        universe = requested_universe(query)
        # Score relevance against the research queries, not the user's
        # instruction-laden prose, so "return no more than 2 ... including
        # URLs" style wording does not dilute term overlap.
        relevance_text = " ".join(plan.search_queries) or query
        evidence = select_evidence(candidates, query, max_results,
                                   universe=universe,
                                   relevance_text=relevance_text)
        candidates_considered = len(candidates)
        if candidates_considered > len(evidence):
            log.info("[%s] evidence capped %d candidate(s) -> %d (max_results=%d)",
                     self.run_id, candidates_considered, len(evidence),
                     max_results)
        evidence_urls = {e.get("url", "") for e in evidence if e.get("url")}

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
        answer = self.parser.validate(
            draft, evidence_urls, query=query, plan=plan, evidence=evidence,
            max_results=max_results)

        counts = {
            "max_results": max_results,
            "tool_calls": calls,
            "candidates_considered": candidates_considered,
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
        return evidence_from_search(r)

    @staticmethod
    def _evidence_from_fetch(obs: dict) -> dict:
        return evidence_from_fetch(obs)
