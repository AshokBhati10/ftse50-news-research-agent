"""Tests for nodes/deduplication.py — semantic near-duplicate detection."""

import numpy as np
import pytest

from nodes.deduplication import SemanticDeduper
from utils.models import Article


def _article(title: str, url: str, quality: float = 0.5) -> Article:
    a = Article(company="X", ticker="X.L", title=title, url=url,
                snippet="body text " + title)
    a.source_quality = quality
    return a


def _fake_embed(texts: list[str]) -> np.ndarray:
    """Deterministic toy embeddings with calibrated similarities.

    - "earnings beat …" vs "beats forecasts …"  -> cosine ≈ 0.9999 (near-dup)
    - either vs "dividend …"                    -> cosine ≈ 0.05  (distinct)
    """
    vecs = []
    for t in texts:
        key = t.split("\n")[0].lower()
        if "earnings beat" in key:
            v = np.array([1.0, 0.05, 0.0])
        elif "beats forecasts" in key:
            v = np.array([0.98, 0.06, 0.0])
        elif "dividend" in key:
            v = np.array([0.0, 1.0, 0.0])
        else:
            # Deterministic well-separated fallback: parity of char codes picks
            # one of two orthogonal vectors, so unknown titles stay distinct.
            s = sum(ord(c) for c in key)
            v = np.array([0.0, 0.0, 1.0]) if s % 2 else np.array([1.0, 0.0, 0.0])
        vecs.append(v)
    return np.stack(vecs)


def _deduper(threshold: float = 0.85) -> SemanticDeduper:
    return SemanticDeduper(model_name="unused", threshold=threshold, embed_fn=_fake_embed)


def test_semantic_dedup_removes_near_duplicates():
    arts = [
        _article("AstraZeneca earnings beat estimates", "https://reuters.com/1", quality=0.8),
        _article("AstraZeneca beats forecasts, up 5%", "https://bbc.com/2", quality=0.6),
        _article("AstraZeneca declares interim dividend", "https://ft.com/3", quality=0.8),
    ]
    out = _deduper().deduplicate(arts)
    assert len(out) == 2
    # Survivor of the earnings pair is the higher source-quality one.
    survivors = {a.url for a in out}
    assert "https://reuters.com/1" in survivors
    assert "https://ft.com/3" in survivors


def test_semantic_dedup_keeps_distinct_stories():
    arts = [
        _article("AstraZeneca earnings beat estimates", "https://reuters.com/1"),
        _article("AstraZeneca declares interim dividend", "https://ft.com/2"),
        _article("Shell approves new oil project", "https://reuters.com/3"),
    ]
    out = _deduper().deduplicate(arts)
    assert len(out) == 3


def test_threshold_is_configurable():
    arts = [
        _article("AstraZeneca earnings beat estimates", "https://reuters.com/1"),
        _article("AstraZeneca beats forecasts, up 5%", "https://bbc.com/2"),
    ]
    # Absurdly strict threshold keeps both.
    assert len(_deduper(threshold=0.99999).deduplicate(arts)) == 2
    # Lenient threshold collapses them.
    assert len(_deduper(threshold=0.5).deduplicate(arts)) == 1


def test_embedding_failure_keeps_pipeline_alive():
    def _boom(texts):
        raise RuntimeError("no model")

    d = SemanticDeduper(model_name="unused", threshold=0.85, embed_fn=_boom)
    arts = [_article("t1", "https://a.com/1"), _article("t1", "https://a.com/2")]
    assert len(d.deduplicate(arts)) == 2


@pytest.mark.asyncio
async def test_deduplicate_async_matches_sync():
    arts = [_article("t1", "https://a.com/1"), _article("t2", "https://a.com/2")]
    d = _deduper()
    out_async = await d.deduplicate_async(arts)
    out_sync = d.deduplicate(arts)
    assert [a.url for a in out_async] == [a.url for a in out_sync]
