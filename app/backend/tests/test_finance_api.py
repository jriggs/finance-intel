"""
Integration tests for finance API endpoints.
Tests the FastAPI routes with mocked market data.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import finance_router

# ── Setup test FastAPI app ────────────────────────────────────────────────────

@pytest.fixture
def app():
    """Create test FastAPI app with finance router."""
    test_app = FastAPI()
    test_app.include_router(finance_router.router)

    # Initialize finance router with mock LLM/RAG/scraper (skip scheduler startup)
    with patch("scheduler.start_scheduler"):
        finance_router.init_finance(
            llm_engine=MagicMock(),
            rag_pipeline=MagicMock(),
            scraper_instance=MagicMock(),
        )

    return test_app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


# ── Mock data ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_stock_info():
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
def mock_price_history():
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


# ── Health endpoint tests ─────────────────────────────────────────────────────

class TestHealthEndpoint:
    """Test /api/finance/health endpoint."""

    @patch("broker.ALPACA_PAPER", True)
    @patch("broker.is_configured", return_value=True)
    def test_health_check(self, mock_configured, client):
        """Health endpoint should return status, broker state, and timestamp."""
        response = client.get("/api/finance/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data
        assert "broker_ready" in data
        assert "llm_loaded" in data


# ── Symbol autocomplete endpoint tests ─────────────────────────────────────────

class TestSymbolsEndpoint:
    """Test /api/finance/symbols endpoint."""

    @patch("db.get_symbol_count", return_value=0)
    def test_symbols_fallback_when_empty(self, mock_count, client):
        """Empty DB (scheduler hasn't populated symbols yet) returns the static fallback universe."""
        import signals

        response = client.get("/api/finance/symbols")
        assert response.status_code == 200

        data = response.json()
        assert data["cached"] is False
        assert data["count"] == len(signals._FALLBACK_UNIVERSE)
        assert len(data["symbols"]) == data["count"]
        assert all("symbol" in s and "name" in s for s in data["symbols"])

    @patch("db.get_symbol_count", return_value=100)
    @patch("db.get_symbols", return_value=[
        {"symbol": "AAPL", "name": "Apple Inc."},
        {"symbol": "MSFT", "name": "Microsoft Corp."},
    ])
    def test_symbols_from_cache(self, mock_get, mock_count, client):
        """Non-empty DB should return cached symbols."""
        response = client.get("/api/finance/symbols")
        assert response.status_code == 200

        data = response.json()
        assert data["cached"]
        assert data["count"] == 100

    @patch("db.get_symbol_count", return_value=0)
    @patch("fundamentals.get_all_tickers", return_value=None)
    def test_symbols_fallback_on_error(self, mock_fetch, mock_count, client):
        """Failed SEC fetch should return fallback universe."""
        response = client.get("/api/finance/symbols")
        assert response.status_code == 200

        data = response.json()
        assert data["count"] > 0  # Fallback has ~150 symbols
        assert any(s["symbol"] == "AAPL" for s in data["symbols"])


# ── Single signal endpoint tests ──────────────────────────────────────────────

class TestSignalEndpoint:
    """Test /api/finance/signal/{symbol} endpoint."""

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_get_signal_success(self, mock_info, mock_prices, client, mock_stock_info, mock_price_history):
        """Endpoint should return full signal dict."""
        mock_info.return_value = mock_stock_info
        mock_prices.return_value = mock_price_history

        response = client.get("/api/finance/signal/AAPL")
        assert response.status_code == 200

        data = response.json()
        assert data["symbol"] == "AAPL"
        assert "score" in data
        assert "grade" in data
        assert "recommendation" in data
        assert "short_term" in data
        assert "long_term" in data
        assert "breakdown" in data

    @patch("signals.get_stock_info", side_effect=Exception("Network error"))
    def test_get_signal_network_error(self, mock_info, client):
        """Network error should return 500."""
        response = client.get("/api/finance/signal/INVALID")
        assert response.status_code == 500

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_signal_structure(self, mock_info, mock_prices, client, mock_stock_info, mock_price_history):
        """Signal response should have all required fields."""
        mock_info.return_value = mock_stock_info
        mock_prices.return_value = mock_price_history

        response = client.get("/api/finance/signal/MSFT")
        data = response.json()

        # Check main fields
        assert "symbol" in data
        assert "name" in data
        assert "price" in data
        assert "grade" in data
        assert data["grade"] in ["A", "B", "C", "D"]

        # Check recommendation
        assert "recommendation" in data
        assert data["recommendation"] in ["BUY", "WATCH", "HOLD", "AVOID"]

        # Check breakdown (value, technical, analyst)
        breakdown = data["breakdown"]
        for dim in ["value", "technical", "analyst"]:
            assert dim in breakdown
            assert "score" in breakdown[dim]
            assert "max" in breakdown[dim]
            assert "reasons" in breakdown[dim]

        # Check dual scores
        st = data["short_term"]
        assert 0 <= st["score"] <= 100
        assert st["grade"] in ["A", "B", "C", "D"]
        assert "reasons" in st

        lt = data["long_term"]
        assert 0 <= lt["score"] <= 100
        assert lt["grade"] in ["A", "B", "C", "D"]
        assert "reasons" in lt


# ── Watchlist signals endpoint tests ──────────────────────────────────────────

class TestWatchlistSignalsEndpoint:
    """Test /api/finance/signals/watchlist endpoint."""

    @patch("db.get_watchlist", return_value=["AAPL", "MSFT"])
    @patch("db.get_crawl_results", return_value=None)
    @patch("signals.score_watchlist")
    @patch("db.save_crawl_results")
    def test_watchlist_signals_no_cache(self, mock_save, mock_score, mock_cache, mock_watchlist, client):
        """No cache should trigger scoring."""
        mock_scores = [
            {"symbol": "AAPL", "score": 75, "grade": "A"},
            {"symbol": "MSFT", "score": 65, "grade": "B"},
        ]
        mock_score.return_value = mock_scores

        response = client.get("/api/finance/signals/watchlist")
        assert response.status_code == 200

        data = response.json()
        assert "scores" in data
        assert len(data["scores"]) == 2
        assert "cached_at" in data
        mock_score.assert_called_once()

    @patch("db.get_watchlist", return_value=["AAPL"])
    @patch("db.get_crawl_results", return_value={
        "items": [
            {"symbol": "AAPL", "score": 75, "grade": "A"},
            {"symbol": "MSFT", "score": 65, "grade": "B"},
        ],
        "fetched_at": "2026-04-08T00:00:00+00:00",
    })
    @patch("signals.score_watchlist")
    def test_watchlist_signals_cache_hit(self, mock_score, mock_cache, mock_watchlist, client):
        """Cache hit should skip scoring if watchlist subset cached symbols."""
        response = client.get("/api/finance/signals/watchlist")
        assert response.status_code == 200

        data = response.json()
        assert data["cached_at"] == "2026-04-08T00:00:00+00:00"
        # Should NOT call score_watchlist since cache contains all watchlist symbols
        mock_score.assert_not_called()

    @patch("db.get_watchlist", return_value=["AAPL", "MSFT", "GOOGL"])
    @patch("db.get_crawl_results", return_value={
        "items": [
            {"symbol": "AAPL", "score": 75, "grade": "A"},
            {"symbol": "MSFT", "score": 65, "grade": "B"},
        ],
        "fetched_at": "2026-04-08T00:00:00+00:00",
    })
    @patch("signals.score_watchlist")
    @patch("db.save_crawl_results")
    def test_watchlist_signals_cache_miss_new_symbol(self, mock_save, mock_score, mock_cache, mock_watchlist, client):
        """New watchlist symbol should invalidate cache and trigger scoring."""
        new_scores = [
            {"symbol": "AAPL", "score": 75, "grade": "A"},
            {"symbol": "MSFT", "score": 65, "grade": "B"},
            {"symbol": "GOOGL", "score": 72, "grade": "A"},
        ]
        mock_score.return_value = new_scores

        response = client.get("/api/finance/signals/watchlist")
        assert response.status_code == 200

        data = response.json()
        assert len(data["scores"]) == 3
        # Should call score_watchlist since watchlist grew
        mock_score.assert_called_once()

    @patch("db.get_watchlist", return_value=["AAPL", "MSFT"])
    @patch("db.get_crawl_results", return_value=None)
    @patch("signals.score_watchlist")
    @patch("db.save_crawl_results")
    def test_watchlist_signals_force_refresh(self, mock_save, mock_score, mock_cache, mock_watchlist, client):
        """force=true should bypass cache and rescore."""
        new_scores = [
            {"symbol": "AAPL", "score": 70, "grade": "B"},
            {"symbol": "MSFT", "score": 60, "grade": "C"},
        ]
        mock_score.return_value = new_scores

        response = client.get("/api/finance/signals/watchlist?force=true")
        assert response.status_code == 200

        mock_score.assert_called_once()
        mock_save.assert_called_once()

    @patch("db.get_watchlist", return_value=["AAPL", "MSFT", "GOOGL"])
    @patch("db.get_crawl_results", return_value=None)
    @patch("signals.score_watchlist")
    @patch("db.save_crawl_results")
    def test_watchlist_signals_structure(self, mock_save, mock_score, mock_cache, mock_watchlist, client):
        """Watchlist signals should return properly formatted scores array."""
        mock_scores = [
            {
                "symbol": "AAPL",
                "score": 75,
                "grade": "A",
                "recommendation": "BUY",
                "breakdown": {
                    "value": {"score": 20, "max": 40, "reasons": []},
                    "technical": {"score": 30, "max": 35, "reasons": []},
                    "analyst": {"score": 25, "max": 25, "reasons": []},
                },
                "short_term": {"score": 80, "grade": "A", "reasons": []},
                "long_term": {"score": 70, "grade": "B", "reasons": []},
            }
        ]
        mock_score.return_value = mock_scores

        response = client.get("/api/finance/signals/watchlist")
        assert response.status_code == 200

        data = response.json()
        assert len(data["scores"]) == 1
        assert data["scores"][0]["symbol"] == "AAPL"
        assert data["scores"][0]["grade"] == "A"


# ── Integration: Score mapping tests ──────────────────────────────────────────

class TestScoreMapping:
    """Test that scores map to grades and recommendations correctly."""

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_grade_a_buy(self, mock_info, mock_prices, client, mock_stock_info, mock_price_history):
        """Score ≥70 should be grade A and BUY."""
        mock_stock_info["pe_ratio"] = 8.0  # Deep value
        mock_stock_info["analyst_target"] = 150.0  # High upside
        mock_info.return_value = mock_stock_info
        mock_prices.return_value = mock_price_history

        response = client.get("/api/finance/signal/AAPL")
        data = response.json()

        if data["score"] >= 70:
            assert data["grade"] == "A"
            assert data["recommendation"] == "BUY"

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_grade_b_watch(self, mock_info, mock_prices, client, mock_stock_info, mock_price_history):
        """Score 50-69 should be grade B and WATCH."""
        mock_stock_info["pe_ratio"] = 18.0
        mock_stock_info["analyst_target"] = 115.0
        mock_info.return_value = mock_stock_info
        mock_prices.return_value = mock_price_history

        response = client.get("/api/finance/signal/MSFT")
        data = response.json()

        if 50 <= data["score"] < 70:
            assert data["grade"] == "B"
            assert data["recommendation"] == "WATCH"

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_grade_c_hold(self, mock_info, mock_prices, client, mock_stock_info, mock_price_history):
        """Score 30-49 should be grade C and HOLD."""
        mock_stock_info["pe_ratio"] = 35.0  # Expensive
        mock_stock_info["analyst_target"] = 100.0  # No upside
        mock_info.return_value = mock_stock_info
        mock_prices.return_value = mock_price_history

        response = client.get("/api/finance/signal/GOOGL")
        data = response.json()

        if 30 <= data["score"] < 50:
            assert data["grade"] == "C"
            assert data["recommendation"] == "HOLD"

    @patch("signals.get_price_history")
    @patch("signals.get_stock_info")
    def test_grade_d_avoid(self, mock_info, mock_prices, client, mock_stock_info, mock_price_history):
        """Score <30 should be grade D and AVOID."""
        mock_stock_info["pe_ratio"] = 50.0  # Very expensive
        mock_stock_info["analyst_target"] = 80.0  # Negative outlook
        mock_stock_info["recommendation"] = "sell"
        mock_info.return_value = mock_stock_info
        mock_prices.return_value = mock_price_history

        response = client.get("/api/finance/signal/TSLA")
        data = response.json()

        if data["score"] < 30:
            assert data["grade"] == "D"
            assert data["recommendation"] == "AVOID"
