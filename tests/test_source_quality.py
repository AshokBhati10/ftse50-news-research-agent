"""Tests for utils/source_quality.py — deterministic source ranking."""

import pytest

from utils.models import Article
from utils.source_quality import rank_by_source_quality, score_source


@pytest.mark.parametrize(
    "url,expected_tier,min_score",
    [
        ("https://www.reuters.com/business/x", "reputable", 0.8),
        ("https://www.bloomberg.com/news/y", "reputable", 0.8),
        ("https://www.ft.com/content/z", "reputable", 0.8),
        ("https://www.bbc.com/news/business-1", "mainstream", 0.6),
        ("https://www.londonstockexchange.com/news/article", "official", 1.0),
        ("https://ir.astrazeneca.com/press-release", "official", 1.0),
        ("https://shell-investors.example.com/update", "official", 1.0),
        ("https://random-blog-12345.example.com/post", "other", 0.4),
    ],
)
def test_score_source_tiers(url, expected_tier, min_score):
    score, tier, reason = score_source(url)
    assert tier == expected_tier
    assert score >= min_score
    assert reason  # explainable


def test_investors_chronicle_is_reputable_not_official():
    """Regression: 'investor' as a substring must not imply an IR page."""
    score, tier, _ = score_source("https://www.investorschronicle.co.uk/azn-dividend-2026")
    assert tier == "reputable"
    assert score == 0.8


def test_official_outranks_reputable_outranks_other():
    arts = [
        Article(company="X", ticker="X.L", title="a", url="https://random.example.com/1"),
        Article(company="X", ticker="X.L", title="b", url="https://www.reuters.com/2"),
        Article(company="X", ticker="X.L", title="c",
                url="https://www.londonstockexchange.com/3"),
    ]
    rank_by_source_quality(arts)
    assert [a.url for a in arts] == [
        "https://www.londonstockexchange.com/3",
        "https://www.reuters.com/2",
        "https://random.example.com/1",
    ]


def test_scoring_is_deterministic():
    first = score_source("https://www.reuters.com/x")
    second = score_source("https://www.reuters.com/x")
    assert first == second
