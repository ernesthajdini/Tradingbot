"""
Zombie-row defense + rule-enforcement guards.

Fixtures are pinned to the LIVE-verified 2026-07-18 LCID chain from the
review's market audit: fresh rows (traded Jul-17) interleaved with zombie
rows whose two-sided quotes froze in Jun-Aug 2025 (pre-reverse-split
prices). The 3.5P zombie (mid 1.755) is the exact row that produced the
$175.50 fake credit.
"""
from datetime import datetime, timedelta

import pytest

from csp_screener.options_data import (
    MAX_ACCEPTED_IV,
    OptionContract,
    OptionsChain,
    _filter_zombie_puts,
)
from csp_screener.setup_generator import MAX_CSP_DELTA, generate_setup

NOW = datetime(2026, 7, 18, 12, 0)
EXP = datetime(2026, 8, 21)
FRESH = NOW - timedelta(days=1)
ZOMBIE = NOW - timedelta(days=330)


def _put(strike, bid, ask, last, ltd, iv=1.0, delta=None, oi=800):
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
    return OptionContract(
        ticker="LCID", expiration=EXP, strike=strike, right="P",
        bid=bid, ask=ask, last=last, mid=mid,
        open_interest=oi, volume=10, iv=iv, delta=delta,
        source="yfinance", last_trade_date=ltd,
    )


def _lcid_chain_rows():
    """The live-verified interleaved chain."""
    return [
        _put(2.0, 0.05, 0.06, 0.055, FRESH),                # fresh
        _put(2.5, 0.93, 0.98, 0.955, ZOMBIE),               # zombie
        _put(3.0, 0.18, 0.19, 0.185, FRESH),                # fresh
        _put(3.5, 1.73, 1.78, 1.755, ZOMBIE, iv=4.85),      # THE zombie
        _put(4.0, 0.30, 0.31, 0.305, FRESH),                # fresh
        _put(4.5, 0.0, 0.0, 2.04, ZOMBIE),                  # zombie via last-fallback
        _put(5.0, 0.50, 0.51, 0.505, FRESH),                # fresh
    ]


def test_zombie_rows_dropped_fresh_rows_kept():
    kept, dropped = _filter_zombie_puts(_lcid_chain_rows(), now=NOW)
    kept_strikes = sorted(c.strike for c in kept)
    assert kept_strikes == [2.0, 3.0, 4.0, 5.0]
    assert len(dropped) == 3
    assert all("zombie row" in d for d in dropped)


def test_monotonicity_kills_undated_impossible_rows():
    # A row with NO last_trade_date survives staleness but prices 5x above a
    # fresh higher strike — arbitrage-impossible, must drop.
    rows = [
        _put(3.5, 1.73, 1.78, 1.755, None),   # undated zombie
        _put(4.0, 0.30, 0.31, 0.305, FRESH),  # fresh higher strike
    ]
    kept, dropped = _filter_zombie_puts(rows, now=NOW)
    assert [c.strike for c in kept] == [4.0]
    assert "arbitrage-impossible" in dropped[0]


def test_penny_violations_are_kept():
    # Live WEN/RIVN chains show harmless $0.01-0.07 inversions — materiality
    # threshold must keep them.
    rows = [
        _put(5.5, 0.10, 0.12, 0.11, FRESH),
        _put(6.0, 0.08, 0.10, 0.09, FRESH),  # 0.11 > 0.09 but tiny
    ]
    kept, _ = _filter_zombie_puts(rows, now=NOW)
    assert len(kept) == 2


def test_weekend_staleness_is_kept():
    rows = [_put(6.0, 0.0, 0.0, 0.10, NOW - timedelta(days=3))]
    kept, dropped = _filter_zombie_puts(rows, now=NOW)
    assert len(kept) == 1 and not dropped


def test_missing_last_trade_date_is_kept():
    rows = [_put(6.0, 0.09, 0.11, 0.10, None)]
    kept, _ = _filter_zombie_puts(rows, now=NOW)
    assert len(kept) == 1


def test_extreme_iv_nulls_greeks_but_keeps_row():
    rows = [_put(3.0, 0.18, 0.19, 0.185, FRESH, iv=4.85, delta=-0.12)]
    kept, dropped = _filter_zombie_puts(rows, now=NOW)
    assert len(kept) == 1
    assert kept[0].iv is None and kept[0].delta is None
    assert any("IV" in d for d in dropped)
    assert MAX_ACCEPTED_IV == 3.0


# ---------------------------------------------------------------------------
# ITM/delta rule enforcement in _pick_best_put (the BB incident)
# ---------------------------------------------------------------------------

