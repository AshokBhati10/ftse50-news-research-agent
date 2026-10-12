"""Detect the market universe a question is scoped to (e.g. FTSE).

Used to keep FTSE-specific questions on FTSE sources and to flag answers that
stray into unrelated global/US rankings. Deliberately generic — the universe
is inferred from the query text and matched against evidence text; no company
names are hardcoded.
"""

from __future__ import annotations

import re

# universe key -> (query patterns, evidence signals)
_UNIVERSES: dict[str, tuple[re.Pattern, tuple[str, ...]]] = {
    "ftse": (
        re.compile(r"\bftse\b|\bftse\s*\d{1,3}\b", re.I),
        ("ftse", "ftse 100", "ftse 250", "london stock exchange", "lse",
         "uk-listed", "britain", "british", "uk company", ".l "),
    ),
    "sp500": (
        re.compile(r"\bs\s*&\s*p\s*500\b|\bsp-?500\b", re.I),
        ("s&p 500", "sp500", "wall street", "us-listed", "nasdaq", "nyse"),
    ),
    "dax": (
        re.compile(r"\bdax\b|\bfrankfurt\b", re.I),
        ("dax", "frankfurt", "german"),
    ),
}

# Ticker-like suffixes that signal a UK listing independent of wording.
_UK_TICKER_RE = re.compile(r"\b[a-z0-9]{1,5}\.l\b", re.I)


def requested_universe(query: str) -> str | None:
    """Return the explicit market universe key requested, else None."""
    q = query or ""
    for key, (pattern, _signals) in _UNIVERSES.items():
        if pattern.search(q):
            return key
    return None


def mentions_universe(text: str, universe: str) -> bool:
    entry = _UNIVERSES.get(universe)
    if entry is None:
        return True
    _pattern, signals = entry
    low = (text or "").lower()
    if any(s in low for s in signals):
        return True
    if universe == "ftse" and _UK_TICKER_RE.search(low):
        return True
    return False
