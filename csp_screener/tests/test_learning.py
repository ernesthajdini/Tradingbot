"""Tests for the learning layer (ticker scorer, analyzer, recommender, confidence)."""
from datetime import datetime, timedelta

import pytest

from csp_screener.learning import (
    confidence, feature_analyzer, recommender, ticker_scorer,
)


# ---------------------------------------------------------------------------
# ticker_scorer
# ---------------------------------------------------------------------------

def _trade(ticker: str, pnl: float, days_ago: int = 1) -> dict:
    closed = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {"ticker": ticker, "pnl": pnl, "closed_at": closed}


def test_ticker_with_no_history_gets_neutral_score():
    s = ticker_scorer.score_ticker("FOO", [])
    assert s.score == 1.0
    assert s.sample_quality == "thin"


def test_ticker_below_cooldown_keeps_neutral_score():
    # Even with a 100% win rate, 2 trades isn't enough to move the score
    trades = [_trade("FOO", 10), _trade("FOO", 5)]
    s = ticker_scorer.score_ticker("FOO", trades)
    assert s.score == 1.0
    assert s.sample_quality == "thin"


def test_chronic_loser_gets_score_below_1():
    # All losses, well past cooldown
    trades = [_trade("BAD", -10, days_ago=i) for i in range(10)]
    s = ticker_scorer.score_ticker("BAD", trades)
    assert s.score < 1.0
    assert s.score >= ticker_scorer.MIN_SCORE
    assert s.sample_quality == "robust"


def test_chronic_winner_gets_score_above_1():
    trades = [_trade("GOOD", 5, days_ago=i) for i in range(10)]
    s = ticker_scorer.score_ticker("GOOD", trades)
    assert s.score > 1.0
    assert s.score <= ticker_scorer.MAX_SCORE


def test_lookback_window_excludes_old_trades():
    # Trades older than LOOKBACK_DAYS should not affect score
    old = [_trade("X", -100, days_ago=300) for _ in range(20)]
    s = ticker_scorer.score_ticker("X", old)
    # All trades excluded -> score back to neutral
    assert s.score == 1.0
    assert s.trades == 0


def test_score_all_tickers_returns_one_per_ticker():
    trades = [_trade("A", 1), _trade("B", 2), _trade("A", -1)]
    scores = ticker_scorer.score_all_tickers(trades)
    assert set(scores.keys()) == {"A", "B"}


# ---------------------------------------------------------------------------
# feature_analyzer
# ---------------------------------------------------------------------------

def test_analyzer_returns_empty_for_no_trades():
    assert feature_analyzer.analyze_features([]) == []


def test_analyzer_buckets_by_delta():
    trades = [
        {"ticker": "A", "pnl": 10, "delta_at_open": -0.20},
        {"ticker": "B", "pnl": -10, "delta_at_open": -0.20},
        {"ticker": "C", "pnl": 10, "delta_at_open": -0.40},
    ]
    out = feature_analyzer.analyze_features(trades)
    delta_buckets = [b for b in out if b.feature == "delta"]
    assert len(delta_buckets) >= 1
    # The 0.15-0.25 bucket should have 2 trades, 1 win
    b = next(b for b in delta_buckets if b.bucket == "0.15-0.25")
    assert b.trades == 2
    assert b.wins == 1


def test_overall_baseline_with_data():
    trades = [{"pnl": 10}, {"pnl": -5}, {"pnl": 8}]
    b = feature_analyzer.overall_baseline(trades)
    assert b["trades"] == 3
    assert b["win_rate"] == pytest.approx(2/3, abs=0.01)


# ---------------------------------------------------------------------------
# recommender
# ---------------------------------------------------------------------------

def test_recommender_no_data_returns_info_only():
    recs = recommender.all_recommendations({"trades": 0}, {}, [])
    assert len(recs) == 1
    assert recs[0].severity == "info"
    assert "No closed trades" in recs[0].title


def test_recommender_flags_chronic_loser():
    # Build a ticker with consistent losses, past threshold
    bad_trades = [_trade("LOSER", -10, days_ago=i) for i in range(8)]
    ticker_stats = ticker_scorer.score_all_tickers(bad_trades)
    recs = recommender.recommend_from_tickers(ticker_stats)
    losers = [r for r in recs if "chronically losing" in r.title]
    assert len(losers) == 1
    assert losers[0].severity == "warn"


def test_recommender_flags_chronic_winner():
    good_trades = [_trade("WINNER", 10, days_ago=i) for i in range(8)]
    ticker_stats = ticker_scorer.score_all_tickers(good_trades)
    recs = recommender.recommend_from_tickers(ticker_stats)
    winners = [r for r in recs if "consistently winning" in r.title]
    assert len(winners) == 1
    assert winners[0].severity == "info"


def test_recommender_respects_min_trades_for_buckets():
    # Single trade in a bucket shouldn't generate a recommendation
    trades = [{"ticker": "A", "pnl": -100, "delta_at_open": -0.40}]
    buckets = feature_analyzer.analyze_features(trades)
    recs = recommender.recommend_from_buckets(buckets, {"win_rate": 0.5})
    assert len(recs) == 0  # only 1 trade in bucket — too thin


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------

def test_confidence_neutral_with_no_history():
    ts = {}
    c = confidence.compute_confidence(
        ticker="FOO", rv_percentile=80.0,
        data_quality="ibkr_greeks", ticker_stats=ts, current_vix=18.0,
    )
    # Neutral ticker × ibkr quality × normal VIX = ~1.0
    assert c.composite == pytest.approx(1.0, abs=0.05)


def test_confidence_penalizes_weak_data_quality():
    ts = {}
    c_good = confidence.compute_confidence("X", 80, "ibkr_greeks", ts, 18)
    c_weak = confidence.compute_confidence("X", 80, "premium_only_no_greeks", ts, 18)
    assert c_weak.composite < c_good.composite


def test_confidence_boosts_in_elevated_vix():
    ts = {}
    c_low = confidence.compute_confidence("X", 80, "ibkr_greeks", ts, 14)
    c_high = confidence.compute_confidence("X", 80, "ibkr_greeks", ts, 24)
    assert c_high.composite > c_low.composite


def test_confidence_punishes_extreme_vix():
    ts = {}
    c_normal = confidence.compute_confidence("X", 80, "ibkr_greeks", ts, 18)
    c_extreme = confidence.compute_confidence("X", 80, "ibkr_greeks", ts, 40)
    assert c_extreme.composite < c_normal.composite


def test_confidence_uses_ticker_score():
    # Build a winning ticker
    good = [_trade("WIN", 10, days_ago=i) for i in range(10)]
    ts = ticker_scorer.score_all_tickers(good)
    c_winner = confidence.compute_confidence("WIN", 80, "ibkr_greeks", ts, 18)
    c_neutral = confidence.compute_confidence("UNKNOWN", 80, "ibkr_greeks", ts, 18)
    assert c_winner.composite > c_neutral.composite
