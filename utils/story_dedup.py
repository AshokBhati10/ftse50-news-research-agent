"""Deterministic near-duplicate *story* detection for retrieved news items.

URL canonicalisation (``nodes.search.normalize_url`` / ``dedup_by_url``)
collapses the *same link*. Syndicated copies of one story, however, live on
different URLs and even different domains, so we additionally compare a
lightweight token fingerprint of the headline.

This module is deliberately dependency-free and deterministic: it never
decides source trust, only whether two items describe *the same story*. It is
a conservative, high-precision detector — near-identical/syndicated headlines
collapse; genuinely different stories that merely share a company name do not
(their token sets differ enough).

Note: this does not attempt to detect *paraphrased* wire copy across outlets.
That semantic judgement belongs to the synthesis stage, which is explicitly
instructed not to treat multiple sources of one transaction as separate
developments.
"""

from __future__ import annotations

import re

# Words that carry little story-identity signal in a headline. Removing them
# makes "X latest news" and "X news developments" collapse to the same story
# while keeping distinguishing nouns/verbs/numbers.
_STOP = frozenset(
    "a an the and or of in on to for with is are was were be been by at from "
    "as it its this that these those what who when where which how why did "
    "does do s latest recent new news update updates development developments "
    "major past week month year today report reports says said".split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def significant_tokens(text: str) -> list[str]:
    """Lowercased, stop-word-filtered tokens (length > 2)."""
    return [t for t in _WORD_RE.findall((text or "").lower())
            if t not in _STOP and len(t) > 2]


def headline_tokens(title: str) -> frozenset[str]:
    return frozenset(significant_tokens(title))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def is_near_duplicate_title(a: str, b: str, threshold: float = 0.8,
                            *, min_tokens: int = 3) -> bool:
    """True when two headlines are the same story (high token overlap).

    ``min_tokens`` guards against over-collapsing very short/generic titles
    (e.g. "Story A"/"Story B" both reduce to a single token): such items are
    treated as distinct.
    """
    ta, tb = headline_tokens(a), headline_tokens(b)
    if len(ta) < min_tokens or len(tb) < min_tokens:
        return False
    return jaccard(ta, tb) >= threshold


def dedup_stories(items: list, *, title_getter, threshold: float = 0.8,
                  min_tokens: int = 3) -> list:
    """Drop near-duplicate stories, keeping the first occurrence (best first).

    Ordering matters: callers rank items before de-duplicating so the survivor
    is the one they prefer (e.g. highest source quality).
    """
    kept: list = []
    seen: list[frozenset[str]] = []
    for item in items:
        toks = headline_tokens(title_getter(item))
        if (len(toks) >= min_tokens
                and any(jaccard(toks, k) >= threshold for k in seen)):
            continue
        kept.append(item)
        if len(toks) >= min_tokens:
            seen.append(toks)
    return kept
