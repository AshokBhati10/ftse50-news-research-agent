"""Deterministic evidence checks for financial-ranking questions.

This module exists for one job: decide whether the retrieved evidence can
support a *market-capitalisation* claim or ranking. It is conservative by
design and never infers missing context:

  * a bare numeric value such as ``238.677`` is **not** a usable market cap —
    without a stated metric, currency/unit and as-of date it carries no
    meaning we can compare across companies;
  * a value is only *usable* when its text states the metric AND carries a
    currency or an explicit magnitude (billion/million/trillion) AND an
    absolute or "as of" date.

It is task-scoped: nothing here runs for non-financial queries. The synthesis
and verification layers call it only when the question is about market caps or
a market-cap ranking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# Metric must be identifiable as market capitalisation (not price/volume/EV).
_METRIC_RE = re.compile(
    r"\bmarket\s*cap(?:itali[sz]ation)?\b|\bmkt\.?\s*cap\b|\bmarket\s+value\b",
    re.I,
)
_RANK_RE = re.compile(
    r"\b(?:rank(?:ing|ed)?|top\s*\d{1,3}|largest|biggest|"
    r"order(?:ed)?\s+by|by\s+market\s+cap)\b",
    re.I,
)
_AS_OF_RE = re.compile(
    r"\b(?:as\s+of|as\s+at|at\s+close|market\s+close|as\s+on|on)\b", re.I
)
_ABS_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b",
    re.I,
)

_CURRENCY_SYMBOL = {"£": "GBP", "$": "USD", "€": "EUR"}
_CURRENCY_WORD = {"gbp": "GBP", "usd": "USD", "eur": "EUR", "gbx": "GBX"}

# Magnitude expressed as a word or suffix.
_MAGNITUDE = {
    "trillion": 1e12, "tn": 1e12, "t": 1e12,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
}
_VALUE_RE = re.compile(
    r"(?P<cur>£|\$|€|\bgbx\b|\bgbp\b|\busd\b|\beur\b)?"
    r"\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<mag>trillion|billion|million|thousand|tn|bn|mn|t|b|m|k)?",
    re.I,
)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Sources acceptable for a market-cap *ranking* (exchange, major financial
# data providers, official filings). Matched against the URL host suffix.
RANKING_SOURCE_SUFFIXES = (
    "londonstockexchange.com", "finance.yahoo.com", "uk.finance.yahoo.com",
    "bloomberg.com", "sec.gov", "companieshouse.gov.uk", "fca.org.uk",
)

_ENTITY_STOP = {
    "the", "a", "an", "as", "of", "at", "on", "in", "and", "market", "cap",
    "capitalisation", "capitalization", "value", "ftse", "lse", "top",
    "rank", "ranked", "ranking", "by", "with", "is", "was", "total",
}
_ENTITY_RE = re.compile(r"[A-Z][\w&\'\-]*(?:\s+[A-Z][\w&\'\-]*){0,4}")


def _bound_entity(prefix: str) -> str:
    """Company name immediately attached to a value, from the text before it.

    Only the current clause is considered, so a figure never borrows a name
    from an earlier sentence/line. Returns "" when no name is adjacent.
    """
    clause = re.split(r"[;\n]|\.\s", prefix)[-1]
    clause = _METRIC_RE.sub(" ", clause)
    names = []
    for m in _ENTITY_RE.finditer(clause):
        words = [w for w in m.group(0).split()
                 if w.lower().strip("&'-") not in _ENTITY_STOP]
        if words:
            names.append((m.end(), " ".join(words)))
    if not names:
        return ""
    end, name = names[-1]
    # Must sit right before the value (only punctuation/ticker between).
    gap = clause[end:]
    if re.fullmatch(r"[\s:,\-\u2013(\w.]{0,14}\)?\s*", gap) is None:
        return ""
    return name.lower()


def is_ranking_source(url: str) -> bool:
    host = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0]
    host = host.split(":")[0]
    return any(host == d or host.endswith("." + d)
               for d in RANKING_SOURCE_SUFFIXES)


@dataclass
class MarketCapValue:
    raw: str
    currency: str = ""
    magnitude: float | None = None
    value: float | None = None
    has_as_of: bool = False
    entity: str = ""  # company the figure is attached to ("" = unbound)
    source_ok: bool = True  # source tier acceptable for a ranking

    @property
    def usable(self) -> bool:
        # Metric presence is checked by the caller; here we require a unit of
        # some kind (currency or explicit magnitude), a date, and a company
        # the figure is bound to.
        has_unit = bool(self.currency) or self.magnitude is not None
        return has_unit and self.has_as_of and bool(self.entity)


@dataclass
class MarketCapAssessment:
    requested: bool = False
    ranking_requested: bool = False
    usable: bool = False
    values: list[MarketCapValue] = field(default_factory=list)
    reason: str = ""


def is_market_cap_question(query: str) -> bool:
    return bool(_METRIC_RE.search(query or ""))


def is_ranking_question(query: str) -> bool:
    return bool(_RANK_RE.search(query or ""))


def answer_claims_ranking(text: str) -> bool:
    """Whether a synthesized answer asserts a ranking/ordering."""
    return bool(_RANK_RE.search(text or ""))


def _to_float(num: str) -> float | None:
    try:
        return float(num.replace(",", ""))
    except ValueError:
        return None


def extract_market_cap_values(text: str) -> list[MarketCapValue]:
    """Extract candidate market-cap values from one evidence/free-text block.

    The block must mention the market-cap metric at all; otherwise there is no
    basis to read a number as market capitalisation.
    """
    if not text or not _METRIC_RE.search(text):
        return []
    has_date = bool(_ABS_DATE_RE.search(text) or _AS_OF_RE.search(text))
    out: list[MarketCapValue] = []
    for m in _VALUE_RE.finditer(text):
        cur_raw = (m.group("cur") or "").strip().lower()
        mag_raw = (m.group("mag") or "").strip().lower()
        num_raw = m.group("num")
        currency = _CURRENCY_SYMBOL.get(cur_raw) or _CURRENCY_WORD.get(cur_raw, "")
        magnitude = _MAGNITUDE.get(mag_raw)
        if not currency and magnitude is None:
            # A bare number (e.g. 238.677) — never a usable market cap.
            continue
        if not currency and mag_raw not in ("trillion", "billion", "million",
                                            "tn", "bn", "mn"):
            # Ambiguous single-letter suffixes ("b", "m", "k") require a
            # currency to be interpretable.
            continue
        base = _to_float(num_raw)
        value = base * magnitude if (base is not None and magnitude) else base
        out.append(MarketCapValue(
            raw=m.group(0).strip(),
            currency=currency,
            magnitude=magnitude,
            value=value,
            has_as_of=has_date,
            entity=_bound_entity(text[:m.start()]),
        ))
    return out


def assess_market_cap_evidence(
    items: list[dict],
    *,
    text_getter: Callable[[dict], str],
    query: str = "",
) -> MarketCapAssessment:
    """Assess whether ``items`` support a market-cap claim/ranking.

    A ranking needs at least two usable, comparably-denominated values; a
    single-company claim needs one. Bare numbers are discarded.
    """
    requested = is_market_cap_question(query)
    ranking = is_ranking_question(query)
    values: list[MarketCapValue] = []
    seen_text: set[str] = set()
    for it in items or []:
        text = text_getter(it)
        # Republished copies of the same text are one source, not two.
        key = re.sub(r"\W+", " ", (text or "").lower()).strip()
        if key in seen_text:
            continue
        seen_text.add(key)
        ok = (not ranking) or is_ranking_source(it.get("url", ""))
        for v in extract_market_cap_values(text):
            v.source_ok = ok
            values.append(v)
    usable_values = [v for v in values if v.usable and v.source_ok]
    currencies = {v.currency for v in usable_values if v.currency}
    # Independent figures: one per distinct company.
    entities = {v.entity for v in usable_values}
    needed = 2 if ranking else 1
    enough = len(entities) >= needed
    # Comparable across companies only when a single currency/unit is used.
    comparable = len(currencies) <= 1
    usable = enough and comparable
    reason = ""
    if not enough:
        reason = ("fewer than the %d usable market-cap value(s) required "
                  "(need an explicit metric, currency/unit, as-of date, a "
                  "named company%s)"
                  % (needed, " and an exchange/financial-data/official "
                     "source" if ranking else ""))
    elif not comparable:
        reason = "usable values are denominated in mixed currencies"
    return MarketCapAssessment(
        requested=requested,
        ranking_requested=ranking,
        usable=usable,
        values=values,
        reason=reason,
    )


def unmatched_figures(answer_text: str, evidence_texts: list[str]) -> list[str]:
    """Currency/magnitude figures in the answer absent from *all* evidence.

    Used as a conservative fabrication check: only currency-or-magnitude
    numbers are considered, and a figure is "unmatched" only when no evidence
    block contains any of its numeric components.
    """
    haystack = " ".join(evidence_texts or [])
    present_numbers = {n.replace(",", "") for n in _NUMBER_RE.findall(haystack)}
    missing: list[str] = []
    for m in _VALUE_RE.finditer(answer_text or ""):
        cur_raw = (m.group("cur") or "").strip().lower()
        mag_raw = (m.group("mag") or "").strip().lower()
        if not cur_raw and not _MAGNITUDE.get(mag_raw):
            continue
        num = m.group("num").replace(",", "")
        if num and num not in present_numbers:
            missing.append(m.group(0).strip())
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for f in missing:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def conflicting_market_caps(values: list[MarketCapValue],
                            tolerance: float = 0.05) -> list[str]:
    """Companies whose usable market-cap figures disagree beyond tolerance.

    Compares only like-for-like (same company, same currency) values; returns
    the company names so the caller can disclose the contradiction instead of
    silently picking one figure.
    """
    by_key: dict[tuple[str, str], list[float]] = {}
    for v in values:
        if v.usable and v.value:
            by_key.setdefault((v.entity, v.currency), []).append(v.value)
    out: list[str] = []
    for (entity, _cur), vals in by_key.items():
        lo, hi = min(vals), max(vals)
        if lo > 0 and (hi - lo) / hi > tolerance and entity not in out:
            out.append(entity)
    return out
