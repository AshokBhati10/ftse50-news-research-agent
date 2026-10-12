"""Deterministic, explainable source-quality ranking.

The LLM never decides source trustworthiness: every article gets a score in
[0, 1] purely from its domain against curated tier lists. The tier name is
kept on the article so rankings stay auditable.

Tiers (higher is better):
  official   (1.00) — company investor-relations pages and market regulators
  reputable  (0.80) — established financial/news publications
  mainstream (0.60) — established general/business press
  other      (0.40) — anything not recognised
  low        (0.15) — obvious SEO farms / scraped / aggregator spam
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Tier domain lists (suffix match, e.g. "edition.cnn.com" matches "cnn.com")
# ---------------------------------------------------------------------------

_OFFICIAL_PATTERNS = (
    "londonstockexchange.com",
    "sec.gov",
    "fca.org.uk",
    "bankofengland.co.uk",
    "euronext.com",
)

_REPUTABLE_FINANCIAL = (
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "barrons.com",
    "marketwatch.com",
    "cnbc.com",
    "economist.com",
    "ftadviser.com",
    "morningstar.com",
    "fool.co.uk",
    "fool.com",
    "investing.com",
    "finance.yahoo.com",
    "uk.finance.yahoo.com",
    "tipranks.com",
    "zacks.com",
    "seekingalpha.com",
    "thisismoney.co.uk",
    "moneyweek.com",
    "investorschronicle.co.uk",
    "cityam.com",
    "financial-news.co.uk",
)

_MAINSTREAM_PRESS = (
    "bbc.com",
    "bbc.co.uk",
    "theguardian.com",
    "independent.co.uk",
    "telegraph.co.uk",
    "thetimes.co.uk",
    "nytimes.com",
    "thetimes.com",
    "apnews.com",
    "sky.com",
    "news.sky.com",
    "standard.co.uk",
    "express.co.uk",
    "mirror.co.uk",
    "dailymail.co.uk",
    "forbes.com",
    "fortune.com",
    "cnn.com",
    "nbcnews.com",
    "abcnews.go.com",
    "cbsnews.com",
    "aljazeera.com",
    "dw.com",
    "euronews.com",
    "politico.eu",
)

_LOW_QUALITY_HINTS = (
    "content farm",
    "scraped",
    "aggregator",
)

# Keyword fragments that strongly suggest SEO/spam domains.
_LOW_QUALITY_DOMAIN_FRAGMENTS = (
    "top10",
    "best-deals",
    "coupon",
    "casinobonus",
    "betting",
    "porn",
    "xxx",
    "torrent",
    "warez",
)


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _matches(domain: str, suffixes: tuple[str, ...]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def score_source(url: str) -> tuple[float, str, str]:
    """Return (score, tier, reason) for an article URL.

    Deterministic and explainable — no LLM involved. Curated publication
    lists are checked *before* the generic IR heuristics so a known outlet
    (e.g. investorschronicle.co.uk) can never be misread as an official
    company page just because its name contains "investor".
    """
    domain = _domain_of(url)
    if not domain:
        return 0.2, "other", "unparseable URL"

    if _matches(domain, _REPUTABLE_FINANCIAL):
        return 0.8, "reputable", f"{domain}: established financial publication"

    if _matches(domain, _MAINSTREAM_PRESS):
        return 0.6, "mainstream", f"{domain}: established mainstream press"

    # Official investor-relations / regulatory pages. Match "ir"/"investor"/
    # "investors" as whole domain labels (split on "." and "-"), never as a
    # raw substring — otherwise "investorschronicle" would false-positive.
    parts = [p for label in domain.split(".") for p in label.split("-")]
    if (
        _matches(domain, _OFFICIAL_PATTERNS)
        or parts[0] == "ir"
        or "investor" in parts
        or "investors" in parts
    ):
        return 1.0, "official", f"{domain}: official/IR or regulatory source"

    if any(frag in domain for frag in _LOW_QUALITY_DOMAIN_FRAGMENTS):
        return 0.15, "low", f"{domain}: low-quality/spam indicators"

    return 0.4, "other", f"{domain}: unrecognised source"


# Section/hub pages ("press releases", "annual reports", ...) are navigation,
# not developments. They are legitimate but rarely answer a specific question.
_SECTION_PAGE_RE = re.compile(
    r"\b(press releases?|annual reports?|media cent(?:re|er)|newsroom|"
    r"investor relations|publications|news & events|events calendar)\b",
    re.I,
)


def is_section_page(title: str, text: str = "") -> bool:
    """Heuristic: does this title/text describe a section/index page?"""
    return bool(_SECTION_PAGE_RE.search(f"{title or ''} {text or ''}"))


def rank_by_source_quality(articles: list) -> list:
    """Sort articles in place by (source_quality, has_date) descending, stable."""
    for a in articles:
        score, tier, _reason = score_source(a.url)
        a.source_quality = score
        a.source_quality_tier = tier
    articles.sort(
        key=lambda a: (a.source_quality, a.published_at is not None),
        reverse=True,
    )
    return articles
