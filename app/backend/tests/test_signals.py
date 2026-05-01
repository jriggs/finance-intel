"""
Unit tests for trading signal engine (signals.py).
Tests all scoring functions with mocked market data.
"""

from unittest.mock import patch

import pandas as pd
import pytest

from signals import (
    _analyst_score,
    _long_term_score,
    _short_term_score,
    _technical_score,
    _value_score,
    score_stock,
)

# ── Mock data fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def basic_info():
    """Stock info with no special conditions."""
    return {
        "name": "Test Corp",
        "price": 100.0,
        "pe_ratio": 15.0,
        "pb_ratio": 1.5,
        "ev_ebitda": 10.0,
        "revenue_growth": 0.15,
        "free_cashflow": 1e9,
        "debt_equity": 0.5,
        "insider_pct": 0.08,
        "volume": 1e7,
        "avg_volume": 1e7,
        "52w_low": 80.0,
        "52w_high": 120.0,
        "analyst_target": 110.0,
        "num_analysts": 12,
        "recommendation": "buy",
        "sector": "Technology",
    }


@pytest.fixture
def price_history_6mo():
    """6-month price history with technical indicators."""
    dates = pd.date_range("2025-10-01", periods=125, freq="D")
    return pd.DataFrame({
        "close": [100 + i * 0.5 for i in range(125)],
        "volume": [1e7] * 125,
        "rsi": [50.0] * 125,
        "sma20": [102 + i * 0.4 for i in range(125)],
        "sma50": [101 + i * 0.3 for i in range(125)],
        "sma200": [98 + i * 0.2 for i in range(125)],
        "macd": [1.0] * 125,
        "macd_signal": [0.5] * 125,
    }, index=dates)


@pytest.fixture
def price_history_1mo():
    """1-month price history for short-term scoring."""
    dates = pd.date_range("2026-03-01", periods=21, freq="D")
    return pd.DataFrame({
        "close": [100 + i * 1.0 for i in range(21)],
        "volume": [1.5e7] * 21,
        "rsi": [55.0] * 21,
        "sma20": [105 + i * 0.5 for i in range(21)],
        "macd": [2.0] * 21,
        "macd_signal": [1.0] * 21,
    }, index=dates)


@pytest.fixture
def empty_info():
    """Stock info with no metrics."""
    return {}


# ── _value_score tests ──────────────────────────────────────────────────────────

class TestValueScore:
    """Test fundamental valuation scoring."""

    def test_deeply_undervalued(self, basic_info):
        """P/E < 12 should award 18 points."""
        basic_info["pe_ratio"] = 10.0
        score, reasons = _value_score(basic_info)
        assert score >= 18
        assert any("deeply undervalued" in r for r in reasons)

    def test_reasonably_valued(self, basic_info):
        """P/E 12-20 should award 12 points."""
        basic_info["pe_ratio"] = 15.0
        score, reasons = _value_score(basic_info)
        assert score >= 12
        assert any("reasonably valued" in r for r in reasons)

    def test_below_book_value(self, basic_info):
        """P/B < 1.0 should award 12 points."""
        basic_info["pb_ratio"] = 0.8
        score, reasons = _value_score(basic_info)
        assert score >= 12
        assert any("below book value" in r for r in reasons)

    def test_cheap_ebitda(self, basic_info):
        """EV/EBITDA < 8 should award 6 points."""
        basic_info["ev_ebitda"] = 7.0
        score, _ = _value_score(basic_info)
        assert score >= 6

    def test_high_revenue_growth(self, basic_info):
        """Revenue growth > 20% should award 4 points."""
        basic_info["revenue_growth"] = 0.25
        score, reasons = _value_score(basic_info)
        assert score >= 4
        assert any("Revenue" in r for r in reasons)

    def test_positive_fcf(self, basic_info):
        """Positive FCF should award 4 points."""
        basic_info["free_cashflow"] = 1e9
        score, reasons = _value_score(basic_info)
        assert score >= 4
        assert any("free cash flow" in r for r in reasons)

    def test_low_debt(self, basic_info):
        """D/E < 0.5 should award 3 points."""
        basic_info["debt_equity"] = 0.3
        score, reasons = _value_score(basic_info)
        assert score >= 3
        assert any("Low debt" in r for r in reasons)

    def test_high_leverage(self, basic_info):
        """D/E > 2.0 should deduct 3 points."""
        basic_info["debt_equity"] = 2.5
        _, reasons = _value_score(basic_info)
        assert any("High leverage" in r for r in reasons)

    def test_insider_ownership(self, basic_info):
        """Insider ownership > 5% should award 3 points."""
        basic_info["insider_pct"] = 0.10
        score, reasons = _value_score(basic_info)
        assert score >= 3
        assert any("Insiders own" in r for r in reasons)

    def test_max_score_capped(self, basic_info):
        """Score should be capped at 40."""
        score, _ = _value_score(basic_info)
        assert score <= 40

    def test_empty_info(self, empty_info):
        """Empty info should return 0 score."""
        score, reasons = _value_score(empty_info)
        assert score == 0
        assert len(reasons) == 0

    def test_invalid_pe_ratio(self, empty_info):
        """Zero or negative P/E should be skipped."""
        empty_info["pe_ratio"] = 0
        score, _ = _value_score(empty_info)
        assert score == 0

        empty_info["pe_ratio"] = -5
        score, _ = _value_score(empty_info)
        assert score == 0


