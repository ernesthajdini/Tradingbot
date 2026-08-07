"""
Backtest harness invariants: journal isolation, manifest enforcement,
sealed-period guard, injected dates (no wall-clock leaks), run logging.
Runs on synthetic EOD chains — the real-data pilot comes later; these tests
prove the MACHINERY, not the strategy.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from csp_screener import config, journal
from csp_screener.backtest import data_loader, engine
from csp_screener.backtest.engine import BacktestParams, ManifestViolation


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Redirect run log + results to tmp; journals to tmp (to PROVE they
    stay empty); Supabase off."""
    monkeypatch.setattr(engine, "RUNS_LOG", tmp_path / "runs_log.jsonl")
    monkeypatch.setattr(engine, "RESULTS_DIR", tmp_path / "results")
    files = {k: tmp_path / f"{k}.jsonl" for k in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", files)
    import csp_screener.supabase_sync as sync
    monkeypatch.setattr(sync, "is_enabled", lambda: False)
    return tmp_path


def synthetic_market(
    start: date, n_days: int, tickers=("AAA", "BBB", "CCC"),
    spot0=12.0, daily_drift=0.0, fixture_iv=0.55,
):
    """
    EOD chains + price frames for n_days business days, shaped like real
    data: a FIXED expiration cycle (every 21 days) and a FIXED strike grid,
    quoted every day — so a position opened on day d still finds its
    contract's quotes on day d+k (that is what marking requires). Puts are
    priced with the production Black-Scholes helper at constant IV: tight
    two-sided, arbitrage-consistent, and they decay through time so exits
    actually fire.
    """
    from csp_screener.virtual_tracker import black_scholes_put_price

    days = [d.date() for d in pd.bdate_range(start=start, periods=n_days)]
    expiries = [days[0] + timedelta(days=k) for k in range(21, n_days + 85, 21)]
    strikes = [round(s, 1) for s in np.arange(6.0, 20.5, 0.5)]
    rows = []
    prices = {}
    rng = np.random.default_rng(42)
    for ticker in tickers:
        spots = spot0 * np.exp(np.cumsum(
            rng.normal(daily_drift, 0.02, len(days))))
        # 300 days of warmup history so RV percentile has a base. One
        # continuous business-day index ending on the last sim day keeps
        # warmup and sim perfectly aligned.
        full_idx = pd.bdate_range(end=pd.Timestamp(days[-1]),
                                  periods=300 + len(days))
        warm = spot0 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))[::-1]
        prices[ticker] = pd.DataFrame({
            "Close": np.concatenate([warm, spots]),
            "Volume": np.full(300 + len(days), 3_000_000.0),
        }, index=full_idx)
        for di, d in enumerate(days):
            spot = float(spots[di])
            for exp in expiries:
                dte = (exp - d).days
                if dte <= 0 or dte > 70:
                    continue
                for strike in strikes:
                    if not (0.6 * spot <= strike <= 1.05 * spot):
                        continue
                    mid = black_scholes_put_price(spot, strike, dte, fixture_iv)
                    if mid < 0.05:
                        continue
                    rows.append({
                        "quote_date": d, "ticker": ticker, "expiration": exp,
                        "strike": strike,
                        "bid": round(mid * 0.99, 4), "ask": round(mid * 1.01, 4),
                        "last": round(mid, 4), "volume": 500,
                        "open_interest": 1500, "iv": fixture_iv,
                        "delta": np.nan,  # loader estimates with the as-of date
                        "underlying_price": spot,
                    })
    frame = pd.DataFrame(rows)
    return frame, prices


class TestManifestEnforcement:
    def test_undeclared_dte_window_rejected(self):
        with pytest.raises(ManifestViolation):
            BacktestParams(dte_min=7, dte_max=14).validate()

    def test_undeclared_delta_rejected(self):
        with pytest.raises(ManifestViolation):
            BacktestParams(target_delta=0.45).validate()

    def test_declared_grid_accepted(self):
        BacktestParams(dte_min=30, dte_max=45, target_delta=0.20).validate()
        BacktestParams().validate()  # production values are in the grid


