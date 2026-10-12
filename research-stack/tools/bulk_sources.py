"""Batch ingestion of free bulk sources into Qdrant (guide section 9, milestone "Week 7+").

STUB: the pure helpers below (OpenAlex abstract rebuild, GDELT GKG URLs, RSS polling) come from the
guide. The ingestion entry points that write to Qdrant are TODOs.

Each helper yields plain dicts ready for: Document -> chunk_document -> Embedder.embed -> VectorStore.upsert.
Load each source into its OWN Qdrant collection (e.g. `openalex_works`, `pubmed`), not `research_chunks`.
For GDELT and RSS items, run the URLs through `research_agent.extract.Extractor` first to get full text.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path


# ---------- OpenAlex snapshot (downloaded with: aws s3 sync s3://openalex/data/jsonl/works ./works --no-sign-request)
def openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not inverted:
        return ""
    positions = {pos: word for word, poss in inverted.items() for pos in poss}
    return " ".join(positions[i] for i in sorted(positions))


def openalex_works(folder: Path, min_year: int = 2018, topic_keyword: str | None = None) -> Iterator[dict]:
    for part in sorted(folder.rglob("part_*.gz")):
        with gzip.open(part, "rt", encoding="utf-8") as fh:
            for line in fh:
                w = json.loads(line)
                if (w.get("publication_year") or 0) < min_year:
                    continue
                abstract = openalex_abstract(w.get("abstract_inverted_index"))
                if not abstract:
                    continue
                if topic_keyword and topic_keyword.lower() not in (w.get("display_name") or "").lower() + abstract.lower():
                    continue
                yield {"url": w.get("doi") or w["id"], "title": w.get("display_name") or "",
                       "date": w.get("publication_date"), "source": "openalex.org", "engine": "openalex-snapshot",
                       "text": abstract, "cited_by_count": w.get("cited_by_count", 0)}


# ---------- GDELT 2.0 GKG: list of news article URLs every 15 minutes (free raw files, no API)
LASTUPDATE = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"


def gdelt_latest_gkg_urls() -> Iterator[dict]:
    """Yields {url, date, source, themes} from the newest GKG file. Fetch the article text with the Extractor."""
    import httpx

    lines = httpx.get(LASTUPDATE, timeout=30).text.strip().splitlines()
    gkg_zip = next(line.split()[-1] for line in lines if line.endswith(".gkg.csv.zip"))
    raw = httpx.get(gkg_zip, timeout=120).content
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode("utf-8", errors="replace")
    csv.field_size_limit(10_000_000)
    for row in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(row) > 7 and row[4].startswith("http"):
            # columns: 1=DATE (YYYYMMDDHHMMSS), 3=SourceCommonName, 4=DocumentIdentifier (URL), 7=V1Themes
            yield {"url": row[4], "date": row[1], "source": row[3], "themes": row[7].split(";")}


# ---------- RSS / Atom feeds you trust (no shared quota; use ETag / Last-Modified to be polite)
def poll_feed(url: str, etag: str | None = None, modified: str | None = None) -> tuple[list[dict], str | None, str | None]:
    import feedparser  # lazy: only needed when polling feeds (listed in app/requirements.txt)

    feed = feedparser.parse(url, etag=etag, modified=modified)
    if getattr(feed, "status", 200) == 304:  # not modified since last poll
        return [], etag, modified
    items = [{"url": e.get("link"), "title": e.get("title", ""), "date": e.get("published") or e.get("updated"),
              "summary": e.get("summary", "")} for e in feed.entries if e.get("link")]
    return items, feed.get("etag"), feed.get("modified")


# ---------- Ingestion into Qdrant: TODO (Week 7+) ----------
async def ingest_openalex(folder: Path, collection: str = "openalex_works") -> int:
    """TODO: wrap each openalex_works() dict in a Document, then chunk_document -> Embedder.embed ->
    VectorStore(collection).upsert(). Run at night, in batches; resume by canonical_url (ids are idempotent)."""
    raise NotImplementedError("TODO: OpenAlex -> Qdrant ingestion")


async def ingest_pubmed(baseline_dir: Path, collection: str = "pubmed") -> int:
    """TODO: parse the PubMed baseline/updatefiles XML from https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/ and ingest."""
    raise NotImplementedError("TODO: PubMed -> Qdrant ingestion")


async def ingest_arxiv(set_spec: str = "cs:cs:AI", collection: str = "arxiv") -> int:
    """TODO: harvest metadata via OAI-PMH (https://oaipmh.arxiv.org/oai). Do NOT crawl arxiv.org or
    pull the requester-pays S3 PDFs (guide section 9)."""
    raise NotImplementedError("TODO: arXiv OAI-PMH -> Qdrant ingestion")


async def ingest_feeds(feed_urls: list[str], collection: str = "news_feeds") -> int:
    """TODO: poll_feed() each URL (persist etag/modified between runs), fetch full text with the Extractor, ingest."""
    raise NotImplementedError("TODO: RSS/Atom -> Qdrant ingestion")


if __name__ == "__main__":
    raise SystemExit("bulk_sources.py is a stub - see the TODOs (guide section 9).")
