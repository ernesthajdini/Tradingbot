"""Tests for the learning layer (ticker scorer, analyzer, recommender, confidence)."""
from datetime import datetime, timedelta

import pytest

from csp_screener.learning import (
    confidence, feature_analyzer, recommender, ticker_scorer,
)


# ---------------------------------------------------------------------------
# ticker_scorer
# ---------------------------------------------------------------------------

def _trade(ticker: str, pnl: float, days_ago: int = 1, **extra) -> dict:
    closed = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {"ticker": ticker, "pnl": pnl, "closed_at": closed, **extra}


def _credit_trade(ticker: str, pnl_pct: float, days_ago: int = 1, credit: float = 50.0) -> dict:
    """Trade with credit data — exercises the expectancy-primary scoring path."""
    return _trade(
        ticker, pnl_pct * credit, days_ago=days_ago,
        credit_received=credit, pnl_pct_of_credit=pnl_pct,
    )


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


def test_high_win_rate_but_negative_ev_scores_below_neutral():
    # THE key property of expectancy-primary scoring: 8 take-profit wins
    # (+0.45 credit each) and 2 stop-loss blow-ups (-2.0 credit each) is an
    # 80% win rate but negative EV — the score must land BELOW 1.0.
    trades = (
        [_credit_trade("TRAP", +0.45, days_ago=i) for i in range(8)]
        + [_credit_trade("TRAP", -2.0, days_ago=10 + i) for i in range(2)]
    )
    s = ticker_scorer.score_ticker("TRAP", trades)
    assert s.raw_win_rate == pytest.approx(0.8)
    assert s.avg_pnl_pct < 0
    assert s.score < 1.0


def test_positive_ev_with_credit_data_scores_above_neutral():
    trades = [_credit_trade("EARN", +0.45, days_ago=i) for i in range(10)]
    s = ticker_scorer.score_ticker("EARN", trades)
    assert s.score > 1.0
    assert s.score <= ticker_scorer.MAX_SCORE
    assert s.shrunk_expectancy > 0


def test_chronic_credit_loser_hits_floor():
    trades = [_credit_trade("BLOWUP", -2.0, days_ago=i) for i in range(10)]
    s = ticker_scorer.score_ticker("BLOWUP", trades)
    assert s.score == ticker_scorer.MIN_SCORE


def test_mildly_negative_ev_never_scores_above_neutral():
    # Adversarial-review finding: with an unconditional win-rate bonus, a
    # ticker at 8 wins / 2 losses summing to slightly negative EV scored
    # ABOVE 1.0. Win rate must never lift a negative-expectancy ticker.
    trades = (
        [_credit_trade("SNEAK", +0.45, days_ago=i) for i in range(8)]
        + [_credit_trade("SNEAK", -1.85, days_ago=10 + i) for i in range(2)]
    )
    s = ticker_scorer.score_ticker("SNEAK", trades)
    assert s.avg_pnl_pct < 0
    assert s.score < 1.0


def test_small_samples_cannot_saturate_the_clamps():
    # Adversarial-review finding: EXPECTANCY_SCALE=3.0 pinned the score to a
    # clamp at exactly COOLDOWN_TRADES=3 normal outcomes, voiding the
    # shrinkage guarantee. 3 ordinary wins/losses must move the score only
    # modestly. (Blow-up losses are exempt — fast penalty is deliberate.)
    wins3 = [_credit_trade("W3", +0.45, days_ago=i) for i in range(3)]
    s = ticker_scorer.score_ticker("W3", wins3)
    assert 1.0 < s.score < ticker_scorer.MAX_SCORE

    losses3 = [_credit_trade("L3", -0.5, days_ago=i) for i in range(3)]
    s = ticker_scorer.score_ticker("L3", losses3)
    assert ticker_scorer.MIN_SCORE < s.score < 1.0


def test_sustained_winner_reaches_max():
    trades = [_credit_trade("SUST", +0.45, days_ago=i % 90) for i in range(10)]
    s = ticker_scorer.score_ticker("SUST", trades)
    assert s.score == ticker_scorer.MAX_SCORE


def test_expectancy_falls_back_to_pnl_over_credit():
    # No pnl_pct_of_credit field, but credit_received present
    trades = [
        _trade("FB", -100.0, days_ago=i, credit_received=50.0) for i in range(5)
    ]
    s = ticker_scorer.score_ticker("FB", trades)
    assert s.avg_pnl_pct == pytest.approx(-2.0)
    assert s.score < 1.0


# ---------------------------------------------------------------------------
# feature_analyzer
# ---------------------------------------------------------------------------


def test_analyzer_buckets_rv_and_vix_from_stamped_fields():
    # The at-open stamping must feed the RV-percentile and VIX dimensions
    trades = [
        {"ticker": "A", "pnl": 10, "rv_percentile_at_open": 92.0, "vix_at_open": 16.5},
        {"ticker": "B", "pnl": -5, "rv_percentile_at_open": 30.0, "vix_at_open": 26.0},
    ]
    out = feature_analyzer.analyze_features(trades)
    features = {b.feature for b in out}
    assert "rv_percentile" in features
    assert "vix" in features
    rv_buckets = {b.bucket for b in out if b.feature == "rv_percentile"}
    assert "75-101" in rv_buckets and "25-50" in rv_buckets

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


