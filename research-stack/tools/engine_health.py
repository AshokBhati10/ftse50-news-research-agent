"""Canary check: run one known query per engine and report which engines are returning results.
Run it from cron every 30-60 minutes and alert when a key engine is empty for several runs.

    python engine_health.py http://localhost:8080
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

CANARIES: dict[str, str] = {
    "duckduckgo": "python asyncio tutorial", "brave": "python asyncio tutorial",
    "google": "python asyncio tutorial", "bing": "python asyncio tutorial",
    "wikipedia": "transformer neural network", "google news": "india economy",
    "bing news": "india economy", "duckduckgo news": "india economy",
    "arxiv": "retrieval augmented generation", "google scholar": "retrieval augmented generation",
    "semantic scholar": "retrieval augmented generation", "pubmed": "crispr gene editing",
    "crossref": "retrieval augmented generation", "openalex": "retrieval augmented generation",
    "github": "searxng", "stackoverflow": "python asyncio gather",
}


async def check(client: httpx.AsyncClient, engine: str, q: str) -> dict[str, object]:
    try:
        r = await client.get("/search", params={"q": q, "format": "json", "engines": engine})
        data = r.json()
        return {"engine": engine, "status": r.status_code,  # wikipedia/wikidata answer mostly via infoboxes, not results
                "results": len(data.get("results", [])) + len(data.get("infoboxes", [])),
                "errors": data.get("unresponsive_engines", [])}
    except (httpx.HTTPError, ValueError) as exc:
        return {"engine": engine, "status": "error", "results": 0, "errors": [str(exc)]}


async def main(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        rows = []
        for engine, q in CANARIES.items():  # sequential on purpose: canaries must not cause blocks
            rows.append(await check(client, engine, q))
            await asyncio.sleep(2)
        errors = (await client.get("/stats/errors")).json()
    for row in rows:
        flag = "OK  " if row["results"] else "FAIL"
        print(f"{flag} {row['engine']:<18} results={row['results']:<3} {row['errors']}")
    print("\n/stats/errors engines with recorded errors:", ", ".join(sorted(errors)) or "none")
    with open("engine_health.json", "w") as fh:
        json.dump(rows, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"))
