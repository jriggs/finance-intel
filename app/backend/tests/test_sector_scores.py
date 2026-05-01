"""Test sector-specific macro and sentiment scoring."""
import sys
from unittest.mock import MagicMock, patch

# Mock imports before importing signals
sys.modules['db'] = MagicMock()
sys.modules['crawler'] = MagicMock()
sys.modules['ingestion'] = MagicMock()

import signals


def test_macro_score_by_sector():
    """Test macro score varies by sector."""
    # High rate environment (5% Fed Funds, normal yield curve)
    macro_snap = {
        "FedFunds": {"value": "5.0"},
        "YieldCurve": {"value": "0.7"},
        "VIX": {"value": "18"},
        "Unemployment": {"value": "4.0"},
        "CPI": {"value": "3.2"},
        "ConsumerSentiment": {"value": "75"},
    }

    # Tech should score LOW in high rate environment
    tech_score, tech_reasons = signals._macro_score(macro_snap, "Technology")

    # Financials should score HIGH in high rate environment
    fin_score, fin_reasons = signals._macro_score(macro_snap, "Financials")

    # Utilities should score MEDIUM (defensive)
    util_score, util_reasons = signals._macro_score(macro_snap, "Utilities")

    print("High Rate Environment (Fed 5%):")
    print(f"  Technology:  {tech_score} (rate sensitive) — {tech_reasons[:2]}")
    print(f"  Financials:  {fin_score} (rate beneficiary) — {fin_reasons[:2]}")
    print(f"  Utilities:   {util_score} (defensive) — {util_reasons[:2]}")

    assert tech_score < fin_score, f"Tech {tech_score} should score lower than Financials {fin_score} in high-rate env"
    assert fin_score >= util_score, f"Financials {fin_score} should score >= Utilities {util_score}"

    # Low rate environment (1% Fed Funds, steep curve)
    macro_snap_low_rates = {
        "FedFunds": {"value": "1.0"},
        "YieldCurve": {"value": "1.5"},
        "VIX": {"value": "12"},
        "Unemployment": {"value": "3.5"},
        "CPI": {"value": "2.1"},
        "ConsumerSentiment": {"value": "95"},
    }

    tech_score_low, _ = signals._macro_score(macro_snap_low_rates, "Technology")
    fin_score_low, _ = signals._macro_score(macro_snap_low_rates, "Financials")

    print("\nLow Rate Environment (Fed 1%):")
    print(f"  Technology:  {tech_score_low} (booming)")
    print(f"  Financials:  {fin_score_low} (margin squeeze)")

    assert tech_score_low > tech_score, "Tech should score higher in low-rate env"
    assert fin_score_low < fin_score, "Financials should score lower in low-rate env"
    print("✓ Sector-specific macro scoring works\n")


def test_sentiment_weighting():
    """Test symbol-specific sentiment weighted 75% over generic 25%."""
    # Setup: generic market has neutral sentiment, AAPL-specific very bullish
    with patch('db.get_crawl_results') as mock_crawl, \
         patch('db.get_sentiment') as mock_sentiment:
        # Generic market headlines: neutral
        generic_items = [
            {"title": "Market mixed today", "text": ""},
            {"title": "Fed pauses rate hikes", "text": ""},
        ]

        # AAPL-specific headlines: very bullish
        aapl_items = [
            {"title": "AAPL beats earnings, strong growth"},
            {"title": "AAPL upgrades from analyst, outperform rating"},
            {"title": "AAPL breaks above 200-day moving average"},
        ]

        def get_crawl_mock(source):
            if "finviz_AAPL" in source or "google_finance_AAPL" in source or "zacks_AAPL" in source:
                return {"items": aapl_items}
            if source in ("reddit", "marketwatch", "zacks", "yahoo", "finviz", "google_finance"):
                return {"items": generic_items}
            return {}

        mock_crawl.side_effect = get_crawl_mock
        mock_sentiment.return_value = None

        score, reasons = signals._sentiment_score("AAPL")

        print(f"AAPL Sentiment Score: {score}")
        print(f"  Reasons: {reasons}")

        # Should be heavily weighted toward bullish (symbol-specific is 75% of score)
        assert score >= 75, f"AAPL sentiment {score} should be >= 75 (3/3 positive symbol headlines)"
        assert "symbol-specific" in str(reasons), "Should mention symbol-specific weighting"
        print("✓ Symbol-specific sentiment weighted heavily\n")

        # Test opposite: bullish market but bearish AAPL
        bearish_aapl = [
            {"title": "AAPL stock drops on weak guidance"},
            {"title": "AAPL downgraded, sell recommendation"},
            {"title": "AAPL misses expectations, warning"},
        ]

        bullish_market = [
            {"title": "Market surge, strong bull rally"},
            {"title": "Economic growth outperforms expectations"},
            {"title": "Stocks breakout to new highs"},
        ]

        def get_crawl_mock2(source):
            if "finviz_AAPL" in source or "google_finance_AAPL" in source or "zacks_AAPL" in source:
                return {"items": bearish_aapl}
            if source in ("reddit", "marketwatch", "zacks", "yahoo", "finviz", "google_finance"):
                return {"items": bullish_market}
            return {}

        mock_crawl.side_effect = get_crawl_mock2

        score2, reasons2 = signals._sentiment_score("AAPL")

        print(f"AAPL Sentiment (bearish stock, bullish market): {score2}")
        print(f"  Reasons: {reasons2}")

        # Should be weighted down because symbol-specific is 75% and all bearish
        assert score2 < 50, f"AAPL sentiment {score2} should be < 50 (bearish symbol news dominates)"
        print("✓ Symbol-specific bearish sentiment overrides bullish market\n")


def test_no_regression_without_sector():
    """Test backward compatibility: macro score works without sector param."""
    macro_snap = {
        "FedFunds": {"value": "3.0"},
        "YieldCurve": {"value": "0.5"},
        "VIX": {"value": "20"},
    }

    # Should work with no sector (defaults to neutral weights)
    score, _ = signals._macro_score(macro_snap)

    assert 0 <= score <= 100, f"Score {score} out of range"
    print(f"✓ Backward compatible: macro_score() with no sector = {score}\n")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Sector-Specific & Stock-Specific Scoring")
    print("=" * 60 + "\n")

    test_macro_score_by_sector()
    test_sentiment_weighting()
    test_no_regression_without_sector()

    print("=" * 60)
    print("All tests passed!")
    print("=" * 60)
