"""
Characterization tests for the db persistence layer.

These lock the observable behavior of every domain's read/write round-trips
against a throwaway SQLite file, so the upcoming split of the db god-module into
a package can be verified to preserve behavior exactly.

The `db` fixture is deliberately structure-agnostic: it patches DB_PATH on
whichever module actually defines `_get_conn` (the monolithic `db` today, or
`db.connection` after the split), so the same tests pass before and after.
"""
import sys

import pytest


@pytest.fixture
def db(tmp_path):
    """Point the db layer at a fresh temp database for the duration of one test."""
    import db as db_mod

    # The module that owns the connection (and therefore reads DB_PATH).
    conn_mod = sys.modules[db_mod._get_conn.__module__]

    orig_path = conn_mod.DB_PATH
    orig_conn = getattr(conn_mod._local, "conn", None)

    conn_mod.DB_PATH = tmp_path / "test.db"
    if hasattr(conn_mod._local, "conn"):
        del conn_mod._local.conn  # force a reconnect to the temp DB
    db_mod._init_schema()
    db_mod._migrate()

    try:
        yield db_mod
    finally:
        temp_conn = getattr(conn_mod._local, "conn", None)
        if temp_conn is not None and temp_conn is not orig_conn:
            temp_conn.close()
        conn_mod.DB_PATH = orig_path
        if orig_conn is not None:
            conn_mod._local.conn = orig_conn
        elif hasattr(conn_mod._local, "conn"):
            del conn_mod._local.conn


# ── Public surface ──────────────────────────────────────────────────────────────

PUBLIC_FUNCTIONS = [
    # watchlist
    "get_watchlist", "add_to_watchlist", "remove_from_watchlist",
    # picks
    "get_picks", "get_open_picks", "update_pick_prices", "add_pick",
    "close_pick", "get_pick_stats",
    # symbols
    "save_symbols", "save_symbol_avg_volumes", "get_symbols", "get_symbol_count",
    "get_symbol_names", "mark_symbol_fetch_failed", "get_fetch_failed_symbols",
    # crawl + sentiment
    "save_crawl_results", "get_crawl_results", "save_sentiment", "get_sentiment",
    # volume task queue
    "init_volume_analysis_tasks", "sync_volume_analysis_tasks", "get_next_volume_task",
    "mark_volume_tasks_for_rescore", "get_next_volume_tasks", "set_volume_task_priority",
    "update_volume_task", "set_volume_task_cooldown",
    # volume results
    "save_volume_result", "get_volume_result", "get_all_volume_scores",
    "get_all_volume_sentiments", "get_volume_results", "get_all_volume_results",
    "get_data_ages", "get_volume_task_counts",
    # recommendations / screeners / insights
    "save_recommendations", "get_recommendations",
    "save_screener_result", "get_screener_result", "get_all_screener_results",
    "save_extended_insight", "get_extended_insight", "get_all_extended_insights",
    # settings / rag
    "get_setting", "set_setting",
    "get_rag_sources", "save_rag_source", "delete_rag_source", "save_all_rag_sources",
    # daily trades
    "get_daily_session", "get_daily_positions_for_date", "save_daily_session",
    "confirm_daily_session", "get_open_daily_positions", "update_daily_position_prices",
    "flag_daily_position_sell", "close_daily_position", "get_daily_history",
    "get_daily_portfolio_stats",
]


def test_public_surface_present_and_callable(db):
    missing = [name for name in PUBLIC_FUNCTIONS if not callable(getattr(db, name, None))]
    assert not missing, f"missing/non-callable db functions: {missing}"


def test_infrastructure_surface_present(db):
    for attr in ("AppSettings", "DB_PATH", "_get_conn", "_init_schema", "_migrate",
                 "_bootstrap_avg_volumes", "_write_lock", "_local"):
        assert hasattr(db, attr), f"missing infra attr: {attr}"
    assert db.AppSettings.MODEL_PATH == "model_path"


# ── Domain round-trips ──────────────────────────────────────────────────────────

def test_watchlist_roundtrip(db):
    seeded = db.get_watchlist()           # seeds defaults on first read
    assert "AAPL" in seeded
    assert db.add_to_watchlist("tsla") == db.get_watchlist()
    assert "TSLA" in db.get_watchlist()
    db.remove_from_watchlist("TSLA")
    assert "TSLA" not in db.get_watchlist()


def test_picks_roundtrip_and_stats(db):
    pick = db.add_pick("aapl", 100.0, "buy", "cheap", target_price=120.0, signal_score=80)
    assert pick["symbol"] == "AAPL" and pick["status"] == "open"
    assert any(p["id"] == pick["id"] for p in db.get_picks())
    assert any(p["id"] == pick["id"] for p in db.get_open_picks())

    db.update_pick_prices({"AAPL": 110.0})
    assert db.get_open_picks()[0]["current_price"] == 110.0

    closed = db.close_pick(pick["id"], 130.0, "target_hit")
    assert closed["status"] == "closed" and closed["was_correct"] is True
    assert closed["final_return"] == 30.0

    stats = db.get_pick_stats()
    assert stats["closed"] == 1 and stats["wins"] == 1 and stats["win_rate"] == 100.0


