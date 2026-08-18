"""
Unit tests for the backtest-driven daily-trade improvements: pre-entry gates
(volatility ceiling, liquidity floor, score sanity), repeat-appearance decay,
equal-weight sizing with a position cap, the hard stop-loss, and the scorer's
volatility / dollar-volume metrics.
"""
import pandas as pd
import pytest

import daily_trades_router as dt
import signals


# ── Pre-entry gates ─────────────────────────────────────────────────────────────

def _cand(**kw):
    base = {"symbol": "TEST", "aggregate_score": 80.0, "volatility": 0.40, "dollar_volume": 10_000_000}
    base.update(kw)
    return base


def test_entry_gate_passes_clean_candidate():
    assert dt._entry_rejection(_cand()) is None


def test_entry_gate_volatility_ceiling():
    assert dt._entry_rejection(_cand(volatility=0.56)) is not None   # above the 55% ceiling
    assert dt._entry_rejection(_cand(volatility=0.55)) is None       # exactly at ceiling passes
    assert "volatility" in dt._entry_rejection(_cand(volatility=0.90))


def test_entry_gate_liquidity_floor():
    assert dt._entry_rejection(_cand(dollar_volume=4_999_999)) is not None
    assert dt._entry_rejection(_cand(dollar_volume=5_000_000)) is None
    assert "liquidity" in dt._entry_rejection(_cand(dollar_volume=2_500))   # the LTAFF case


def test_entry_gate_score_sanity_rejects_out_of_range():
    assert "declared range" in dt._entry_rejection(_cand(aggregate_score=240))   # the 240 bug
    assert "declared range" in dt._entry_rejection(_cand(aggregate_score=-5))
    assert "declared range" in dt._entry_rejection(_cand(aggregate_score=None))
    assert dt._entry_rejection(_cand(aggregate_score=100)) is None
    assert dt._entry_rejection(_cand(aggregate_score=0)) is None


def test_entry_gate_fails_closed_on_missing_risk_metrics():
    # Can't confirm a name is safe without its metrics → skip it, don't trade blind.
    assert dt._entry_rejection(_cand(volatility=None)) is not None
    assert dt._entry_rejection(_cand(dollar_volume=None)) is not None


# ── Repeat-appearance decay ──────────────────────────────────────────────────────

def test_appearance_counts_dedupes_within_session():
    sessions = [
        {"positions": [{"symbol": "AAA"}, {"symbol": "aaa"}, {"symbol": "BBB"}]},
        {"positions": [{"symbol": "AAA"}]},
    ]
    counts = dt._appearance_counts(sessions)
    assert counts["AAA"] == 2   # both sessions (deduped within the first)
    assert counts["BBB"] == 1


def test_decay_factor_fades_repeats():
    assert dt._decay_factor(0) == 1.0
    assert dt._decay_factor(1) == pytest.approx(dt._APPEARANCE_DECAY)
    assert dt._decay_factor(3) == pytest.approx(dt._APPEARANCE_DECAY ** 3)
    assert dt._decay_factor(4) < dt._decay_factor(1)   # more appearances → more fade


# ── Equal-weight sizing + position cap ───────────────────────────────────────────

def test_allocate_equal_weight_under_cap():
    picks = [{"symbol": f"S{i}"} for i in range(10)]
    dt._allocate(picks)
    assert all(p["allocation_usd"] == round(dt._DAILY_BUDGET / 10, 4) for p in picks)
    assert [p["rank"] for p in picks] == list(range(1, 11))


def test_allocate_position_cap_binds_when_few_picks():
    picks = [{"symbol": f"S{i}"} for i in range(5)]   # equal = 20% > 15% cap
    dt._allocate(picks)
    cap = round(dt._DAILY_BUDGET * dt._MAX_POSITION_PCT, 4)
    assert all(p["allocation_usd"] == cap for p in picks)


def test_allocate_empty_is_safe():
    dt._allocate([])   # must not raise


# ── Hard stop-loss ───────────────────────────────────────────────────────────────

def test_stop_loss_flags_at_threshold():
    positions = [
        {"id": "a", "symbol": "AAA", "pnl_pct": -25.0},
        {"id": "b", "symbol": "BBB", "pnl_pct": -20.0},   # exactly at the stop
        {"id": "c", "symbol": "CCC", "pnl_pct": -19.9},   # just above → keep
        {"id": "d", "symbol": "DDD", "pnl_pct": 5.0},
        {"id": "e", "symbol": "EEE", "pnl_pct": None},
    ]
    flagged = dt._stop_loss_flags(positions)
    assert set(flagged) == {"a", "b"}
    assert "Stop loss" in flagged["a"]["suggested_sell_reason"]


# ── Scorer risk metrics ──────────────────────────────────────────────────────────

def test_annualized_volatility_zero_for_flat_prices():
    assert signals.annualized_volatility(pd.DataFrame({"close": [100.0] * 80})) == 0.0


def test_annualized_volatility_none_for_short_history():
    assert signals.annualized_volatility(pd.DataFrame({"close": [100, 101, 102]})) is None


def test_annualized_volatility_high_for_choppy_prices():
    closes = [100.0]
    for i in range(80):
        closes.append(closes[-1] * (1.02 if i % 2 == 0 else 1 / 1.02))
    vol = signals.annualized_volatility(pd.DataFrame({"close": closes}))
    assert vol is not None and vol > 0.20


def test_annualized_volatility_winsorizes_bad_ticks():
    # A garbage tick (100 → 5 → 100) would blow up an un-winsorized estimate;
    # clipped daily returns keep it bounded (still high, not astronomical).
    closes = [100.0] * 40 + [5.0] + [100.0] * 39
    vol = signals.annualized_volatility(pd.DataFrame({"close": closes}))
    assert vol is not None and 0.5 < vol < 5.0


def test_median_dollar_volume_from_ohlcv():
    df = pd.DataFrame({"close": [10.0] * 80, "volume": [1_000_000] * 80})
    assert signals.median_dollar_volume(df) == 10_000_000.0


def test_median_dollar_volume_falls_back_to_info():
    empty = pd.DataFrame({"close": [], "volume": []})
    dv = signals.median_dollar_volume(empty, info={"price": 20.0, "avg_volume": 500_000})
    assert dv == 10_000_000.0


def test_median_dollar_volume_none_without_data():
    assert signals.median_dollar_volume(pd.DataFrame({"close": [], "volume": []}), info={}) is None