# ── _analyst_score tests ────────────────────────────────────────────────────────

class TestAnalystScore:
    """Test analyst consensus scoring."""

    def test_strong_upside(self, basic_info):
        """Target > 25% above price should award 15 points."""
        basic_info["price"] = 100.0
        basic_info["analyst_target"] = 130.0
        score, reasons = _analyst_score(basic_info)
        assert score >= 15
        assert any("upside" in r for r in reasons)

    def test_moderate_upside(self, basic_info):
        """Target 10-25% above price should award 8 points."""
        basic_info["price"] = 100.0
        basic_info["analyst_target"] = 115.0
        score, _ = _analyst_score(basic_info)
        assert score >= 8

    def test_below_target(self, basic_info):
        """Target 5% below price should deduct 5 points."""
        basic_info["price"] = 100.0
        basic_info["analyst_target"] = 94.0
        _, reasons = _analyst_score(basic_info)
        assert any("below current price" in r for r in reasons)

    def test_strong_buy_recommendation(self, basic_info):
        """'strong_buy' rec should award 10 points."""
        basic_info["recommendation"] = "strong_buy"
        score, reasons = _analyst_score(basic_info)
        assert score >= 10
        assert any("Strong Buy" in r for r in reasons)

    def test_buy_recommendation(self, basic_info):
        """'buy' rec should award 10 points."""
        basic_info["recommendation"] = "buy"
        score, _ = _analyst_score(basic_info)
        assert score >= 10

    def test_hold_recommendation(self, basic_info):
        """'hold' rec should award 3 points."""
        basic_info["recommendation"] = "hold"
        score, _ = _analyst_score(basic_info)
        assert score >= 3

    def test_sell_recommendation(self, basic_info):
        """'sell' rec should deduct 5 points."""
        basic_info["recommendation"] = "sell"
        _, reasons = _analyst_score(basic_info)
        assert any("Sell" in r for r in reasons)

    def test_analyst_coverage(self, basic_info):
        """10+ analysts should award 3 points."""
        basic_info["num_analysts"] = 15
        score, reasons = _analyst_score(basic_info)
        assert score >= 3
        assert any("15 analysts" in r for r in reasons)

    def test_max_score_capped(self, basic_info):
        """Score should be capped at 25."""
        score, _ = _analyst_score(basic_info)
        assert score <= 25

    def test_empty_info(self, empty_info):
        """Empty info should return 0 score."""
        score, _ = _analyst_score(empty_info)
        assert score == 0


# ── _technical_score tests ──────────────────────────────────────────────────────

