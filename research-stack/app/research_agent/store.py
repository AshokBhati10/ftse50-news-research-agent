from __future__ import annotations

import re
import uuid
from typing import Any

from .models import Chunk, Passage

# qdrant-client is imported lazily so the package imports without it installed.

NAMESPACE = uuid.UUID("6f1c2b1e-6f7a-4b5e-9d1a-3e2f4a5b6c7d")  # fixed, so point ids are stable


class VectorStore:
    """Qdrant collection of chunks. Re-upserting the same URL+chunk overwrites (idempotent)."""

    def __init__(self, url: str, collection: str, dim: int) -> None:
        from qdrant_client import AsyncQdrantClient

        self.client = AsyncQdrantClient(url=url)
        self.collection = collection
        self.dim = dim

    async def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

        if await self.client.collection_exists(self.collection):
            return
        await self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )
        # Payload indexes make filtered search fast.
        for field, schema in [
            ("canonical_url", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("engine", PayloadSchemaType.KEYWORD),
            ("doc_type", PayloadSchemaType.KEYWORD),
            ("run_id", PayloadSchemaType.KEYWORD),
            ("date", PayloadSchemaType.DATETIME),
        ]:
            await self.client.create_payload_index(self.collection, field_name=field, field_schema=schema)

    @staticmethod
    def point_id(canonical_url: str, index: int) -> str:
        return str(uuid.uuid5(NAMESPACE, f"{canonical_url}#{index}"))

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]], run_id: str) -> None:
        from qdrant_client.models import PointStruct

        points = []
        for ch, vec in zip(chunks, vectors):
            d = ch.doc
            payload: dict[str, Any] = {
                "text": ch.text,
                "chunk_index": ch.index,
                "url": d.url,
                "canonical_url": d.canonical_url,
                "title": d.title,
                "source": d.source,
                "engine": d.engine,
                "engines": d.engines,
                "query": d.query,
                "doc_type": d.doc_type,
                "content_hash": d.content_hash,
                "fetched_at": d.fetched_at,
                "run_id": run_id,
            }
            if d.date and re.match(r"^\d{4}-\d{2}-\d{2}", d.date):
                payload["date"] = d.date[:10]  # "YYYY-MM-DD" is accepted by Qdrant's datetime index
            points.append(PointStruct(id=self.point_id(d.canonical_url, ch.index), vector=vec, payload=payload))
        if points:
            await self.client.upsert(collection_name=self.collection, points=points, wait=True)

    async def search(
        self,
        vector: list[float],
        limit: int = 40,
        *,
        sources: list[str] | None = None,
        run_id: str | None = None,
    ) -> list[Passage]:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        must: list[Any] = []
        if sources:
            must.append(FieldCondition(key="source", match=MatchAny(any=sources)))
        if run_id:
            must.append(FieldCondition(key="run_id", match=MatchAny(any=[run_id])))
        res = await self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=limit,
            query_filter=Filter(must=must) if must else None,
            with_payload=True,
        )
        out: list[Passage] = []
        for p in res.points:
            pl = p.payload or {}
            out.append(Passage(text=pl.get("text", ""), url=pl.get("url", ""), title=pl.get("title", ""),
                               date=pl.get("date"), source=pl.get("source", ""), engine=pl.get("engine", ""),
                               score=float(p.score)))
        return out

    async def close(self) -> None:
        await self.client.close()
