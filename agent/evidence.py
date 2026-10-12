"""Evidence assembly for the agentic research loop.

Converts raw tool observations into normalized evidence dictionaries, then
de-duplicates (canonical URL + near-duplicate story) and selects the final,
bounded evidence set.

The selection step is where the ``max_results`` contract is enforced: the run
may *collect* as many candidates as the search budget yields, but only up to
``max_results`` distinct articles are retained as analysed evidence. This keeps
search breadth (how many searches run) separate from the analysis cap (how
many distinct results are used), so the agent can still discover several
developments and then choose the best ones.
"""

from __future__ import annotations

from nodes.search import normalize_url
from utils.query_scope import mentions_universe
from utils.source_quality import is_section_page
from utils.story_dedup import (
    dedup_stories,
    headline_tokens,
    jaccard,
    significant_tokens,
)

# Diversity knobs for final selection: prefer items whose *novel* terms (terms
# not already in the query) differ, so paraphrased copies of one transaction do
# not crowd out a second distinct development. Only applies when there is
# enough signal to judge (>= _DIVERSITY_MIN_TOKENS novel terms).
_DIVERSITY_THRESHOLD = 0.5
_DIVERSITY_MIN_TOKENS = 3


def evidence_from_search(r: dict) -> dict:
    """Normalize one web_search result into an evidence dict."""
    return {
        "title": r.get("title", ""),
        "url": r.get("url", ""),
        "source": r.get("source", ""),
        "published_at": r.get("published_at"),
        "text": r.get("snippet", ""),
        "source_quality": r.get("source_quality", 0.0),
        "kind": "search",
    }


def evidence_from_fetch(obs: dict) -> dict:
    """Normalize one fetch_article observation into an evidence dict."""
    return {
        "title": obs.get("title", "") or obs.get("url", ""),
        "url": obs.get("url", ""),
        "source": obs.get("source", ""),
        "published_at": obs.get("published_at"),
        "text": obs.get("content", ""),
        "source_quality": 0.5,
        "kind": "article",
    }


def evidence_text(e: dict) -> str:
    return f"{e.get('title', '')} {e.get('text', '')}"


def is_full_article(e: dict) -> bool:
    return e.get("kind") == "article"


def dedup_by_url_evidence(items: list[dict]) -> list[dict]:
    """Collapse identical/canonical URLs, keeping the first occurrence."""
    seen: set[str] = set()
    out: list[dict] = []
    for e in items:
        url = (e.get("url") or "").strip()
        key = normalize_url(url) if url else ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(e)
    return out


def _overlap_ratio(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    low = text.lower()
    hits = sum(1 for t in terms if t in low)
    return hits / len(terms)


def _hub_penalty(e: dict) -> float:
    """Undated section/index pages are not developments; penalize them."""
    if e.get("published_at"):
        return 0.0
    return 0.3 if is_section_page(e.get("title", ""), e.get("text", "")) else 0.0


def _score(e: dict, terms: list[str], universe: str | None) -> float:
    score = float(e.get("source_quality") or 0.0)
    score += 0.5 * _overlap_ratio(terms, evidence_text(e))
    if is_full_article(e):
        score += 0.15
    # Dated items are strongly preferred: recency is central to news queries.
    score += 0.2 if e.get("published_at") else 0.0
    score -= _hub_penalty(e)
    if universe and mentions_universe(evidence_text(e), universe):
        score += 0.25
    return score


def _novel_tokens(title: str, query_terms: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in headline_tokens(title) if t not in query_terms)


def _diverse_select(ranked: list[dict], limit: int,
                    query_terms: frozenset[str]) -> list[dict]:
    """Greedy MMR-style pick that spreads selection across distinct stories."""
    selected: list[dict] = []
    selected_novel: list[frozenset[str]] = []
    deferred: list[dict] = []
    for e in ranked:
        toks = _novel_tokens(e.get("title", ""), query_terms)
        too_similar = (
            len(toks) >= _DIVERSITY_MIN_TOKENS
            and any(len(k) >= _DIVERSITY_MIN_TOKENS
                    and jaccard(toks, k) >= _DIVERSITY_THRESHOLD
                    for k in selected_novel)
        )
        if too_similar:
            deferred.append(e)
            continue
        selected.append(e)
        if len(toks) >= _DIVERSITY_MIN_TOKENS:
            selected_novel.append(toks)
        if len(selected) >= limit:
            return selected
    # No distinct candidates left: top up with the deferred ones.
    for e in deferred:
        if len(selected) >= limit:
            break
        selected.append(e)
    return selected


def select_evidence(items: list[dict], query: str, limit: int,
                    *, universe: str | None = None,
                    relevance_text: str | None = None) -> list[dict]:
    """Rank distinct evidence and retain at most ``limit`` items.

    URL de-duplication happens first, then ranking (source quality, query
    relevance, full text over snippets, dated items, on-universe coverage),
    then near-duplicate-story de-duplication so the highest-ranked copy of a
    syndicated story survives, then diversity-aware selection so a second
    distinct development is not crowded out by paraphrased copies of the
    first. The cap counts *distinct* articles.

    ``relevance_text`` (e.g. the planner's search queries) is used for the
    relevance signal so instruction-laden user prose does not dilute it.
    """
    if limit <= 0:
        return []
    distinct = dedup_by_url_evidence(items)
    terms = significant_tokens(relevance_text or query)
    ranked = sorted(distinct, key=lambda e: _score(e, terms, universe),
                    reverse=True)
    ranked = dedup_stories(ranked, title_getter=lambda e: e.get("title", ""))
    return _diverse_select(ranked, limit, frozenset(terms))
