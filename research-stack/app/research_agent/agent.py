from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from .cache import Cache
from .chunking import chunk_document
from .citations import CitationTracker
from .config import Settings
from .extract import Extractor
from .llm import LLM
from .logutil import event
from .models import (Document, Passage, ResearchReport, SearchHit, SearchQuery, SourceRecord)
from .models_ml import Embedder, Reranker
from .planner import Plan, fallback_plan, parse_plan, route_query, VALID_CATEGORIES, VALID_RANGES
from .prompts import GAP_SYSTEM, PLANNER_SYSTEM, SYNTH_SYSTEM
from .searx_client import SearxClient, SearxError
from .store import VectorStore
from .urls import Deduper, hostname

log = logging.getLogger(__name__)


def to_queries(items: list[dict[str, Any]], limit: int) -> list[SearchQuery]:
    """Validate follow-up queries from the gap review (invalid categories/ranges are repaired)."""
    out: list[SearchQuery] = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        q = str(it.get("q", "")).strip()
        if not q:
            continue
        cat = it.get("category") if it.get("category") in VALID_CATEGORIES else "general"
        tr = it.get("time_range") if it.get("time_range") in VALID_RANGES else None
        out.append(route_query(q, cat, tr))
    return out


@dataclass
class RunState:
    """Everything collected during one run (never shared between runs)."""

    run_id: str
    sources: dict[str, SourceRecord] = field(default_factory=dict)
    hits: dict[str, SearchHit] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def touch(self, hit: SearchHit, sq: SearchQuery) -> None:
        rec = self.sources.get(hit.canonical_url)
        if rec is None:
            rec = SourceRecord(url=hit.url, title=hit.title, source=hostname(hit.url), engines=[],
                               published=hit.published, extraction="not_fetched", snippet=hit.snippet)
            self.sources[hit.canonical_url] = rec
            self.hits[hit.canonical_url] = hit
        rec.engines = sorted(set(rec.engines) | set(hit.engines))
        if sq.q not in rec.queries:
            rec.queries.append(sq.q)
        if sq.categories not in rec.categories:
            rec.categories.append(sq.categories)