class TestIsolationAndLogging:
    def test_run_writes_nothing_to_any_journal(self, isolated):
        frame, prices = synthetic_market(date(2023, 1, 2), 40)
        result = engine.run(frame, prices,
                            source_meta={"includes_delisted": True})
        assert result["summary"]["closed_trades"] >= 1, \
            "fixture must actually exercise open+close paths"
        for topic, path in journal.JOURNAL_FILES.items():
            assert not path.exists(), f"backtest leaked into {topic}"

    def test_every_run_logged_started_and_completed(self, isolated):
        frame, prices = synthetic_market(date(2023, 1, 2), 10)
        engine.run(frame, prices, source_meta={"includes_delisted": True})
        lines = [json.loads(l) for l in
                 (isolated / "runs_log.jsonl").read_text().splitlines()]
        assert [l["status"] for l in lines] == ["started", "completed"]
        assert lines[0]["frozen_config_hash"]

    def test_survivorship_stamped_when_unknown(self, isolated):
        frame, prices = synthetic_market(date(2023, 1, 2), 10)
        result = engine.run(frame, prices, source_meta={})
        assert result["summary"]["survivorship"] == "biased"
        assert result["summary"]["earnings_gate"] == "unavailable"


class TestSealedPeriod:
    def test_sealed_dates_skipped_by_default(self, isolated):
        frame, prices = synthetic_market(date(2024, 12, 16), 20)
        result = engine.run(frame, prices,
                            source_meta={"includes_delisted": True})
        traded_dates = {t["closed_on"] for t in result["trades"]}
        curve_dates = {p["date"] for p in result["equity_curve"]}
        assert all(d < "2025-01-01" for d in traded_dates | curve_dates)

    def test_sealed_unlock_is_logged(self, isolated):
        frame, prices = synthetic_market(date(2024, 12, 16), 20)
        engine.run(frame, prices, allow_sealed=True,
                   source_meta={"includes_delisted": True})
        lines = [json.loads(l) for l in
                 (isolated / "runs_log.jsonl").read_text().splitlines()]
        assert lines[0]["sealed_unlocked"] is True


class TestReplayCorrectness:
    def test_dte_derives_from_asof_not_wall_clock(self, isolated):
        """A 2023 replay must produce 2023-relative DTEs — the injected-date
        fix. If any generator read datetime.now(), dte_at_open would be
        negative (expirations are years in the past)."""
        frame, prices = synthetic_market(date(2023, 1, 2), 40)
        result = engine.run(frame, prices,
                            source_meta={"includes_delisted": True})
        assert result["trades"], "fixture produced no trades"
        for t in result["trades"]:
            opened = date.fromisoformat(t["opened_on"])
            closed = date.fromisoformat(t["closed_on"])
            assert opened >= date(2023, 1, 2)
            assert closed >= opened
        curve = result["equity_curve"]
        assert curve[0]["date"].startswith("2023")

    def test_marks_prefer_market_source(self, isolated):
        """Synthetic chains quote every (date, strike) two-sided and tight,
        so closes should overwhelmingly mark from the market."""
        frame, prices = synthetic_market(date(2023, 1, 2), 40)
        result = engine.run(frame, prices,
                            source_meta={"includes_delisted": True})
        share = result["summary"]["market_marked_share"]
        assert share is not None and share > 0.8

    def test_book_rules_hold(self, isolated):
        frame, prices = synthetic_market(date(2023, 1, 2), 40)
        result = engine.run(frame, prices,
                            source_meta={"includes_delisted": True})
        for p in result["equity_curve"]:
            assert p["open"] <= config.MAX_VIRTUAL_OPEN

    def test_vix_kill_blocks_entries(self, isolated):
        frame, prices = synthetic_market(date(2023, 1, 2), 15)
        result = engine.run(
            frame, prices, vix_lookup=lambda d: 60.0,
            source_meta={"includes_delisted": True})
        assert result["summary"]["closed_trades"] == 0
        assert all(p.get("vix_killed") for p in result["equity_curve"])


