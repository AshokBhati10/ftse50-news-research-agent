"""Company-specific search query construction.

Strategy: one strong primary query first; refined variants only when the
primary search comes back insufficient (keeps external search usage minimal).
"""

from __future__ import annotations

from utils.models import Company

# Materiality-oriented terms bias results toward market-moving stories rather
# than lifestyle/SEO content about the company.
_PRIMARY_TEMPLATE = (
    "{company} ({ticker}) financial news earnings results acquisition guidance "
    "regulation market impact"
)

_REFINED_TEMPLATES = [
    "{company} {ticker} stock news earnings profit warning merger",
    "{company} latest news investors shareholders announcement",
    "{company} results guidance dividend buyback outlook",
]


def build_primary_query(company: Company) -> str:
    ticker_short = company.ticker.replace(".L", "")
    return _PRIMARY_TEMPLATE.format(company=company.name, ticker=ticker_short)


def build_refined_queries(company: Company, limit: int = 2) -> list[str]:
    ticker_short = company.ticker.replace(".L", "")
    return [
        t.format(company=company.name, ticker=ticker_short)
        for t in _REFINED_TEMPLATES[:limit]
    ]