def test_symbols_roundtrip(db):
    db.save_symbols([{"symbol": "AAPL", "name": "Apple Inc."},
                     {"symbol": "MSFT", "name": "Microsoft Corp."}])
    assert db.get_symbol_count() == 2
    assert {s["symbol"] for s in db.get_symbols()} == {"AAPL", "MSFT"}
    assert db.get_symbol_names(["aapl"]) == {"AAPL": "Apple Inc."}

    db.save_symbol_avg_volumes({"AAPL": 1_000_000.0})
    db.mark_symbol_fetch_failed(["MSFT"])
    assert "MSFT" in db.get_fetch_failed_symbols(within_days=7)
    # crawl 'nyse_symbols' is backed by the symbols table
    assert db.get_crawl_results("nyse_symbols")["count"] == 2


def test_crawl_and_sentiment_roundtrip(db):
    db.save_crawl_results("reddit", {"items": [{"title": "hello"}]})
    got = db.get_crawl_results("reddit")
    assert got["items"][0]["title"] == "hello" and "fetched_at" in got
    # a list payload is wrapped under "items"
    db.save_crawl_results("zacks", [{"title": "x"}])
    assert db.get_crawl_results("zacks")["items"] == [{"title": "x"}]

    db.save_sentiment("aapl", 72.5, "bullish")
    assert db.get_sentiment("AAPL")["score"] == 72.5
    assert db.get_sentiment()["AAPL"]["summary"] == "bullish"


def test_settings_roundtrip(db):
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("model_path", "/models/x.gguf")
    assert db.get_setting("model_path") == "/models/x.gguf"
    db.set_setting("cfg", {"enabled": True, "n": 3})
    assert db.get_setting("cfg") == {"enabled": True, "n": 3}


def test_volume_results_roundtrip(db):
    db.save_symbols([{"symbol": "NVDA", "name": "NVIDIA"}])
    db.save_volume_result("nvda", {
        "score": 88, "grade": "A", "recommendation": "BUY",
        "macro": {"score": 70}, "sentiment": {"score": 65},
        "short_term": {"score": 80}, "long_term": {"score": 90},
        "sector": "Technology", "industry": "Semiconductors", "avg_volume": 5_000_000,
    })
    assert db.get_volume_result("NVDA")["score"] == 88
    assert db.get_all_volume_scores()["NVDA"]["grade"] == "A"
    assert db.get_all_volume_sentiments()["NVDA"] == 65.0
    results, total = db.get_volume_results(limit=10)
    assert total == 1 and results[0]["symbol"] == "NVDA"
    assert db.get_all_volume_results()[0]["overall_score"] == 88
    assert db.get_data_ages()["volume_last_updated"] is not None


def test_recommendations_screeners_insights_roundtrip(db):
    db.save_recommendations({"recommendations": [{"symbol": "AAPL"}], "total_evaluated": 5})
    assert db.get_recommendations()["data"]["total_evaluated"] == 5

    db.save_screener_result("undervalued", {"results": [{"symbol": "T"}],
                                            "universe_size": 100, "shortlist_size": 10})
    assert db.get_screener_result("undervalued")["data"]["results"][0]["symbol"] == "T"
    assert "undervalued" in db.get_all_screener_results()

    db.save_extended_insight("aapl", "long analysis", model_name="m1")
    assert db.get_extended_insight("AAPL")["insight_text"] == "long analysis"
    assert db.get_all_extended_insights()[0]["symbol"] == "AAPL"


def test_rag_sources_roundtrip(db):
    db.save_rag_source("s1", {"url": "http://a"})
    assert db.get_rag_sources()["s1"]["url"] == "http://a"
    db.delete_rag_source("s1")
    assert "s1" not in db.get_rag_sources()
    db.save_all_rag_sources({"a": {"x": 1}, "b": {"y": 2}})
    assert set(db.get_rag_sources()) == {"a", "b"}


def test_daily_trades_roundtrip(db):
    positions = [
        {"symbol": "AAPL", "aggregate_score": 90, "allocation_usd": 6.0, "source": "overall"},
        {"symbol": "MSFT", "aggregate_score": 80, "allocation_usd": 4.0, "source": "screener"},
    ]
    db.save_daily_session("2026-06-03", positions)
    session = db.get_daily_session("2026-06-03")
    assert session["status"] == "recommended"
    rows = db.get_daily_positions_for_date("2026-06-03")
    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}

    db.confirm_daily_session("2026-06-03", {"AAPL": 100.0, "MSFT": 200.0})
    assert db.get_daily_session("2026-06-03")["status"] == "confirmed"
    purchased = [r for r in db.get_daily_positions_for_date("2026-06-03") if r["status"] == "purchased"]
    assert len(purchased) == 2

    assert isinstance(db.get_daily_history(limit=5), list)
    assert isinstance(db.get_daily_portfolio_stats(), dict)
