"""Tests for nodes/ingestion.py — table detection, ticker normalisation, ranking."""

import pandas as pd
import pytest

from nodes.ingestion import (
    IngestionError,
    _detect_constituents_table,
    build_ftse50,
    normalize_london_ticker,
)


def _ftse_table() -> pd.DataFrame:
    return pd.DataFrame({
        "Company": ["AstraZeneca", "HSBC Holdings", "Shell"],
        "Ticker": ["AZN", "HSBA", "SHEL"],
        "FTSE Index": ["FTSE 100"] * 3,
    })


def _big_table() -> pd.DataFrame:
    # Mirrors fetch_ftse100_constituents' normalized columns.
    rows = [("Company %d" % i, "T%d" % i) for i in range(60)]
    return pd.DataFrame(rows, columns=["company", "epic"])


def test_detect_table_by_columns_not_index():
    junk = pd.DataFrame({"a": [1], "b": [2]})
    real = _ftse_table()
    # Real table is NOT first — detection must still find it.
    found = _detect_constituents_table([junk, real])
    assert list(found.columns)[:2] == ["Company", "Ticker"]


def test_detect_table_raises_when_missing():
    with pytest.raises(IngestionError):
        _detect_constituents_table([pd.DataFrame({"a": [1]})])


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AZN", "AZN.L"),
        ("azn", "AZN.L"),
        (" AZN ", "AZN.L"),
        ("AZN.L", "AZN.L"),
        ("HSBA", "HSBA.L"),
        ("BT.A", "BT-A.L"),  # internal dots become hyphens for yfinance
    ],
)
def test_normalize_london_ticker(raw, expected):
    assert normalize_london_ticker(raw) == expected


def test_normalize_empty_ticker_raises():
    with pytest.raises(IngestionError):
        normalize_london_ticker("   ")


def test_fetch_market_cap_corrects_gbp_pence_quirk(monkeypatch):
    """yfinance fast_info reports LSE caps 100x (GBp price); we correct it."""
    import nodes.ingestion as ing

    class FakeFastInfo:
        def get(self, key):
            return 18_443_371_083_864.0 if key == "marketCap" else None

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker
            self.fast_info = FakeFastInfo()

        @property
        def info(self):  # pragma: no cover — should not be reached
            raise AssertionError("info should not be needed")

    monkeypatch.setattr(ing.yf, "Ticker", FakeTicker)
    cap = ing._fetch_market_cap("AZN.L")
    assert cap == pytest.approx(184_433_710_838.64)


@pytest.mark.asyncio
async def test_build_ftse50_sorts_by_market_cap(monkeypatch):
    import nodes.ingestion as ing

    monkeypatch.setattr(ing, "fetch_ftse100_constituents", _big_table)
    caps = {f"T{i}.L": float(1000 - i) for i in range(60)}
    caps["T59.L"] = None  # missing cap ranks last

    async def fake_caps(tickers, max_workers=8):
        return {t: caps[t] for t in tickers}

    monkeypatch.setattr(ing, "fetch_market_caps", fake_caps)

    from config.settings import Settings
    companies = await ing.build_ftse50(Settings(), cache=None, limit=50)

    assert len(companies) == 50
    assert companies[0].ticker == "T0.L"  # largest cap first
    assert companies[0].market_cap == 1000.0
    # T59 (missing cap) must not be in the top 50.
    assert all(c.ticker != "T59.L" for c in companies)
    assert all(c.ticker.endswith(".L") for c in companies)
