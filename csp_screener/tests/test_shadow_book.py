"""
Shadow book invariants. The one that matters most: shadow rows can NEVER
reach virtual_trades — golive.gate_status() pools every close there, so one
leaked shadow corrupts the 200-trade counter and the market-marked share.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from csp_screener import config, golive, journal, shadow_book
from csp_screener.setup_generator import VirtualSetup


@pytest.fixture
def temp_journals(tmp_path, monkeypatch):
    """Point every journal topic at a temp dir; disable Supabase."""
    files = {k: tmp_path / f"{k}.jsonl" for k in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", files)
    import csp_screener.supabase_sync as sync
    monkeypatch.setattr(sync, "is_enabled", lambda: False)
    return files


def _setup(ticker="XYZ", strike=9.0, credit=45.0, dte=35, tier="sandbox"):
    exp = (datetime.now() + timedelta(days=dte)).date().isoformat()
    return VirtualSetup(
        ticker=ticker, spot_at_screen=10.0, expiration=exp, dte=dte,
        strike=strike, pct_otm=0.10, delta=-0.30, iv=0.60,
        bid=0.40, ask=0.50, mid=0.45, bid_ask_pct=0.05,
        open_interest=800, volume=100,
        estimated_credit_per_contract=credit,
        max_loss_per_contract=strike * 100 - credit,
        breakeven=strike - credit / 100, data_quality="test",
        reasoning=[], structure="csp", tier=tier,
    )


class TestSeparation:
    def test_shadow_open_never_touches_virtual_trades(self, temp_journals):
        shadow_book.open_shadow_position(_setup(), "scr_1", "rank_cutoff", 7)
        assert not temp_journals["virtual_trades"].exists()
        assert temp_journals["shadow_trades"].exists()

    def test_shadow_close_never_touches_virtual_trades(self, temp_journals):
        shadow_book.open_shadow_position(_setup(), "scr_1", "rank_cutoff", 7)
        trade = shadow_book.get_open_shadow_trades()[0]
        shadow_book.close_shadow_position(
            trade, "take_profit_50pct", 10.5, 20.0)
        assert not temp_journals["virtual_trades"].exists()

    def test_golive_gate_blind_to_shadow_closes(self, temp_journals):
        """200 shadow closes must leave the gate at 0/200."""
        for i in range(3):
            shadow_book.open_shadow_position(
                _setup(ticker=f"T{i}", strike=9.0 + i), "scr_1", "rank_cutoff", i)
        for trade in shadow_book.get_open_shadow_trades():
            shadow_book.close_shadow_position(
                trade, "take_profit_50pct", 10.5, 20.0)
        status = golive.gate_status()
        assert status["closed_trades"] == 0

    def test_shadow_trade_id_carries_shadow_segment(self, temp_journals):
        tid = shadow_book.open_shadow_position(_setup(), "scr_1", "rank_cutoff", 7)
        assert "::shadow::" in tid


class TestEconomics:
    def test_close_economics_match_production(self, temp_journals):
        """Same trade, same numbers — shared close_economics by construction."""
        from csp_screener import virtual_tracker
        shadow_book.open_shadow_position(_setup(credit=45.0), "scr_1", "rank_cutoff", 7)
        trade = shadow_book.get_open_shadow_trades()[0]
        rec = shadow_book.close_shadow_position(
            trade, "take_profit_50pct", 10.5, 20.0)
        econ = virtual_tracker.close_economics(45.0, 20.0, "csp")
        assert rec["pnl"] == round(econ["pnl"], 2)
        assert rec["pnl_pessimistic"] == round(econ["pnl_pessimistic"], 2)

    def test_close_carries_mark_source_notes_format(self, temp_journals):
        shadow_book.open_shadow_position(_setup(), "scr_1", "rank_cutoff", 7)
        trade = shadow_book.get_open_shadow_trades()[0]
        shadow_book.close_shadow_position(
            trade, "expired", 10.0, 0.0, notes="mark_source=model, model_price=0.00")
        closes = [r for r in journal.read_all("shadow_trades")
                  if r["event"] == "close"]
        assert "mark_source=" in closes[0]["notes"]


class TestBookRules:
    def test_one_open_per_ticker_and_cooldown(self, temp_journals, monkeypatch):
        """record_run_shadows enforces the same book rules as production."""
        import csp_screener.options_data as od
        chain_calls = []
        monkeypatch.setattr(od, "fetch_chain",
                            lambda t, p, ibkr_client=None: chain_calls.append(t))
        # An open shadow on ABC blocks a new ABC shadow (fetch never happens)
        shadow_book.open_shadow_position(_setup(ticker="ABC"), "scr_0",
                                         "rank_cutoff", 6)
        summary = shadow_book.record_run_shadows(
            "scr_1",
            discarded=[{"ticker": "ABC", "last_price": 10.0, "rank_full": 6,
                        "tier": "sandbox"}],
            earnings_blocked=[],
            production_open_tickers=set(),
            production_cooldown_tickers=set(),
        )
        assert summary["opened"] == []
        assert summary["skipped"] == 1
        assert chain_calls == []

    def test_production_tickers_never_shadowed(self, temp_journals, monkeypatch):
        import csp_screener.options_data as od
        monkeypatch.setattr(od, "fetch_chain",
                            lambda t, p, ibkr_client=None: pytest.fail(
                                "fetched a chain for a production ticker"))
        summary = shadow_book.record_run_shadows(
            "scr_1",
            discarded=[{"ticker": "TAKEN", "last_price": 10.0, "rank_full": 6,
                        "tier": "sandbox"}],
            earnings_blocked=[],
            production_open_tickers={"TAKEN"},
            production_cooldown_tickers=set(),
        )
        assert summary["opened"] == []

    def test_per_run_caps_are_logged_not_silent(self, temp_journals, monkeypatch):
        import csp_screener.options_data as od
        monkeypatch.setattr(od, "fetch_chain", lambda t, p, ibkr_client=None: None)
        many = [{"ticker": f"T{i}", "last_price": 10.0, "rank_full": 6 + i,
                 "tier": "sandbox"}
                for i in range(shadow_book.MAX_NEW_RANK_SHADOWS_PER_RUN + 5)]
        summary = shadow_book.record_run_shadows(
            "scr_1", discarded=many, earnings_blocked=[],
            production_open_tickers=set(), production_cooldown_tickers=set())
        assert summary["dropped_by_cap"] == 5


class TestQuoteBudget:
    def test_budget_caps_fresh_fetches(self, temp_journals, monkeypatch):
        """N shadows, budget k < N ⇒ exactly k fresh quote fetches."""
        n, budget = 6, 2
        for i in range(n):
            shadow_book.open_shadow_position(
                _setup(ticker=f"T{i}", strike=9.0, dte=35), f"scr_{i}",
                "rank_cutoff", 6)
        fetches = []
        import csp_screener.shadow_book as sb
        monkeypatch.setattr(sb, "market_quote_resolver",
                            lambda trade: fetches.append(trade.ticker) or None)
        import csp_screener.options_data as od
        monkeypatch.setattr(od, "_position_quote_cache", {})
        summary = sb.mark_open_shadows(
            lambda t: 10.0, lambda t: 0.60,
            market_open=True, quote_budget=budget)
        assert summary["quotes_spent"] == budget
        assert len(fetches) == budget
        assert summary["updated"] == n

    def test_cache_rides_are_free(self, temp_journals, monkeypatch):
        shadow_book.open_shadow_position(_setup(ticker="CACHED"), "scr_1",
                                         "rank_cutoff", 6)
        trade = shadow_book.get_open_shadow_trades()[0]
        import csp_screener.options_data as od
        key = (trade.ticker, trade.expiration.date().isoformat())
        monkeypatch.setattr(od, "_position_quote_cache", {key: {}})
        import csp_screener.shadow_book as sb
        monkeypatch.setattr(sb, "market_quote_resolver", lambda t: None)
        summary = sb.mark_open_shadows(
            lambda t: 10.0, lambda t: 0.60, market_open=True, quote_budget=0)
        assert summary["quotes_spent"] == 0
        assert summary["updated"] == 1

    def test_market_closed_spends_nothing(self, temp_journals, monkeypatch):
        shadow_book.open_shadow_position(_setup(), "scr_1", "rank_cutoff", 6)
        import csp_screener.shadow_book as sb
        monkeypatch.setattr(sb, "market_quote_resolver",
                            lambda t: pytest.fail("fetched after hours"))
        summary = sb.mark_open_shadows(
            lambda t: 10.0, lambda t: 0.60, market_open=False)
        assert summary["quotes_spent"] == 0


class TestEarningsAutopsy:
    def test_earnings_cohort_generates_with_gate_off(self, temp_journals, monkeypatch):
        """The counterfactual must HOLD THROUGH earnings: the generator is
        called with next_earnings_days=None (gate off) and the true distance
        is stamped on the open record."""
        import csp_screener.options_data as od
        import csp_screener.setup_generator as sg
        seen = {}

        def fake_generate(ticker, price, chain, next_earnings_days="MISSING",
                          **kw):
            seen["ned"] = next_earnings_days
            return _setup(ticker=ticker)

        monkeypatch.setattr(od, "fetch_chain", lambda t, p, ibkr_client=None: object())
        monkeypatch.setattr(sg, "generate_setup", fake_generate)
        summary = shadow_book.record_run_shadows(
            "scr_1", discarded=[],
            earnings_blocked=[{"ticker": "XYZ", "last_price": 10.0,
                               "next_earnings_days": 9, "tier": "sandbox"}],
            production_open_tickers=set(), production_cooldown_tickers=set())
        assert seen["ned"] is None
        assert len(summary["opened"]) == 1
        opens = [r for r in journal.read_all("shadow_trades")
                 if r["event"] == "open"]
        assert opens[0]["shadow_reason"] == "earnings_blackout"
        assert opens[0]["next_earnings_days_at_open"] == 9

    def test_cohort_summary_separates_reasons(self, temp_journals):
        shadow_book.open_shadow_position(
            _setup(ticker="RANK"), "scr_1", "rank_cutoff", 6)
        shadow_book.open_shadow_position(
            _setup(ticker="EARN", strike=8.0), "scr_1", "earnings_blackout",
            next_earnings_days=9)
        for trade in shadow_book.get_open_shadow_trades():
            shadow_book.close_shadow_position(
                trade, "take_profit_50pct", 10.5, 20.0,
                notes="mark_source=market, model_price=21.00")
        report = shadow_book.cohort_summary()
        assert report["shadow_rank_cutoff"]["closed"] == 1
        assert report["shadow_earnings_blackout"]["closed"] == 1
        assert report["shadow_rank_cutoff"]["market_marked_share"] == 1.0


class TestReviewFixes:
    """Regressions for the adversarial-review findings on this build."""

    def test_earnings_cohort_draws_budget_first(self, temp_journals, monkeypatch):
        """With a chain budget of 1, the earnings candidate must win the
        slot — the scarcer experiment can't be starved by the rank cohort."""
        import csp_screener.options_data as od
        import csp_screener.setup_generator as sg
        opened_for = []
        monkeypatch.setattr(od, "fetch_chain",
                            lambda t, p, ibkr_client=None: object())

        def fake_generate(ticker, price, chain, next_earnings_days=None, **kw):
            opened_for.append(ticker)
            return _setup(ticker=ticker, dte=40)

        monkeypatch.setattr(sg, "generate_setup", fake_generate)
        monkeypatch.setattr(shadow_book, "SHADOW_OPEN_CHAIN_BUDGET", 1)
        summary = shadow_book.record_run_shadows(
            "scr_1",
            discarded=[{"ticker": "RANK1", "last_price": 10.0, "rank_full": 6,
                        "tier": "sandbox"}],
            earnings_blocked=[{"ticker": "EARN1", "last_price": 10.0,
                               "next_earnings_days": 9, "tier": "sandbox"}],
            production_open_tickers=set(), production_cooldown_tickers=set())
        assert opened_for == ["EARN1"]
        assert summary["capped"] == 1  # the rank candidate hit the budget

    def test_earnings_shadow_must_span_the_report(self, temp_journals, monkeypatch):
        """A counterfactual that closes BEFORE earnings teaches nothing —
        earnings in 25d with a 40-DTE setup (19d hold) must not open."""
        import csp_screener.options_data as od
        import csp_screener.setup_generator as sg
        monkeypatch.setattr(od, "fetch_chain",
                            lambda t, p, ibkr_client=None: object())
        monkeypatch.setattr(
            sg, "generate_setup",
            lambda ticker, price, chain, **kw: _setup(ticker=ticker, dte=40))
        summary = shadow_book.record_run_shadows(
            "scr_1", discarded=[],
            earnings_blocked=[{"ticker": "LATE", "last_price": 10.0,
                               "next_earnings_days": 25, "tier": "sandbox"}],
            production_open_tickers=set(), production_cooldown_tickers=set())
        assert summary["opened"] == []
        assert summary["not_spanning"] == 1

    def test_inert_when_supabase_up_but_table_missing(self, temp_journals,
                                                      monkeypatch):
        """Supabase configured + shadow table absent ⇒ skip opens entirely
        (no fetch burn on data that cannot persist on stateless runners)."""
        import csp_screener.supabase_sync as sync
        import csp_screener.options_data as od

        class _BrokenClient:
            def table(self, name):
                raise RuntimeError("PGRST205: table not found")

        monkeypatch.setattr(sync, "is_enabled", lambda: True)
        monkeypatch.setattr(sync, "_get_client", lambda: _BrokenClient())
        monkeypatch.setattr(od, "fetch_chain",
                            lambda t, p, ibkr_client=None: pytest.fail(
                                "fetched a chain while inert"))
        summary = shadow_book.record_run_shadows(
            "scr_1",
            discarded=[{"ticker": "AAA", "last_price": 10.0, "rank_full": 6,
                        "tier": "sandbox"}],
            earnings_blocked=[],
            production_open_tickers=set(), production_cooldown_tickers=set())
        assert summary.get("inert") is True
        assert summary["opened"] == []

    def test_live_shadow_respects_ex_div_blackout(self, temp_journals,
                                                  monkeypatch):
        """Production live candidates skip names with ex-div inside DTE_MAX;
        the shadow cohort must play by the same rules."""
        import csp_screener.options_data as od
        import csp_screener.earnings as earn_mod
        monkeypatch.setattr(
            earn_mod, "fetch_ex_dividend",
            lambda t: datetime.now() + timedelta(days=10))
        monkeypatch.setattr(od, "fetch_chain",
                            lambda t, p, ibkr_client=None: pytest.fail(
                                "fetched a chain for an ex-div-blocked name"))
        summary = shadow_book.record_run_shadows(
            "scr_1",
            discarded=[{"ticker": "DIV", "last_price": 30.0, "rank_full": 6,
                        "tier": "live"}],
            earnings_blocked=[],
            production_open_tickers=set(), production_cooldown_tickers=set())
        assert summary["opened"] == []
        assert summary["skipped"] == 1

    def test_cohort_summary_same_window(self, temp_journals):
        """Production closes from BEFORE the first shadow open must not
        pollute the taken cohort."""
        from csp_screener import virtual_tracker
        old_setup = _setup(ticker="OLD")
        tid = virtual_tracker.open_virtual_position(old_setup, "scr_old")
        virtual_tracker.close_virtual_position(
            trade_id=tid,
            exit_reason="expired", exit_spot=10.0, final_put_price=0.0,
            credit_received=45.0, ticker="OLD", strike=old_setup.strike,
            expiration=old_setup.expiration)
        import time
        time.sleep(0.01)
        shadow_book.open_shadow_position(_setup(ticker="SH"), "scr_1",
                                         "rank_cutoff", 6)
        report = shadow_book.cohort_summary()
        assert report["taken"]["closed"] == 0
        assert report["taken_window_start"] is not None


