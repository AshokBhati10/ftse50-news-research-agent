"""Node 1 — FTSE 50 ingestion.

Dynamically builds the top-50 FTSE companies by market capitalisation:

  Wikipedia FTSE 100 constituents table  ->  yfinance market caps  ->  top 50

Robustness rules:
- The Wikipedia table is detected by expected columns, never by fixed index.
- London tickers are normalised for yfinance (``AZN`` -> ``AZN.L``).
- ``fast_info.market_cap`` is preferred (lighter/faster than ``.info``);
  ``.info["marketCap"]`` is the fallback.
- Missing market caps are kept (ranked last) rather than dropping the company.
- Successful ingestion is cached for 7 days.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yfinance as yf

from config.settings import Settings
from utils.cache import CacheManager
from utils.models import Company

log = logging.getLogger(__name__)

FTSE100_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/FTSE_100_Index"
CACHE_KEY = "ftse50_v1"


class IngestionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Wikipedia table handling
# ---------------------------------------------------------------------------

def _detect_constituents_table(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Pick the FTSE 100 constituents table by column names, not by index."""
    for table in tables:
        cols = [str(c).strip().lower() for c in table.columns]
        has_company = any("company" in c for c in cols)
        has_ticker = any(k in c for c in cols for k in ("ticker", "epic", "symbol"))
        if has_company and has_ticker:
            return table
    raise IngestionError(
        "Could not locate the FTSE 100 constituents table on Wikipedia "
        "(no table with Company + Ticker/EPIC/Symbol columns found)."
    )


def _column(df: pd.DataFrame, *candidates: str) -> str:
    cols = {str(c).strip().lower(): str(c) for c in df.columns}
    for cand in candidates:
        for key, original in cols.items():
            if cand in key:
                return original
    raise IngestionError(f"None of {candidates} found in table columns: {list(df.columns)}")


def fetch_ftse100_constituents() -> pd.DataFrame:
    """Download and validate the FTSE 100 constituents table."""
    try:
        # Fetch with requests (browser UA): pd.read_html(url) uses urllib with
        # Python's default UA, which Wikipedia 403s. Wrap in StringIO so the
        # HTML text is parsed as content, not treated as a file path.
        import io

        import requests

        resp = requests.get(
            FTSE100_WIKIPEDIA_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FTSE50NewsAgent/1.0)"},
            timeout=30,
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
    except Exception as exc:
        raise IngestionError(f"Failed to read FTSE 100 page: {exc}") from exc
    table = _detect_constituents_table(tables)
    company_col = _column(table, "company")
    ticker_col = _column(table, "ticker", "epic", "symbol")
    df = table[[company_col, ticker_col]].copy()
    df.columns = ["company", "epic"]
    df = df.dropna(subset=["epic"])
    df["company"] = df["company"].astype(str).str.strip()
    df["epic"] = df["epic"].astype(str).str.strip()
    df = df[df["epic"] != ""].drop_duplicates(subset=["epic"])
    if len(df) < 50:
        raise IngestionError(f"Constituents table unexpectedly small: {len(df)} rows")
    log.info("Found %d FTSE 100 constituents on Wikipedia", len(df))
    return df


# ---------------------------------------------------------------------------
# Ticker + market-cap handling
# ---------------------------------------------------------------------------

def normalize_london_ticker(epic: str) -> str:
    """Normalise a raw EPIC/ticker for yfinance (``AZN`` -> ``AZN.L``).

    Internal dots become hyphens (``BT.A`` -> ``BT-A.L``), which is the
    form yfinance expects for such listings.
    """
    epic = str(epic).strip().upper().replace(" ", "")
    if not epic:
        raise IngestionError("Empty ticker")
    if epic.endswith(".L"):
        return epic
    return epic.rstrip(".").replace(".", "-") + ".L"


def _fetch_market_cap(ticker: str) -> float | None:
    """Blocking yfinance call — run inside a thread pool.

    Prefers ``fast_info`` (lightweight), but corrects a yfinance quirk: for
    London listings it reports ``marketCap`` computed from the GBp (pence)
    price, i.e. 100x the true GBP value — hence the ``/ 100`` for ``.L``
    tickers. Falls back to ``info['marketCap']`` (correctly currency-adjusted
    but slower/flakier) when ``fast_info`` has nothing.
    """
    try:
        t = yf.Ticker(ticker)
        cap = None
        try:
            cap = t.fast_info.get("marketCap")
            if cap and ticker.upper().endswith(".L"):
                cap = cap / 100.0
        except Exception:
            cap = None
        if not cap:
            try:
                cap = (t.info or {}).get("marketCap")  # heavier fallback
            except Exception:
                cap = None
        return float(cap) if cap else None
    except Exception as exc:  # noqa: BLE001 — one bad ticker must not kill ingestion
        log.warning("Market-cap lookup failed for %s: %s", ticker, exc)
        return None


async def fetch_market_caps(tickers: list[str], max_workers: int = 8) -> dict[str, float | None]:
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = await asyncio.gather(
            *(loop.run_in_executor(pool, _fetch_market_cap, t) for t in tickers)
        )
    return dict(zip(tickers, results))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def build_ftse50(
    settings: Settings,
    cache: CacheManager | None = None,
    limit: int = 50,
    use_cache: bool = True,
) -> list[Company]:
    """Return the top-`limit` FTSE companies sorted by market cap (descending)."""
    if cache and use_cache:
        cached = cache.companies.get("ftse50", CACHE_KEY)
        if cached:
            companies = [Company(**c) for c in cached][:limit]
            log.info("Using cached FTSE50 list (%d companies)", len(companies))
            return companies

    df = await asyncio.to_thread(fetch_ftse100_constituents)
    df["ticker"] = df["epic"].map(normalize_london_ticker)

    caps: dict[str, float | None] = {}
    if cache:
        # Reuse per-ticker cached caps where fresh.
        uncached = []
        for t in df["ticker"]:
            hit = cache.marketcap.get("caps", t)
            if hit is None:
                uncached.append(t)
            else:
                caps[t] = hit
        log.info("Market-cap cache hits: %d/%d", len(caps), len(df))
    else:
        uncached = list(df["ticker"])

    if uncached:
        log.info("Fetching market caps for %d tickers via yfinance …", len(uncached))
        fresh = await fetch_market_caps(uncached)
        caps.update(fresh)
        if cache:
            for t, c in fresh.items():
                cache.marketcap.set("caps", t, c)

    df["market_cap"] = df["ticker"].map(caps)
    missing = int(df["market_cap"].isna().sum())
    if missing:
        log.warning("%d tickers have no market-cap data; they rank last", missing)

    df = df.sort_values("market_cap", ascending=False, na_position="last").head(limit)
    companies = [
        Company(name=row.company, ticker=row.ticker,
                market_cap=float(row.market_cap) if pd.notna(row.market_cap) else None)
        for row in df.itertuples()
    ]

    if cache and use_cache:
        cache.companies.set("ftse50", CACHE_KEY, [c.model_dump(mode="json") for c in companies])
    log.info("Top %d: %s …", limit, ", ".join(c.ticker for c in companies[:5]))
    return companies
