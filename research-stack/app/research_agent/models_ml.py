from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .models import Passage

# sentence-transformers (and torch) are heavy: they are imported, and the model weights are loaded,
# only when a model is first used - never at import time.


class Embedder:
    """BAAI/bge-m3 dense embeddings (1024-dim, multilingual, up to 8192 tokens)."""

    def __init__(self, model_name: str = "BAAI/bge-m3", device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None
        self._dim: int | None = None

    @property
    def model(self):  # SentenceTransformer, loaded on first access
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            # sentence-transformers >= 5 names it get_embedding_dimension(); older versions use the long name.
            get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
            dim = get_dim()
            self._dim = int(dim) if dim else 1024
        return self._dim

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self.model.encode(list(texts), batch_size=16, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Model inference is CPU/GPU bound: run it off the event loop.
        return await asyncio.to_thread(self._encode, texts)


class Reranker:
    """BAAI/bge-reranker-v2-m3 cross-encoder: scores (query, passage) pairs jointly."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None

    @property
    def model(self):  # CrossEncoder, loaded on first access
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name, max_length=512, device=self.device)
        return self._model

    def _score(self, query: str, texts: list[str]) -> list[float]:
        scores = self.model.predict([(query, t) for t in texts], batch_size=16, show_progress_bar=False)
        return [float(s) for s in scores]

    async def rerank(self, query: str, passages: list[Passage], top_k: int, min_score: float = 0.05) -> list[Passage]:
        if not passages:
            return []
        scores = await asyncio.to_thread(self._score, query, [p.text for p in passages])
        for p, s in zip(passages, scores):
            p.score = s
        ranked = sorted(passages, key=lambda p: p.score, reverse=True)
        return [p for p in ranked if p.score >= min_score][:top_k]