class TestFilterAndRankOutParams:
    def test_earnings_rejects_and_shadow_pool_collected(self, temp_journals):
        import pandas as pd
        import numpy as np
        from csp_screener.filters import TickerContext
        from csp_screener.main import step_filter_and_rank

        rng = np.random.default_rng(7)
        n = 300

        def history(seed_scale):
            prices = 10.0 * np.exp(np.cumsum(
                rng.normal(0, 0.01 * seed_scale, n)))
            idx = pd.bdate_range(end=datetime.now().date(), periods=n)
            return pd.Series(prices, index=idx)

        now = datetime.now()
        contexts = [
            TickerContext(ticker="EARNBLOCK", last_price=10.0,
                          avg_volume_20d=2_000_000,
                          next_earnings=now + timedelta(days=5),
                          price_history=history(1.0)),
        ] + [
            TickerContext(ticker=f"OK{i}", last_price=10.0,
                          avg_volume_20d=2_000_000,
                          next_earnings=None, price_history=history(1.0 + i * 0.3))
            for i in range(config.MAX_CANDIDATES_IN_EMAIL + 3)
        ]
        pool: list = []
        rejects: list = []
        ranked = step_filter_and_rank(
            contexts, now, shadow_pool=pool, earnings_rejects=rejects)
        assert [r["ticker"] for r in rejects] == ["EARNBLOCK"]
        assert rejects[0]["next_earnings_days"] == 5
        assert len(ranked) == config.MAX_CANDIDATES_IN_EMAIL
        assert len(pool) == 3
        top = {r.ticker for r in ranked}
        assert all(p["ticker"] not in top for p in pool)
        assert all(p["rank_full"] > config.MAX_CANDIDATES_IN_EMAIL for p in pool)