def _chain(spot, puts):
    return OptionsChain(ticker="BB", spot=spot, expirations=[EXP], puts=puts,
                        source="yfinance")


def _bb_put(strike, mid, delta, oi=900):
    # 2% bid/ask spread — inside the MAX_BID_ASK_PCT_OF_MID liquidity gate
    return OptionContract(
        ticker="BB", expiration=EXP, strike=strike, right="P",
        bid=round(mid * 0.99, 4), ask=round(mid * 1.01, 4), last=mid, mid=mid,
        open_interest=oi, volume=50, iv=0.9, delta=delta,
        source="yfinance", last_trade_date=FRESH,
    )


def test_itm_put_never_selected():
    # BB Jul-8: spot 11.10, the 12-strike (ITM, delta -0.53) was chosen.
    # With a legit OTM row present, the OTM strike must win.
    chain = _chain(11.10, [
        _bb_put(12.0, 1.96, -0.534),   # the rule-breaker
        _bb_put(9.0, 0.35, -0.28),     # legit OTM
    ])
    setup = generate_setup("BB", 11.10, chain)
    assert setup is not None
    assert setup.strike == 9.0


def test_no_trade_when_only_itm_or_high_delta_rows():
    chain = _chain(11.10, [
        _bb_put(12.0, 1.96, -0.534),
        _bb_put(11.5, 1.40, -0.48),
        _bb_put(11.0, 1.10, -0.45),   # OTM but above MAX_CSP_DELTA
    ])
    assert MAX_CSP_DELTA == 0.40
    assert generate_setup("BB", 11.10, chain) is None


# ---------------------------------------------------------------------------
# IV clamp in the daily mark (churn mechanism severed)
# ---------------------------------------------------------------------------

def test_garbage_open_iv_cannot_mint_day_one_take_profit():
    from csp_screener.virtual_tracker import OpenVirtualTrade, evaluate_open_position

    trade = OpenVirtualTrade(
        trade_id="t::LCID::3.5::2026-08-21", screen_id="s",
        opened_at=NOW, ticker="LCID", spot_at_open=5.99,
        expiration=EXP, dte_at_open=34, strike=3.5,
        credit_received=20.0, max_loss=330.0, breakeven=3.3,
        iv_at_open=4.85,  # garbage IV from the zombie row
    )
    result = evaluate_open_position(trade, current_spot=5.99, current_iv=1.03,
                                    today=NOW + timedelta(days=1))
    # With the clamp, one calm day cannot capture 50% of the credit
    assert result["pnl_pct_of_credit"] < 0.50

    # And a normal IV is untouched by the clamp (min(0.9, 2.5) == 0.9)
    trade_normal = OpenVirtualTrade(
        trade_id="t::WEN::6.0::2026-08-21", screen_id="s",
        opened_at=NOW, ticker="WEN", spot_at_open=8.6,
        expiration=EXP, dte_at_open=34, strike=6.0,
        credit_received=4.0, max_loss=596.0, breakeven=5.96,
        iv_at_open=0.9,
    )
    r = evaluate_open_position(trade_normal, current_spot=8.6, current_iv=0.9,
                               today=NOW + timedelta(days=1))
    assert r["pnl_pct_of_credit"] < 0.50


# ---------------------------------------------------------------------------
# Quarantine coverage guards
# ---------------------------------------------------------------------------

def test_streamlit_dashboard_has_no_ungated_reconstruct():
    # app.py can't be imported under pytest (st.set_page_config at module
    # load), so pin at source level: the quarantine-bypassing duplicate
    # helper must stay deleted.
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
           ).read_text(encoding="utf-8")
    assert "def _reconstruct_closed_trades" not in src
    assert "evaluator._reconstruct_closed_trades()" in src


def test_typescript_sanity_constants_match_python():
    # The dashboard mirrors sanity.py by hand — pin the constants in lockstep.
    from pathlib import Path
    from csp_screener import sanity
    ts = (Path(__file__).resolve().parent.parent.parent / "dashboard-web" /
          "lib" / "queries.ts").read_text(encoding="utf-8")
    assert f"MAX_CSP_CREDIT_FRAC_OF_STRIKE = {sanity.MAX_CSP_CREDIT_FRAC_OF_STRIKE}" in ts
    assert f"MAX_DEEP_OTM_CREDIT_FRAC = {sanity.MAX_DEEP_OTM_CREDIT_FRAC}" in ts
    assert f"DEEP_OTM_DELTA = {sanity.DEEP_OTM_DELTA}" in ts