class TestTechnicalScore:
    """Test technical analysis scoring."""

    @patch("signals.get_price_history")
    def test_rsi_oversold_recovery(self, mock_price_hist, basic_info, price_history_6mo):
        """RSI 30-45 (recovering from oversold) should award 15 points."""
        mock_price_hist.return_value = price_history_6mo.copy()
        price_history_6mo.loc[price_history_6mo.index[-1], "rsi"] = 40.0
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _technical_score(basic_info, "TEST")
        assert score >= 15
        assert any("recovering from oversold" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_rsi_overbought(self, mock_price_hist, basic_info, price_history_6mo):
        """RSI > 70 should deduct 5 points."""
        price_history_6mo.loc[price_history_6mo.index[-1], "rsi"] = 75.0
        mock_price_hist.return_value = price_history_6mo

        _, reasons = _technical_score(basic_info, "TEST")
        assert any("overbought" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_above_200_sma(self, mock_price_hist, basic_info, price_history_6mo):
        """Price > 200 SMA should award 8 points."""
        price_history_6mo.loc[price_history_6mo.index[-1], "close"] = 105.0
        price_history_6mo.loc[price_history_6mo.index[-1], "sma200"] = 100.0
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _technical_score(basic_info, "TEST")
        assert score >= 8
        assert any("200-day SMA" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_golden_cross(self, mock_price_hist, basic_info, price_history_6mo):
        """SMA50 > SMA200 should award 5 points."""
        price_history_6mo.loc[price_history_6mo.index[-1], "sma50"] = 105.0
        price_history_6mo.loc[price_history_6mo.index[-1], "sma200"] = 100.0
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _technical_score(basic_info, "TEST")
        assert score >= 5
        assert any("Golden cross" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_near_52w_low(self, mock_price_hist, basic_info, price_history_6mo):
        """Price near 52-week low should award 10 points."""
        basic_info["52w_low"] = 80.0
        basic_info["52w_high"] = 120.0
        price_history_6mo.loc[price_history_6mo.index[-1], "close"] = 85.0  # 5.6% from low
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _technical_score(basic_info, "TEST")
        assert score >= 10
        assert any("52-week low" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_near_52w_high(self, mock_price_hist, basic_info, price_history_6mo):
        """Price near 52-week high should deduct 3 points."""
        basic_info["52w_low"] = 80.0
        basic_info["52w_high"] = 120.0
        price_history_6mo.loc[price_history_6mo.index[-1], "close"] = 118.0  # 95% from low
        mock_price_hist.return_value = price_history_6mo

        _, reasons = _technical_score(basic_info, "TEST")
        assert any("52-week high" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_volume_surge(self, mock_price_hist, basic_info, price_history_6mo):
        """Volume > 1.5x average should award 5 points."""
        basic_info["volume"] = 2e7
        basic_info["avg_volume"] = 1e7
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _technical_score(basic_info, "TEST")
        assert score >= 5
        assert any("volume" in r.lower() for r in reasons)

    @patch("signals.get_price_history")
    def test_macd_bullish(self, mock_price_hist, basic_info, price_history_6mo):
        """MACD > signal should award 5 points."""
        price_history_6mo.loc[price_history_6mo.index[-1], "macd"] = 2.0
        price_history_6mo.loc[price_history_6mo.index[-1], "macd_signal"] = 1.0
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _technical_score(basic_info, "TEST")
        assert score >= 5
        assert any("MACD bullish" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_empty_price_history(self, mock_price_hist, basic_info):
        """Empty price history should return 0 score."""
        mock_price_hist.return_value = pd.DataFrame()

        score, reasons = _technical_score(basic_info, "TEST")
        assert score == 0
        assert len(reasons) == 0

    @patch("signals.get_price_history")
    def test_score_bounded(self, mock_price_hist, basic_info, price_history_6mo):
        """Score should be bounded 0-35."""
        mock_price_hist.return_value = price_history_6mo
        score, _ = _technical_score(basic_info, "TEST")
        assert 0 <= score <= 35


# ── _short_term_score tests ─────────────────────────────────────────────────────

class TestShortTermScore:
    """Test 1-30 day momentum scoring."""

    @patch("signals.get_price_history")
    def test_rsi_recovery(self, mock_price_hist, basic_info, price_history_1mo):
        """RSI 30-45 should award 20 points."""
        price_history_1mo.loc[price_history_1mo.index[-1], "rsi"] = 40.0
        mock_price_hist.return_value = price_history_1mo

        score, reasons = _short_term_score(basic_info, "TEST")
        assert score >= 20
        assert any("recovering from oversold" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_macd_bullish_momentum(self, mock_price_hist, basic_info, price_history_1mo):
        """MACD > signal should award 15 points."""
        price_history_1mo.loc[price_history_1mo.index[-1], "macd"] = 3.0
        price_history_1mo.loc[price_history_1mo.index[-1], "macd_signal"] = 1.0
        mock_price_hist.return_value = price_history_1mo

        score, reasons = _short_term_score(basic_info, "TEST")
        assert score >= 15
        assert any("MACD bullish" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_above_sma20(self, mock_price_hist, basic_info, price_history_1mo):
        """Price > SMA20 should award 12 points."""
        price_history_1mo.loc[price_history_1mo.index[-1], "close"] = 115.0
        price_history_1mo.loc[price_history_1mo.index[-1], "sma20"] = 110.0
        mock_price_hist.return_value = price_history_1mo

        score, reasons = _short_term_score(basic_info, "TEST")
        assert score >= 12
        assert any("20-day SMA" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_volume_breakout(self, mock_price_hist, basic_info, price_history_1mo):
        """Volume > 2.0x average should award 15 points."""
        basic_info["volume"] = 2.5e7
        basic_info["avg_volume"] = 1e7
        mock_price_hist.return_value = price_history_1mo

        score, reasons = _short_term_score(basic_info, "TEST")
        assert score >= 15
        assert any("Volume" in r and "breakout" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_week_momentum_up(self, mock_price_hist, basic_info, price_history_1mo):
        """Up >5% in 1 week should award 10 points."""
        # Last price 20, 1-week-ago price (5 days back) 10
        price_history_1mo.loc[price_history_1mo.index[-1], "close"] = 120.0
        price_history_1mo.loc[price_history_1mo.index[-6], "close"] = 110.0
        mock_price_hist.return_value = price_history_1mo

        score, reasons = _short_term_score(basic_info, "TEST")
        assert score >= 10
        assert any("week" in r.lower() for r in reasons)

    @patch("signals.get_price_history")
    def test_score_bounded(self, mock_price_hist, basic_info, price_history_1mo):
        """Score should be bounded 0-100."""
        mock_price_hist.return_value = price_history_1mo
        score, _ = _short_term_score(basic_info, "TEST")
        assert 0 <= score <= 100

    @patch("signals.get_price_history")
    def test_empty_price_history(self, mock_price_hist, basic_info):
        """Empty price history should return 0 score."""
        mock_price_hist.return_value = pd.DataFrame()

        score, _ = _short_term_score(basic_info, "TEST")
        assert score == 0


# ── _long_term_score tests ──────────────────────────────────────────────────────

class TestLongTermScore:
    """Test long-term fundamentals + technicals scoring."""

    @patch("signals.get_price_history")
    def test_combines_value_and_analyst(self, mock_price_hist, basic_info, price_history_6mo):
        """Long-term should include value_score + analyst_score."""
        mock_price_hist.return_value = price_history_6mo

        score, reasons = _long_term_score(basic_info, "TEST")

        # Should have reasons from value (P/E), analyst, and technicals
        assert any("P/E" in r for r in reasons) or any("analyst" in r.lower() for r in reasons)
        assert 0 <= score <= 100

    @patch("signals.get_price_history")
    def test_above_200sma_long(self, mock_price_hist, basic_info, price_history_6mo):
        """Price > 200 SMA should award 12 points."""
        price_history_6mo.loc[price_history_6mo.index[-1], "close"] = 105.0
        price_history_6mo.loc[price_history_6mo.index[-1], "sma200"] = 100.0
        mock_price_hist.return_value = price_history_6mo

        _, reasons = _long_term_score(basic_info, "TEST")
        assert any("200-day SMA" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_golden_cross_long(self, mock_price_hist, basic_info, price_history_6mo):
        """Golden cross should award 8 points."""
        price_history_6mo.loc[price_history_6mo.index[-1], "sma50"] = 105.0
        price_history_6mo.loc[price_history_6mo.index[-1], "sma200"] = 100.0
        mock_price_hist.return_value = price_history_6mo

        _, reasons = _long_term_score(basic_info, "TEST")
        assert any("Golden cross" in r for r in reasons)

    @patch("signals.get_price_history")
    def test_score_bounded(self, mock_price_hist, basic_info, price_history_6mo):
        """Score should be bounded 0-100."""
        mock_price_hist.return_value = price_history_6mo
        score, _ = _long_term_score(basic_info, "TEST")
        assert 0 <= score <= 100


# ── score_stock integration tests ──────────────────────────────────────────────

class TestScoreStock:
    """Test full signal analysis."""

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_returns_full_dict(self, mock_info, mock_price_hist, basic_info, price_history_6mo):
        """score_stock should return complete signal dict."""
        mock_info.return_value = basic_info
        mock_price_hist.return_value = price_history_6mo

        result = score_stock("TEST")

        # Check all required fields
        assert result["symbol"] == "TEST"
        assert "score" in result
        assert "grade" in result
        assert "recommendation" in result
        assert "breakdown" in result
        assert "short_term" in result
        assert "long_term" in result
        assert "all_reasons" in result

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_grade_a_high_score(self, mock_info, mock_price_hist, basic_info, price_history_6mo):
        """Score ≥70 should be grade A."""
        basic_info["pe_ratio"] = 10.0  # High value score
        basic_info["analyst_target"] = 140.0  # High analyst upside
        mock_info.return_value = basic_info
        mock_price_hist.return_value = price_history_6mo

        result = score_stock("TEST")
        if result["score"] >= 70:
            assert result["grade"] == "A"

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_recommendation_mapping(self, mock_info, mock_price_hist, basic_info, price_history_6mo):
        """Recommendations should map to scores correctly."""
        mock_info.return_value = basic_info
        mock_price_hist.return_value = price_history_6mo

        result = score_stock("TEST")

        score = result["score"]
        if score >= 70:
            assert result["recommendation"] == "BUY"
        elif score >= 50:
            assert result["recommendation"] == "WATCH"
        elif score >= 30:
            assert result["recommendation"] == "HOLD"
        else:
            assert result["recommendation"] == "AVOID"

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_breakdown_structure(self, mock_info, mock_price_hist, basic_info, price_history_6mo):
        """Breakdown should have value, technical, analyst with scores and reasons."""
        mock_info.return_value = basic_info
        mock_price_hist.return_value = price_history_6mo

        result = score_stock("TEST")

        breakdown = result["breakdown"]
        for dim in ["value", "technical", "analyst"]:
            assert dim in breakdown
            assert "score" in breakdown[dim]
            assert "max" in breakdown[dim]
            assert "reasons" in breakdown[dim]

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_short_term_grade(self, mock_info, mock_price_hist, basic_info, price_history_6mo):
        """Short-term should have score and grade."""
        mock_info.return_value = basic_info
        mock_price_hist.return_value = price_history_6mo

        result = score_stock("TEST")

        st = result["short_term"]
        assert 0 <= st["score"] <= 100
        assert st["grade"] in ["A", "B", "C", "D"]

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_long_term_grade(self, mock_info, mock_price_hist, basic_info, price_history_6mo):
        """Long-term should have score and grade."""
        mock_info.return_value = basic_info
        mock_price_hist.return_value = price_history_6mo

        result = score_stock("TEST")

        lt = result["long_term"]
        assert 0 <= lt["score"] <= 100
        assert lt["grade"] in ["A", "B", "C", "D"]
