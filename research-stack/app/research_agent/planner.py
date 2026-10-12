"""Query planning and category routing.

The LLM breaks a question into focused sub-questions, each with a SearXNG
category (general / news / science / it), search queries and an optional time
constraint. LLM output is treated as untrusted: every field is validated and
anything invalid is repaired or replaced by a deterministic heuristic, so a bad
or missing LLM answer never stops a research run.

Routing reuses the SearXNG instance's own configuration: queries are sent with
the `categories` parameter (the engines in each category are defined once, in
searxng/settings.yml). An explicit `engines` list is honoured only for engine
names the instance actually reports, never from a hardcoded list here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, cast

from .models import Category, SearchQuery

log = logging.getLogger(__name__)

VALID_CATEGORIES: tuple[str, ...] = ("general", "news", "science", "it")
VALID_RANGES: tuple[str, ...] = ("day", "week", "month", "year")
MIN_SUBQUESTIONS = 2
MAX_SUBQUESTIONS = 5
MAX_QUERIES_PER_SUBQUESTION = 3

_NEWS = re.compile(
    r"\b(latest|recent(?:ly)?|today|yesterday|this (?:week|month)|past (?:week|month|day)|last (?:week|month)|"
    r"news|announce[sd]?|announcement|headline|breaking|earnings|buyback|acquisition|acquire[sd]?|"
    r"merger|ipo|lawsuit|shares?|stock|market|launch(?:ed|es)?|ceo|appoint(?:ed|ment))\b", re.I)
_SCIENCE = re.compile(
    r"\b(study|studies|paper|papers|research|arxiv|pubmed|clinical|trial|peer[- ]reviewed|literature|"
    r"survey|benchmark|method|methods|algorithm|theorem|hypothesis|dataset|journal|meta-analysis|"
    r"faithfulness|evaluation metrics?)\b", re.I)
_IT = re.compile(
    r"\b(python|javascript|typescript|rust|golang|java|docker|kubernetes|linux|api|sdk|library|libraries|"
    r"framework|github|stack ?overflow|npm|pip|pypi|compiler|stack trace|exception|error message|"
    r"regex|sql|postgres|redis|http|css|html|bug|install)\b", re.I)

_RANGE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(today|yesterday|past 24 hours|last 24 hours|past day|last day)\b", re.I), "day"),
    (re.compile(r"\b(this week|past week|last week|past 7 days|last 7 days)\b", re.I), "week"),
    (re.compile(r"\b(this month|past month|last month|past 30 days|last 30 days|past few weeks)\b", re.I), "month"),
    (re.compile(r"\b(this year|past year|last year|past 12 months|last 12 months)\b", re.I), "year"),
)


@dataclass
class SubQuestion:
    text: str
    category: Category = "general"
    time_range: str | None = None
    queries: list[str] = field(default_factory=list)
    engines: list[str] | None = None

    def search_queries(self) -> list[SearchQuery]:
        """Routable SearXNG queries for this sub-question (category + time constraint)."""
        return [route_query(q, self.category, self.time_range, self.engines) for q in self.queries]


@dataclass
class Plan:
    sub_questions: list[SubQuestion]
    source: str = "llm"  # "llm" | "llm-repaired" | "fallback"
    notes: list[str] = field(default_factory=list)

    def all_queries(self, limit: int | None = None) -> list[SearchQuery]:
        out: list[SearchQuery] = []
        seen: set[str] = set()
        # Interleave so a small query budget still touches every sub-question.
        depth = max((len(s.queries) for s in self.sub_questions), default=0)
        for i in range(depth):
            for sub in self.sub_questions:
                if i < len(sub.queries):
                    sq = route_query(sub.queries[i], sub.category, sub.time_range, sub.engines)
                    key = sq.q.lower()
                    if key not in seen:
                        seen.add(key)
                        out.append(sq)
        return out[:limit] if limit else out

    def texts(self) -> list[str]:
        return [s.text for s in self.sub_questions]

    def describe(self) -> list[dict[str, Any]]:
        return [{"sub_question": s.text, "category": s.category, "time_range": s.time_range,
                 "queries": list(s.queries), "engines": list(s.engines or [])} for s in self.sub_questions]


# ------------------------------------------------------------------ heuristics

def classify_category(text: str) -> Category:
    """Deterministic category guess used when the LLM gives none or an invalid one."""
    scores = {"news": len(_NEWS.findall(text)), "science": len(_SCIENCE.findall(text)), "it": len(_IT.findall(text))}
    best = max(scores, key=lambda k: scores[k])
    return cast(Category, best) if scores[best] > 0 else "general"


def infer_time_range(text: str) -> str | None:
    """Time constraint explicitly stated in the text ("past month" -> "month"); None otherwise."""
    for pattern, rng in _RANGE_PATTERNS:
        if pattern.search(text):
            return rng
    return None


def route_query(q: str, category: str, time_range: str | None, engines: list[str] | None = None) -> SearchQuery:
    cat = category if category in VALID_CATEGORIES else "general"
    rng = time_range if time_range in VALID_RANGES else None
    return SearchQuery(q=q.strip(), categories=cast(Category, cat), time_range=cast(Any, rng),
                       engines=engines or None)


# ------------------------------------------------------------------ validation

def _clean_text(v: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", v).strip()[:limit] if isinstance(v, str) else ""


def _queries_from(raw: Any) -> list[tuple[str, str | None]]:
    """Accept ["q", ...] or [{"q": "...", "time_range": ...}, ...]; returns (query, time_range) pairs."""
    out: list[tuple[str, str | None]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            q, tr = _clean_text(item), None
        elif isinstance(item, dict):
            q, tr = _clean_text(item.get("q") or item.get("query")), item.get("time_range")
        else:
            continue
        if q:
            out.append((q, tr if tr in VALID_RANGES else None))
    return out


def _valid_engines(raw: Any, known: Iterable[str] | None) -> list[str] | None:
    """Keep only engines the SearXNG instance reports; without that list, ignore engine hints."""
    if not isinstance(raw, list) or known is None:
        return None
    known_set = {k.lower(): k for k in known}
    kept = [known_set[e.lower()] for e in raw if isinstance(e, str) and e.lower() in known_set]
    return kept or None


def fallback_plan(question: str, reason: str = "") -> Plan:
    """Deterministic plan: the question itself, routed by keyword heuristics. Always valid."""
    q = _clean_text(question, 400) or "research question"
    category = classify_category(q)
    time_range = infer_time_range(q)
    sub = SubQuestion(text=q, category=category, time_range=time_range, queries=[q])
    return Plan([sub], source="fallback", notes=[f"planner fallback: {reason}"] if reason else ["planner fallback"])


def parse_plan(data: Any, question: str, *, known_engines: Iterable[str] | None = None,
               max_queries: int = 10) -> Plan:
    """Validate/repair an LLM planner response into a Plan (never raises).

    Accepted shapes:
      new:    {"sub_questions": [{"question", "category", "time_range", "queries": [...]}]}
      legacy: {"sub_questions": ["..."], "queries": [{"q", "category", "time_range"}]}
    """
    if not isinstance(data, dict):
        return fallback_plan(question, "planner output was not a JSON object")
    raw_subs = data.get("sub_questions")
    if not isinstance(raw_subs, list) or not raw_subs:
        return fallback_plan(question, "planner output had no sub_questions")

    notes: list[str] = []
    subs: list[SubQuestion] = []
    legacy_queries = data.get("queries") if isinstance(data.get("queries"), list) else []

    for raw in raw_subs[:MAX_SUBQUESTIONS]:
        if isinstance(raw, str):
            text, cat_raw, tr_raw, engines_raw, qs = _clean_text(raw), None, None, None, []
        elif isinstance(raw, dict):
            text = _clean_text(raw.get("question") or raw.get("text") or raw.get("sub_question"))
            cat_raw, tr_raw = raw.get("category"), raw.get("time_range")
            engines_raw, qs = raw.get("engines"), _queries_from(raw.get("queries"))
        else:
            continue
        if not text:
            continue
        category = cat_raw if cat_raw in VALID_CATEGORIES else classify_category(text)
        if cat_raw is not None and cat_raw not in VALID_CATEGORIES:
            notes.append(f"invalid category {cat_raw!r} for {text[:40]!r}; used {category!r}")
        time_range = tr_raw if tr_raw in VALID_RANGES else infer_time_range(text)
        queries: list[str] = []
        for q, q_range in qs:
            time_range = time_range or q_range
            if q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        subs.append(SubQuestion(text=text, category=cast(Category, category), time_range=time_range,
                                queries=queries[:MAX_QUERIES_PER_SUBQUESTION],
                                engines=_valid_engines(engines_raw, known_engines)))

    if not subs:
        return fallback_plan(question, "no usable sub_questions")

    # Legacy flat query list: the query's own category is authoritative. Attach it to a sub-question of that
    # category, else to the first sub-question that has no queries yet (adopting the category), else the first.
    for item in legacy_queries:
        cat = item.get("category") if isinstance(item, dict) else None
        for q, q_range in _queries_from([item]):
            target = next((s for s in subs if cat in VALID_CATEGORIES and s.category == cat), None)
            if target is None:
                target = next((s for s in subs if not s.queries), subs[0])
                if cat in VALID_CATEGORIES and not target.queries:
                    target.category = cast(Category, cat)
            if q.lower() not in {x.lower() for x in target.queries} and \
                    len(target.queries) < MAX_QUERIES_PER_SUBQUESTION:
                target.queries.append(q)
                target.time_range = target.time_range or q_range

    # Every sub-question must be searchable: fall back to its own text.
    for s in subs:
        if not s.queries:
            s.queries = [s.text]
            notes.append(f"no queries for {s.text[:40]!r}; searching its text")

    plan = Plan(subs, source="llm-repaired" if notes else "llm", notes=notes)
    if len(subs) < MIN_SUBQUESTIONS:
        plan.notes.append(f"planner returned {len(subs)} sub-question(s); fewer than the usual {MIN_SUBQUESTIONS}")
    # Trim to the overall query budget while keeping every sub-question represented.
    budget = max(max_queries, len(subs))
    total = sum(len(s.queries) for s in subs)
    while total > budget:
        longest = max(subs, key=lambda s: len(s.queries))
        if len(longest.queries) <= 1:
            break
        longest.queries.pop()
        total -= 1
    return plan
