"""Node 3b — Deduplication (two levels).

1. URL-level: canonicalised URL matching (see :func:`nodes.search.normalize_url`).
2. Content/title semantic: ``all-MiniLM-L6-v2`` embeddings + cosine similarity.

The similarity threshold is configurable (default 0.85) and deliberately
*not* treated as permanently optimal — ``evaluation.py`` ships duplicate /
near-duplicate / distinct cases so it can be tuned objectively.

When near-duplicates are found, the article with the highest source quality
(then the most recent date) is kept, so syndicated copies collapse onto the
most authoritative version.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

import numpy as np

from config.settings import Settings
from utils.models import Article

log = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], "np.ndarray"]


class SemanticDeduper:
    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        threshold: float = 0.85,
        embed_fn: EmbedFn | None = None,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self._embed_fn = embed_fn
        self._model = None
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------ model
    def _ensure_model(self):
        # Double-checked locking: several companies' dedup threads may race here.
        if self._model is None and self._embed_fn is None:
            with self._model_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    log.info("Loading embedding model %s …", self.model_name)
                    self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._embed_fn is not None:
            return np.asarray(self._embed_fn(texts), dtype=np.float32)
        model = self._ensure_model()
        return np.asarray(
            model.encode(texts, convert_to_numpy=True, normalize_embeddings=True),
            dtype=np.float32,
        )

    # --------------------------------------------------------------- deduping
    @staticmethod
    def _article_text(a: Article) -> str:
        # Title carries the most signal for syndication detection; the body
        # disambiguates genuinely different stories that share a headline shape.
        body = (a.content or a.snippet or "")[:800]
        return f"{a.title}\n{body}".strip()

    def deduplicate(self, articles: list[Article]) -> list[Article]:
        """Return articles with near-duplicates removed (order: best first)."""
        if len(articles) < 2:
            return list(articles)

        texts = [self._article_text(a) for a in articles]
        try:
            embeddings = self.embed(texts)
        except Exception as exc:  # noqa: BLE001 — embedding failure keeps pipeline alive
            log.warning("Embedding failed (%s); skipping semantic dedup", exc)
            return list(articles)

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized = embeddings / norms
        sim = normalized @ normalized.T

        # Prefer higher source quality, then recency, when choosing survivors.
        order = sorted(
            range(len(articles)),
            key=lambda i: (
                articles[i].source_quality,
                articles[i].published_at is not None,
            ),
            reverse=True,
        )
        kept: list[int] = []
        dropped: list[int] = []
        for idx in order:
            if any(sim[idx, k] > self.threshold for k in kept):
                dropped.append(idx)
                continue
            kept.append(idx)

        if dropped:
            log.info(
                "Semantic dedup: dropped %d near-duplicates (threshold %.2f)",
                len(dropped), self.threshold,
            )
        # Restore original relative order among survivors for stability.
        kept_sorted = sorted(kept, key=lambda i: order.index(i))
        return [articles[i] for i in kept_sorted]

    async def deduplicate_async(self, articles: list[Article]) -> list[Article]:
        return await asyncio.to_thread(self.deduplicate, articles)


def build_deduper(settings: Settings, embed_fn: EmbedFn | None = None) -> SemanticDeduper:
    return SemanticDeduper(
        model_name=settings.embedding_model,
        threshold=settings.dedup_similarity_threshold,
        embed_fn=embed_fn,
    )