class TestPositionMarkParity:
    """_position_mark must mirror options_data.fetch_position_quote EXACTLY.
    Adversarial review (executed proof): width-gating the LONG wing — which
    production deliberately does not do, because cheap wings always look
    wide in % terms — silently model-marked live-tier replay closes."""

    def _frame(self, wing_bid, wing_ask):
        return pd.DataFrame([
            {"quote_date": date(2023, 3, 1), "ticker": "XX",
             "expiration": date(2023, 4, 6), "strike": 27.0,
             "bid": 1.00, "ask": 1.05, "last": 1.02, "volume": 10,
             "open_interest": 900, "iv": 0.5, "delta": -0.3,
             "underlying_price": 29.0},
            {"quote_date": date(2023, 3, 1), "ticker": "XX",
             "expiration": date(2023, 4, 6), "strike": 25.0,
             "bid": wing_bid, "ask": wing_ask,
             "last": (wing_bid + wing_ask) / 2, "volume": 10,
             "open_interest": 900, "iv": 0.5, "delta": -0.2,
             "underlying_price": 29.0},
        ])

    def _pos(self):
        from csp_screener.virtual_tracker import OpenVirtualTrade
        return OpenVirtualTrade(
            trade_id="t", screen_id="s",
            opened_at=datetime(2023, 2, 20),
            ticker="XX", spot_at_open=29.0,
            expiration=datetime(2023, 4, 6), dte_at_open=45,
            strike=27.0, credit_received=70.0, max_loss=130.0,
            breakeven=26.3, iv_at_open=0.5,
            structure="put_credit_spread", long_strike=25.0, tier="live")

    def test_wide_wing_still_market_marks(self):
        """$0.05-wide wing on a $0.355 mid = 14% of mid — admitted at entry
        by production's absolute gate, must not void the mark."""
        frame = self._frame(0.33, 0.38)
        mark = engine._position_mark(frame, date(2023, 3, 1), self._pos())
        assert mark == pytest.approx((1.025 - 0.355) * 100.0)

    def test_wide_short_leg_still_voids(self):
        frame = self._frame(0.33, 0.38)
        frame.loc[frame["strike"] == 27.0, ["bid", "ask"]] = [0.80, 1.25]
        mark = engine._position_mark(frame, date(2023, 3, 1), self._pos())
        assert mark is None

    def test_one_sided_wing_voids(self):
        frame = self._frame(0.0, 0.38)
        mark = engine._position_mark(frame, date(2023, 3, 1), self._pos())
        assert mark is None

    def test_inverted_spread_never_clamps_to_zero(self):
        frame = self._frame(1.50, 1.55)  # wing worth more than short leg
        mark = engine._position_mark(frame, date(2023, 3, 1), self._pos())
        assert mark is None


class TestDataEndedSettlement:
    def test_position_closes_when_price_data_stops(self, isolated, monkeypatch):
        """A name whose rows stop mid-hold must close as data_ended, not
        coast on a frozen spot to a mild forced exit. Threshold tightened to
        1 day so the silence outruns the normal 21-DTE exit in the fixture."""
        monkeypatch.setattr(engine, "DATA_ENDED_AFTER_DAYS", 1)
        frame, prices = synthetic_market(date(2023, 1, 2), 40,
                                         tickers=("AAA",))
        cutoff = pd.Timestamp(date(2023, 1, 20))
        prices["AAA"] = prices["AAA"][prices["AAA"].index <= cutoff]
        frame = frame[frame["quote_date"] <= cutoff.date()
                      ].reset_index(drop=True)
        # Keep walking dates past the cutoff so the silence is observed
        extra_days = [d.date() for d in
                      pd.bdate_range(start=date(2023, 1, 23), periods=15)]
        pad = frame.iloc[:1].copy()
        rows = []
        for d in extra_days:
            r = pad.copy()
            r["quote_date"] = d
            r["bid"] = 0.0
            r["ask"] = 0.0
            rows.append(r)
        frame = pd.concat([frame] + rows, ignore_index=True)
        result = engine.run(frame, prices,
                            source_meta={"includes_delisted": True})
        assert result["summary"]["data_ended_closes"] >= 1
        assert any(t["exit_reason"] == "data_ended" for t in result["trades"])


class TestLoader:
    def test_universe_asof_uses_only_history(self):
        idx = pd.DatetimeIndex(pd.bdate_range(start=date(2023, 1, 2), periods=60))
        px = pd.DataFrame({
            "Close": np.linspace(10, 200, 60),  # leaves the band later
            "Volume": np.full(60, 2_000_000.0),
        }, index=idx)
        early = idx[2].date()  # Close ≈ 16.4, inside the 5-25 band
        got = data_loader.universe_asof(
            {"XX": px}, early, config.PRICE_MIN, config.PRICE_MAX,
            config.MIN_DAILY_VOLUME)
        assert got == ["XX"], "as-of filter must see the EARLY price, not the last"
        late = idx[-1].date()
        assert data_loader.universe_asof(
            {"XX": px}, late, config.PRICE_MIN, config.PRICE_MAX,
            config.MIN_DAILY_VOLUME) == []

    def test_universe_asof_rejects_non_datetime_index(self):
        px = pd.DataFrame({"Close": [10.0, 11.0],
                           "Volume": [2e6, 2e6]})  # RangeIndex
        with pytest.raises(ValueError, match="non-datetime index"):
            data_loader.universe_asof(
                {"XX": px}, date(2023, 1, 5), 5, 25, 1_000_000)

    def test_chains_for_date_respects_dte_window(self):
        frame, _ = synthetic_market(date(2023, 1, 2), 3)
        asof = date(2023, 1, 2)
        chains = data_loader.chains_for_date(frame, asof, 25, 45)
        for chain in chains.values():
            for c in chain.puts:
                dte = (c.expiration.date() - asof).days
                assert 25 <= dte <= 45