def test_wilson_gate_suppresses_marginal_deviation():
    # 6 wins / 8 trades looks like a 75% winner but the 95% CI includes 0.5 —
    # this must NOT produce a recommendation (statistical theater guard).
    trades = (
        [_trade("MEH", 10, days_ago=i) for i in range(6)]
        + [_trade("MEH", -10, days_ago=6 + i) for i in range(2)]
    )
    ticker_stats = ticker_scorer.score_all_tickers(trades)
    recs = recommender.recommend_from_tickers(ticker_stats)
    assert recs == []


def test_wilson_gate_passes_decisive_records():
    # 24 wins / 30 trades: CI comfortably excludes 0.5 — must fire.
    trades = (
        [_trade("SOLID", 10, days_ago=i % 90) for i in range(24)]
        + [_trade("SOLID", -10, days_ago=(i % 90) + 1) for i in range(6)]
    )
    ticker_stats = ticker_scorer.score_all_tickers(trades)
    recs = recommender.recommend_from_tickers(ticker_stats)
    assert len(recs) == 1
    assert "consistently winning" in recs[0].title
    assert "wilson_ci" in recs[0].supporting_data


def test_wilson_interval_sanity():
    lo, hi = recommender._wilson_interval(0, 8)
    assert hi < 0.5           # 0-for-8 is decisively bad
    lo, hi = recommender._wilson_interval(6, 8)
    assert lo < 0.5 < hi      # 6-of-8 is not decisive
    assert recommender._wilson_interval(0, 0) == (0.0, 1.0)


# ---------------------------------------------------------------------------
# band flips (base vs pessimistic friction)
# ---------------------------------------------------------------------------

def test_band_flip_flags_ticker_whose_wins_evaporate():
    trades = [
        _trade("THIN", +3.0, days_ago=i, pnl_pessimistic=-1.0) for i in range(6)
    ]
    recs = recommender.recommend_band_flips(trades)
    ticker_recs = [r for r in recs if r.category == "friction_band"
                   and r.supporting_data.get("ticker") == "THIN"]
    assert len(ticker_recs) == 1
    assert ticker_recs[0].severity == "warn"


def test_band_flip_flags_overall_record():
    trades = [
        _trade("A", +5.0, days_ago=i, pnl_pessimistic=-0.5) for i in range(3)
    ] + [
        _trade("B", +5.0, days_ago=i, pnl_pessimistic=-0.5) for i in range(3)
    ]
    recs = recommender.recommend_band_flips(trades)
    overall = [r for r in recs if "optimistic fills" in r.title]
    assert len(overall) == 1
    assert overall[0].severity == "alert"


def test_no_winning_rec_for_high_winrate_negative_ev():
    # Adversarial-review finding: the "consistently winning / ranker already
    # boosts this" rec fired on win rate alone, even when the dollars were
    # negative and the scorer was actually PENALIZING the name.
    trades = (
        [_credit_trade("FAKEWIN", +0.45, days_ago=i % 90) for i in range(24)]
        + [_credit_trade("FAKEWIN", -2.5, days_ago=(i % 90) + 1) for i in range(6)]
    )
    stats = ticker_scorer.score_all_tickers(trades)
    assert stats["FAKEWIN"].avg_pnl < 0  # scenario precondition
    recs = recommender.recommend_from_tickers(stats)
    assert all("consistently winning" not in r.title for r in recs)


def test_band_flip_needs_banded_trades_per_ticker():
    # Adversarial-review finding: 4 legacy (unbanded) trades + 1 banded trade
    # fired a per-ticker warn claiming "5 trades" of evidence. Only banded
    # trades count toward the per-ticker gate.
    filler = [_trade("OTHER", +1.0, days_ago=i, pnl_pessimistic=+0.5)
              for i in range(6)]  # passes the global banded gate
    legacy = [_trade("X", +10.0, days_ago=i) for i in range(4)]
    one_banded = [_trade("X", +5.0, days_ago=5, pnl_pessimistic=-46.0)]
    recs = recommender.recommend_band_flips(filler + legacy + one_banded)
    assert all(r.supporting_data.get("ticker") != "X" for r in recs)


def test_band_flip_silent_when_bands_agree():
    trades = [
        _trade("OK", +5.0, days_ago=i, pnl_pessimistic=+2.0) for i in range(6)
    ]
    assert recommender.recommend_band_flips(trades) == []


def test_band_flip_silent_without_pessimistic_data():
    # Legacy journals without pnl_pessimistic can't create flips
    trades = [_trade("OLD", +5.0, days_ago=i) for i in range(10)]
    assert recommender.recommend_band_flips(trades) == []


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