class ResearchAgent:
    def __init__(self, cfg: Settings, *, cache: Cache | None = None, searx: SearxClient | None = None,
                 extractor: Extractor | None = None, llm: LLM | None = None, embedder: Embedder | None = None,
                 reranker: Reranker | None = None, store: VectorStore | None = None) -> None:
        """Components can be injected (shared model instances in a worker; fakes in tests)."""
        self.cfg = cfg
        self.cache = cache or Cache(cfg.valkey_url)
        self.searx = searx or SearxClient(cfg.searxng_url, self.cache, search_ttl=cfg.search_cache_ttl,
                                          news_ttl=cfg.news_cache_ttl, concurrency=cfg.search_concurrency)
        self.extractor = extractor or Extractor(
            self.cache, user_agent=cfg.user_agent, per_host_delay=cfg.per_host_delay,
            concurrency=cfg.fetch_concurrency, page_ttl=cfg.page_cache_ttl)
        self.llm = llm or LLM(cfg.llm_base_url, cfg.llm_model, cfg.llm_timeout)
        self.embedder = embedder or Embedder(cfg.embed_model, cfg.device)
        self.reranker = reranker or Reranker(cfg.rerank_model, cfg.device)
        self.store = store or VectorStore(cfg.qdrant_url, cfg.qdrant_collection, self.embedder.dim)

    async def startup(self) -> None:
        await self.store.ensure_collection()

    async def shutdown(self) -> None:
        for closer in (self.searx.close, self.extractor.close, self.llm.close, self.store.close, self.cache.close):
            await closer()

    # ---------- 1. plan ----------
    async def plan(self, question: str) -> Plan:
        """LLM plan (sub-questions + categories + queries); a deterministic plan if the LLM output is unusable."""
        try:
            data = await self.llm.chat_json(PLANNER_SYSTEM, f"Question: {question}")
        except (ValueError, KeyError, httpx.HTTPError) as exc:  # bad JSON, LLM down or timed out
            event(log, "plan_failed", level=logging.WARNING, error=type(exc).__name__)
            return fallback_plan(question, f"LLM planner unavailable ({type(exc).__name__})")
        plan = parse_plan(data, question, known_engines=await self.searx.known_engines(),
                          max_queries=self.cfg.max_total_queries)
        event(log, "plan", source=plan.source, sub_questions=len(plan.sub_questions),
              categories=[s.category for s in plan.sub_questions],
              time_ranges=[s.time_range or "none" for s in plan.sub_questions])
        return plan

    # ---------- 2. multi-query search ----------
    async def search_all(self, queries: list[SearchQuery], state: RunState | None = None,
                         round_no: int = 0) -> list[SearchHit]:
        """Run the queries concurrently (bounded by the client's semaphore + a per-query timeout).

        A failing engine, query or timeout is recorded as a warning; the remaining queries still contribute.
        """
        state = state or RunState(run_id="-")

        async def one(sq: SearchQuery) -> list[tuple[SearchQuery, SearchHit]]:
            try:
                resp = await asyncio.wait_for(self.searx.search(sq), timeout=self.cfg.search_timeout)
            except (SearxError, asyncio.TimeoutError, httpx.HTTPError) as exc:
                reason = "timed out" if isinstance(exc, asyncio.TimeoutError) else str(exc)[:120]
                state.warn(f"search failed for {sq.q!r} [{sq.categories}]: {reason}")
                event(log, "search_failed", level=logging.WARNING, round=round_no, query=sq.q,
                      category=sq.categories, error=type(exc).__name__)
                return []
            for name, why in resp.unresponsive_engines:
                state.warn(f"engine {name} unresponsive ({why})")
            engines = sorted({e for h in resp.hits for e in h.engines})
            event(log, "search", round=round_no, query=sq.q, category=sq.categories,
                  time_range=sq.time_range or "none", requested_engines=sq.engines or "category-default",
                  hits=len(resp.hits), engines=engines, unresponsive=len(resp.unresponsive_engines))
            return [(sq, h) for h in resp.hits[: self.cfg.results_per_query]]

        batches = await asyncio.gather(*(one(q) for q in queries))
        merged: dict[str, SearchHit] = {}
        for batch in batches:
            for sq, h in batch:
                state.touch(h, sq)
                prev = merged.get(h.canonical_url)
                if prev is None:
                    merged[h.canonical_url] = h
                else:  # same URL from several queries: it is probably important
                    prev.score += h.score
                    prev.engines = sorted(set(prev.engines) | set(h.engines))
        # SearXNG's score already rewards agreement between engines; add a bonus per extra engine.
        return sorted(merged.values(), key=lambda h: h.score + 0.5 * len(h.engines), reverse=True)

    def select_for_fetch(self, hits: list[SearchHit], dedup: Deduper) -> list[SearchHit]:
        """Top hits, skipping already-seen URLs and capping results per domain for diversity."""
        per_domain: Counter[str] = Counter()
        chosen: list[SearchHit] = []
        for h in hits:
            if dedup.seen_url(h.canonical_url):
                continue
            domain = hostname(h.canonical_url)
            if per_domain[domain] >= self.cfg.max_per_domain:
                continue
            per_domain[domain] += 1
            dedup.add_url(h.canonical_url)
            chosen.append(h)
            if len(chosen) >= self.cfg.max_pages_per_round:
                break
        return chosen

    # ---------- 3. fetch, extract, chunk, embed, store ----------
    async def _fetch(self, hit: SearchHit) -> Document | None:
        try:
            return await asyncio.wait_for(self.extractor.fetch(hit), timeout=self.cfg.fetch_timeout)
        except (asyncio.TimeoutError, Exception) as exc:  # one bad page must never fail the run
            log.info("fetch failed %s: %r", hit.url, exc)
            return None

    async def ingest(self, hits: list[SearchHit], dedup: Deduper, run_id: str,
                     state: RunState | None = None) -> list[Document]:
        state = state or RunState(run_id=run_id)
        results = await asyncio.gather(*(self._fetch(h) for h in hits))
        fresh: list[Document] = []
        failed = 0
        for hit, doc in zip(hits, results):
            rec = state.sources.get(hit.canonical_url)
            if doc is None:
                failed += 1
                if rec:
                    rec.extraction = "failed"
            elif dedup.is_duplicate_text(doc.text):
                if rec:
                    rec.extraction = "duplicate_text"
            else:
                fresh.append(doc)
                if rec:
                    rec.extraction = "extracted"
                    rec.published = doc.date or rec.published
        if failed:
            state.warn(f"{failed} of {len(hits)} selected pages could not be extracted in one round "
                       "(blocked, paywalled, timed out or empty); their search snippets are not full text")
        chunks = [c for d in fresh for c in chunk_document(d)]
        if chunks:
            vectors = await self.embedder.embed([c.text for c in chunks])
            await self.store.upsert(chunks, vectors, run_id)
        event(log, "ingest", selected=len(hits), extracted=len(fresh), failed=failed,
              duplicates=len(hits) - failed - len(fresh), chunks=len(chunks))
        return fresh

    # ---------- 4. retrieve + rerank ----------
    async def evidence_by_question(self, sub_questions: list[str], run_id: str) -> dict[str, list[Passage]]:
        """Per sub-question: dense recall from Qdrant (this run first, then the library), then rerank."""
        vectors = await self.embedder.embed(sub_questions)
        per_q = max(3, self.cfg.top_k_passages // max(1, len(sub_questions)))
        out: dict[str, list[Passage]] = {}
        for sq, vec in zip(sub_questions, vectors):
            cands = await self.store.search(vec, limit=30, run_id=run_id)
            cands += await self.store.search(vec, limit=10)  # older research in the library
            uniq = {(p.url, p.text[:80]): p for p in cands}
            out[sq] = await self.reranker.rerank(sq, list(uniq.values()), top_k=per_q)
        return out

    def merge_passages(self, by_q: dict[str, list[Passage]]) -> list[Passage]:
        picked: dict[tuple[str, str], Passage] = {}
        for passages in by_q.values():
            for p in passages:
                key = (p.url, p.text[:80])
                if key not in picked or picked[key].score < p.score:
                    picked[key] = p
        return sorted(picked.values(), key=lambda p: p.score, reverse=True)[: self.cfg.top_k_passages]

    async def evidence(self, sub_questions: list[str], run_id: str) -> list[Passage]:
        return self.merge_passages(await self.evidence_by_question(sub_questions, run_id))

    def coverage_detail(self, by_q: dict[str, list[Passage]]) -> list[str]:
        """Per sub-question "passages/sources/best-score" for the logs (explains why coverage is not met)."""
        out = []
        for i, (sq, ps) in enumerate(by_q.items(), 1):
            ft = [p for p in ps if p.kind == "fulltext"]
            good = [p for p in ft if p.score >= self.cfg.coverage_min_score]
            best = max((p.score for p in ft), default=0.0)
            out.append(f"sq{i}:passages={len(ft)},good={len(good)},sources={len({p.source for p in good})},best={best:.2f}")
        return out

    # ---------- 5. coverage + gap detection ----------
    def coverage(self, by_q: dict[str, list[Passage]]) -> dict[str, bool]:
        """A sub-question is covered when enough full-text passages from enough distinct sources score well."""
        cov: dict[str, bool] = {}
        for sq, passages in by_q.items():
            good = [p for p in passages if p.kind == "fulltext" and p.score >= self.cfg.coverage_min_score]
            cov[sq] = (len(good) >= self.cfg.coverage_min_passages
                       and len({p.source for p in good}) >= self.cfg.coverage_min_sources)
        return cov

    async def find_gaps(self, question: str, subs: list[str], passages: list[Passage]) -> dict[str, Any]:
        snippets = "\n".join(f"- ({p.source}) {p.text[:300]}" for p in passages)
        user = f"Question: {question}\nSub-questions: {subs}\n\nEvidence:\n{snippets or '(none yet)'}"
        try:
            return await self.llm.chat_json(GAP_SYSTEM, user)
        except (ValueError, KeyError, httpx.HTTPError) as exc:  # incl. LLM timeouts: keep going with what we have
            log.warning("gap check failed: %r", exc)
            return {"done": False, "gaps": [], "follow_up_queries": []}

    async def follow_up_queries(self, question: str, plan: Plan, uncovered: list[str],
                                passages: list[Passage]) -> tuple[list[SearchQuery], list[str]]:
        """Targeted searches for uncovered sub-questions. The LLM suggests wording; if it fails, the
        sub-question text itself is searched in that sub-question's category."""
        review = await self.find_gaps(question, uncovered, passages)
        gaps = [str(g) for g in review.get("gaps", []) if isinstance(g, str)] or list(uncovered)
        queries = to_queries(review.get("follow_up_queries", []) or [], self.cfg.queries_per_round)
        if not queries:
            by_text = {s.text: s for s in plan.sub_questions}
            for text in uncovered:
                sub = by_text.get(text)
                if sub:
                    queries.append(route_query(text, sub.category, sub.time_range, sub.engines))
        return queries[: self.cfg.queries_per_round], gaps

    # ---------- 6. synthesis ----------
    async def synthesize(self, question: str, passages: list[Passage]) -> tuple[str, CitationTracker]:
        tracker = CitationTracker()
        context = tracker.context_block(passages)
        user = f"Question: {question}\n\nSources:\n\n{context}"
        answer = await self.llm.chat(SYNTH_SYSTEM, user, temperature=0.2)
        return answer, tracker

    def snippet_passages(self, state: RunState, used_urls: set[str]) -> list[Passage]:
        """Search-result excerpts for pages whose text could not be extracted. Labelled as snippets so they
        are never presented as full-text evidence."""
        out: list[Passage] = []
        for key, rec in state.sources.items():
            if len(out) >= self.cfg.max_snippet_passages:
                break
            if rec.extraction != "failed" or not rec.snippet or rec.url in used_urls:
                continue
            rec.extraction = "snippet_only"
            out.append(Passage(text=rec.snippet, url=rec.url, title=rec.title, date=rec.published,
                               source=rec.source, engine=rec.engines[0] if rec.engines else "",
                               score=0.0, kind="snippet"))
        return out

    # ---------- the loop ----------
    async def run(self, question: str) -> ResearchReport:
        run_id = uuid.uuid4().hex[:12]
        state = RunState(run_id=run_id)
        dedup = Deduper()
        plan = await self.plan(question)
        state.warnings.extend(n for n in plan.notes if n not in state.warnings)
        subs = plan.texts()
        budget = self.cfg.max_total_queries
        queries = plan.all_queries(limit=min(self.cfg.queries_per_round, budget))
        queries_run: list[str] = []
        gaps: list[str] = []
        by_q: dict[str, list[Passage]] = {s: [] for s in subs}
        cov: dict[str, bool] = {s: False for s in subs}
        rounds = 0

        for rounds in range(1, self.cfg.max_rounds + 1):
            queries = [q for q in queries if q.q not in queries_run]  # never repeat a query
            queries = queries[: max(0, budget - len(queries_run))]      # total query budget
            if not queries:
                break
            queries_run += [q.q for q in queries]
            event(log, "round_start", round=rounds, queries=[q.q for q in queries],
                  categories=sorted({q.categories for q in queries}), budget_left=budget - len(queries_run))

            hits = await self.search_all(queries, state, rounds)
            new_docs = await self.ingest(self.select_for_fetch(hits, dedup), dedup, run_id, state)
            try:
                by_q = await self.evidence_by_question(subs, run_id)
            except Exception as exc:  # vector store / embedder trouble: keep what earlier rounds produced
                state.warn(f"evidence retrieval failed in round {rounds}: {type(exc).__name__}")
                event(log, "retrieval_failed", level=logging.WARNING, round=rounds, error=type(exc).__name__)
            cov = self.coverage(by_q)
            uncovered = [s for s in subs if not cov.get(s)]
            event(log, "round_end", round=rounds, new_docs=len(new_docs), covered=len(subs) - len(uncovered),
                  total=len(subs), detail=self.coverage_detail(by_q))
            if not uncovered:
                gaps = []
                break  # sufficient coverage
            gaps = list(uncovered)
            if rounds >= self.cfg.max_rounds or len(queries_run) >= budget or (not new_docs and rounds > 1):
                break  # round / query budget exhausted, or searching no longer yields anything new
            queries, llm_gaps = await self.follow_up_queries(
                question, plan, uncovered, self.merge_passages(by_q))
            gaps = llm_gaps

        passages = self.merge_passages(by_q)
        if any(not v for v in cov.values()):
            passages += self.snippet_passages(state, {p.url for p in passages})
        if not passages:
            state.warn("no usable evidence was retrieved; the answer can only state what is not covered")
        answer, tracker = await self.synthesize(question, passages)
        answer, cited = tracker.validate(answer)
        references = tracker.references_markdown(cited)
        if passages and not cited:
            # Small local models sometimes ignore the citation format. Say so, and show what the model was given
            # without pretending those sources back specific claims.
            state.warn("the model gave no valid inline citations; sources listed below were provided to it "
                       "but are not linked to individual claims")
            references = tracker.references_markdown(tracker.consulted(), heading="Sources consulted")
        event(log, "done", rounds=rounds, queries=len(queries_run), passages=len(passages),
              cited=len(cited), warnings=len(state.warnings),
              uncovered=sum(1 for v in cov.values() if not v))
        return ResearchReport(
            question=question,
            answer_markdown=answer + references,
            citations=cited,
            sub_questions=subs,
            rounds=rounds,
            queries_run=queries_run,
            open_gaps=gaps,
            plan=plan.describe(),
            sources=list(state.sources.values()),
            warnings=state.warnings,
            coverage=cov,
            plan_source=plan.source,
        )
