"""
Walk-forward engine: replays the production screen day by day over
historical EOD chains, using the REAL gate code — filters, ranker,
setup generators (with injected as-of dates), sanity caps, friction model,
exit rules — and an in-memory book.

ISOLATION (MANIFEST rail): this module never imports csp_screener.journal
or supabase_sync. It cannot write to the production journals, the shadow
book, Supabase, or the go-live gate. Results go to plain files under
backtest/results/.

MULTIPLICITY (MANIFEST rail): every run() call appends a 'started' line to
runs_log.jsonl before touching data, and a 'completed' line after. The
count of 'started' lines is the honesty denominator — crashed runs count.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from csp_screener import config, data_pipeline, filters, ranker, setup_generator
from csp_screener.filters import TickerContext
from csp_screener.virtual_tracker import (
    OpenVirtualTrade, close_economics, evaluate_open_position,
)
from csp_screener.backtest import data_loader

logger = logging.getLogger(__name__)

BACKTEST_DIR = Path(__file__).resolve().parent
RUNS_LOG = BACKTEST_DIR / "runs_log.jsonl"
RESULTS_DIR = BACKTEST_DIR / "results"

# MANIFEST section 2: sealed test period. The unlock is a visible, logged act.
SEALED_START = date(2025, 1, 1)

# Mirrors main.REOPEN_COOLDOWN_DAYS (imported value would drag main's module
# initialization into the replay path; the frozen-config hash pins drift).
REOPEN_COOLDOWN_DAYS = 3

# A held name whose price data simply STOPS (delisting/halt) must not coast
# on a frozen spot to a mild forced exit — that censors the left tail the
# survivorship rail exists for. After this many days of silence the position
# is closed and counted separately (data_ended_closes) so readers see it.
DATA_ENDED_AFTER_DAYS = 7

# Vol-input hygiene for MODEL marks (backtest-local; production untouched).
# options_data.MAX_ACCEPTED_IV already declares 3.0 the point past which an
# implied vol is garbage rather than data. Realized vol computed from
# as-traded prices can blow past it on a corporate action the split file
# doesn't cover (AABA's liquidating distribution: RV 374%; EDU pre-fix:
# 657%), and the blended sigma then prices a put at many times any quote —
# which is how the first run manufactured stop-losses. Capping the vol INPUT
# cannot hide a real loss: realised P&L comes from quotes, not from sigma.
MAX_MODEL_VOL = 3.0

# MANIFEST section 1 + AMENDMENT 1: the declared search space.
ALLOWED_DTE_WINDOWS = {(25, 45), (30, 45)}
ALLOWED_TARGET_DELTAS = {0.15, 0.20, 0.25, 0.30}
ALLOWED_EXIT_DTE = {21, 7, 0}          # 0 = hold to expiry
ALLOWED_STOP_MULT = {2.0, 3.0, None}   # None = no stop
ALLOWED_UNIVERSES = {"single_name", "index_etf"}
# AMENDMENT 2: (spread width, max risk per spread) account-scale levels,
# read off the measured median risk per width / the playbook's 5% rule.
ALLOWED_SCALES = {(2.0, 130.0), (5.0, 400.0), (10.0, 800.0)}


class ManifestViolation(Exception):
    """Raised when a run asks for a parameter outside MANIFEST.md."""


@dataclass(frozen=True)
class BacktestParams:
    dte_min: int = config.DTE_MIN
    dte_max: int = config.DTE_MAX
    target_delta: float = config.TARGET_DELTA
    tier: str = "sandbox"          # 'sandbox' (CSP) | 'live' (spread)
    label: str = "production"
    # AMENDMENT 1 knobs. Defaults reproduce production exactly, so a
    # default-constructed run is byte-identical to Phase 1.
    exit_dte: int = config.VIRTUAL_FORCE_EXIT_DTE
    stop_mult: Optional[float] = config.VIRTUAL_SL_MULTIPLE
    universe: str = "single_name"
    scale: tuple = (config.SPREAD_WIDTH_WIDE, config.MAX_RISK_PER_SPREAD)

    def validate(self) -> None:
        if (self.dte_min, self.dte_max) not in ALLOWED_DTE_WINDOWS:
            raise ManifestViolation(
                f"DTE window {self.dte_min}-{self.dte_max} is not in the "
                f"declared grid {sorted(ALLOWED_DTE_WINDOWS)} — see MANIFEST.md")
        if self.target_delta not in ALLOWED_TARGET_DELTAS:
            raise ManifestViolation(
                f"target_delta {self.target_delta} is not in the declared "
                f"grid {sorted(ALLOWED_TARGET_DELTAS)} — see MANIFEST.md")
        if self.tier not in ("sandbox", "live"):
            raise ManifestViolation(f"unknown tier {self.tier!r}")
        if self.exit_dte not in ALLOWED_EXIT_DTE:
            raise ManifestViolation(
                f"exit_dte {self.exit_dte} outside the declared space "
                f"{sorted(ALLOWED_EXIT_DTE)} — see MANIFEST Amendment 1")
        if self.stop_mult not in ALLOWED_STOP_MULT:
            raise ManifestViolation(
                f"stop_mult {self.stop_mult} outside the declared space "
                f"— see MANIFEST Amendment 1")
        if self.universe not in ALLOWED_UNIVERSES:
            raise ManifestViolation(f"unknown universe {self.universe!r}")
        if tuple(self.scale) not in ALLOWED_SCALES:
            raise ManifestViolation(
                f"scale {self.scale} outside the declared space "
                f"{sorted(ALLOWED_SCALES)} — see MANIFEST Amendment 2")


def _frozen_config_hash() -> str:
    """Hash of every production value the manifest declares FROZEN, so a
    drifted config can't silently produce incomparable runs."""
    frozen = {
        "VIRTUAL_TP_PCT": config.VIRTUAL_TP_PCT,
        "VIRTUAL_FORCE_EXIT_DTE": config.VIRTUAL_FORCE_EXIT_DTE,
        "VIRTUAL_SL_MULTIPLE": config.VIRTUAL_SL_MULTIPLE,
        "COMMISSION_PER_CONTRACT": config.COMMISSION_PER_CONTRACT,
        "SLIPPAGE_PCT_OF_PREMIUM": config.SLIPPAGE_PCT_OF_PREMIUM,
        "SLIPPAGE_PCT_PESSIMISTIC": config.SLIPPAGE_PCT_PESSIMISTIC,
        "MIN_OPEN_INTEREST": config.MIN_OPEN_INTEREST,
        "MAX_BID_ASK_PCT_OF_MID": config.MAX_BID_ASK_PCT_OF_MID,
        "MIN_NET_CREDIT_AFTER_FRICTION": config.MIN_NET_CREDIT_AFTER_FRICTION,
        "MAX_FRICTION_PCT_OF_CREDIT": config.MAX_FRICTION_PCT_OF_CREDIT,
        "MAX_RISK_PER_SPREAD": config.MAX_RISK_PER_SPREAD,
        "SPREAD_WIDTH_NARROW": config.SPREAD_WIDTH_NARROW,
        "SPREAD_WIDTH_WIDE": config.SPREAD_WIDTH_WIDE,
        "VIX_KILL_SWITCH": config.VIX_KILL_SWITCH,
        "MIN_DAILY_VOLUME": config.MIN_DAILY_VOLUME,
        "MAX_VIRTUAL_OPEN": config.MAX_VIRTUAL_OPEN,
        "MAX_CANDIDATES_IN_EMAIL": config.MAX_CANDIDATES_IN_EMAIL,
        "EARNINGS_EXCLUSION_DAYS": config.EARNINGS_EXCLUSION_DAYS,
        "PRICE_MIN": config.PRICE_MIN, "PRICE_MAX": config.PRICE_MAX,
        "LIVE_PRICE_MIN": config.LIVE_PRICE_MIN,
        "LIVE_PRICE_MAX": config.LIVE_PRICE_MAX,
        "MAX_CSP_DELTA": setup_generator.MAX_CSP_DELTA,
        # Ranker constants (the manifest freezes the ranker too) + the book
        # cooldown this engine hard-codes to mirror main.REOPEN_COOLDOWN_DAYS.
        "RV_WINDOW_DAYS": config.RV_WINDOW_DAYS,
        "RV_HISTORY_DAYS": config.RV_HISTORY_DAYS,
        "REOPEN_COOLDOWN_DAYS": REOPEN_COOLDOWN_DAYS,
    }
    blob = json.dumps(frozen, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _append_run_log(record: dict) -> None:
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def _position_mark(
    frame: pd.DataFrame, asof: date, pos: OpenVirtualTrade,
) -> Optional[float]:
    """
    Historical market mark for an open position, mirroring
    options_data.fetch_position_quote's honesty gates EXACTLY: the SHORT leg
    needs a live two-sided quote within 2x the entry width gate; the LONG
    wing needs only a two-sided quote — cheap wings always look wide in %
    terms (the entry gate's own rationale), and width-gating them here would
    model-mark most live-tier closes the production path market-marks
    (adversarial review, executed proof: a $0.05-wide wing on a $0.35 mid is
    14% of mid — admitted at entry, must not void the mark).
    Inverted spread -> None (never $0).
    """
    day = data_loader.day_rows(frame, asof)

    def leg_mid(strike: float, require_tight: bool = True) -> Optional[float]:
        rows = day[
            (day["ticker"] == pos.ticker)
            & (day["expiration"] == pos.expiration.date())
            & (day["strike"].round(4) == round(strike, 4))
        ]
        if rows.empty:
            return None
        r = rows.iloc[0]
        bid, ask = float(r["bid"]), float(r["ask"])
        if not (bid > 0 and ask > 0):
            return None
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        if require_tight and (ask - bid) / mid > config.MAX_BID_ASK_PCT_OF_MID * 2:
            return None
        return mid

    short = leg_mid(pos.strike)
    if short is None:
        return None
    if pos.structure != "put_credit_spread" or not pos.long_strike:
        return short * 100.0
    long_m = leg_mid(pos.long_strike, require_tight=False)
    if long_m is None:
        return None
    net = (short - long_m) * 100.0
    return net if net >= 0 else None


@contextmanager
def _exit_rules(exit_dte: int, stop_mult, scale=None):
    """Temporarily set the exit constants virtual_tracker.evaluate_open_position
    reads. The engine calls PRODUCTION exit code by design, so varying the
    declared exit knobs means varying those constants for the duration of the
    run — never a fork of the logic. Restored in a finally block."""
    old_dte = config.VIRTUAL_FORCE_EXIT_DTE
    old_sl = config.VIRTUAL_SL_MULTIPLE
    old_w, old_wn, old_risk = (config.SPREAD_WIDTH_WIDE,
                               config.SPREAD_WIDTH_NARROW,
                               config.MAX_RISK_PER_SPREAD)
    try:
        if scale is not None:
            # Account-scale knob (Amendment 2): both widths are set to the
            # declared width so the generator's spot-based preference cannot
            # silently fall back to a narrower one.
            config.SPREAD_WIDTH_WIDE = config.SPREAD_WIDTH_NARROW = scale[0]
            config.MAX_RISK_PER_SPREAD = scale[1]
        config.VIRTUAL_FORCE_EXIT_DTE = exit_dte
        # No stop = a multiple no mark can reach (the rule is
        # pnl <= -mult * credit; 1e9 makes it unreachable).
        config.VIRTUAL_SL_MULTIPLE = 1e9 if stop_mult is None else stop_mult
        yield
    finally:
        config.VIRTUAL_FORCE_EXIT_DTE = old_dte
        config.VIRTUAL_SL_MULTIPLE = old_sl
        config.SPREAD_WIDTH_WIDE, config.SPREAD_WIDTH_NARROW = old_w, old_wn
        config.MAX_RISK_PER_SPREAD = old_risk


def run(
    frame: pd.DataFrame,
    prices: dict[str, pd.DataFrame],
    params: BacktestParams = BacktestParams(),
    source_meta: Optional[dict] = None,
    earnings_lookup: Optional[Callable[[str, date], Optional[datetime]]] = None,
    vix_lookup: Optional[Callable[[date], Optional[float]]] = None,
    allow_sealed: bool = False,
    write_results: bool = True,
    candidates_by_date: Optional[dict] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """
    Walk the normalized chain frame date by date through the production
    pipeline. Returns {trades, equity_curve, summary, run_id, ...}.

    frame: normalized long-format puts (see data_loader).
    prices: {ticker: OHLCV DataFrame} covering the same period — MUST include
        delisted names or the run is stamped survivorship-biased.
    earnings_lookup(ticker, asof) -> next earnings datetime after asof, or
        None. Absent => the blackout gate cannot replay; the run is stamped
        earnings_gate=unavailable (MANIFEST rail).
    vix_lookup(asof) -> VIX close, for the kill switch. Absent => stamped.
    allow_sealed: unlocks dates >= 2025-01-01. The unlock is logged. Use
        ONCE, for the final pre-registered configuration (MANIFEST rail).
    """
    params.validate()
    run_id = f"bt_{uuid.uuid4().hex[:8]}"
    store_dates = getattr(frame, "dates", None)
    all_dates = (list(store_dates) if store_dates is not None
                 else sorted(d for d in frame["quote_date"].unique()))
    if date_from is not None:
        all_dates = [d for d in all_dates if d >= date_from]
    if date_to is not None:
        all_dates = [d for d in all_dates if d <= date_to]
    live_dates = [d for d in all_dates if allow_sealed or d < SEALED_START]
    sealed_skipped = len(all_dates) - len(live_dates)

    meta = dict(source_meta or {})
    stamp = {
        "run_id": run_id,
        "status": "started",
        "started_at": datetime.now().isoformat(),
        "params": asdict(params),
        "frozen_config_hash": _frozen_config_hash(),
        "date_range": [str(live_dates[0]), str(live_dates[-1])] if live_dates else None,
        "n_dates": len(live_dates),
        "sealed_dates_skipped": sealed_skipped,
        "sealed_unlocked": bool(allow_sealed),
        "survivorship": ("includes_delisted" if meta.get("includes_delisted")
                         else "biased"),
        "earnings_gate": "replayed" if earnings_lookup else "unavailable",
        "vix_gate": "replayed" if vix_lookup else "unavailable",
        # Stamped honesty gaps (same pattern as the gates above): production
        # applies an ex-div blackout to live candidates and a learning-aware
        # ranker weight — neither replays here, and a run log that hid that
        # would violate the manifest's own rails.
        "ex_div_gate": "not_replayed",
        "learning_ranker": "not_replayed",
    }
    _append_run_log(stamp)
    if allow_sealed:
        logger.warning("SEALED PERIOD UNLOCKED for run %s — this is the "
                       "one-shot test the manifest allows once.", run_id)

    price_min = config.LIVE_PRICE_MIN if params.tier == "live" else config.PRICE_MIN
    price_max = config.LIVE_PRICE_MAX if params.tier == "live" else config.PRICE_MAX

    book: list[OpenVirtualTrade] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []
    cooldown_until: dict[str, date] = {}
    missing_chains: set = set()   # ranked candidate with no chain on file
    cum_pnl = 0.0

    def _sliced(ticker: str, asof: date) -> Optional[pd.DataFrame]:
        df = prices.get(ticker)
        if df is None or df.empty:
            return None
        try:
            out = df[df.index.date <= asof]
        except (AttributeError, TypeError):
            return None
        return out if not out.empty else None

    exit_ctx = _exit_rules(params.exit_dte, params.stop_mult, params.scale)
    exit_ctx.__enter__()
    try:
      for asof in live_dates:
        asof_dt = datetime.combine(asof, datetime.min.time())

        # ---- 1. Mark & exit the open book (market mark first, model fallback)
        for pos in list(book):
            hist = _sliced(pos.ticker, asof)
            if hist is None:
                continue
            spot = data_pipeline.last_price(hist)
            if spot is None:
                continue
            iv = min(data_pipeline.recent_realized_vol(hist, window=30) or 0.30,
                     MAX_MODEL_VOL)
            market_price = _position_mark(frame, asof, pos)
            # Data-ended settlement: price rows stopped mid-hold (delisting/
            # halt). Coasting on the frozen spot to a mild 21-DTE exit would
            # censor exactly the left-tail outcomes short puts die on — close
            # NOW at the best available mark and count it visibly.
            data_age = (asof - hist.index[-1].date()).days
            if data_age > DATA_ENDED_AFTER_DAYS:
                result = evaluate_open_position(
                    pos, spot, iv, today=asof_dt, market_price=market_price)
                result["exit_now"] = True
                result["exit_reason"] = "data_ended"
            else:
                result = evaluate_open_position(
                    pos, spot, iv, today=asof_dt, market_price=market_price)
            if not result["exit_now"]:
                continue
            econ = close_economics(
                pos.credit_received, result["current_put_price"], pos.structure)
            cum_pnl += econ["pnl"]
            trades.append({
                "trade_id": pos.trade_id,
                "ticker": pos.ticker,
                "tier": pos.tier,
                "structure": pos.structure,
                "opened_on": pos.opened_at.date().isoformat(),
                "closed_on": asof.isoformat(),
                "exit_reason": result["exit_reason"],
                "mark_source": result["mark_source"],
                "credit_received": round(pos.credit_received, 2),
                "final_put_price": round(result["current_put_price"], 4),
                "pnl": round(econ["pnl"], 2),
                "pnl_pessimistic": round(econ["pnl_pessimistic"], 2),
                "pnl_gross": round(econ["pnl_gross"], 2),
                "max_loss": round(pos.max_loss, 2),
            })
            book.remove(pos)
            cooldown_until[pos.ticker] = asof + timedelta(days=REOPEN_COOLDOWN_DAYS)

        # ---- 2. Screen (the production pipeline, as-of this date)
        vix = vix_lookup(asof) if vix_lookup else None
        if vix is not None and vix > config.VIX_KILL_SWITCH:
            equity_curve.append({"date": asof.isoformat(),
                                 "cum_pnl": round(cum_pnl, 2),
                                 "open": len(book), "vix_killed": True})
            continue

        if candidates_by_date is not None:
            # PRECOMPUTED RANKING PATH (study scale). The daily ranking is a
            # pure function of stock data, so it is computed once for all
            # config cells by precompute_candidates.py rather than re-derived
            # here for every cell over ~10k ticker-histories per day. The
            # ORDER is authoritative; everything downstream (chain selection,
            # strike choice, every gate) still runs the production code.
            # verify_ranking.py proves this path reproduces the engine's own
            # filter+rank output on sampled days before any run is trusted.
            universe = list(
                candidates_by_date.get(asof.isoformat(), {}).get(
                    params.tier, []))
            day_chains = data_loader.chains_for_date(
                frame, asof, params.dte_min, params.dte_max,
                tickers=set(universe), require_two_sided=True)
            for t in universe:
                if t not in day_chains:
                    missing_chains.add((asof.isoformat(), t))
        else:
            day_chains = data_loader.chains_for_date(
                frame, asof, params.dte_min, params.dte_max)
            universe = data_loader.universe_asof(
                {t: p for t, p in prices.items() if t in day_chains},
                asof, price_min, price_max, config.MIN_DAILY_VOLUME)

        contexts = []
        for ticker in universe:
            hist = _sliced(ticker, asof)
            if hist is None:
                continue
            next_earn = earnings_lookup(ticker, asof) if earnings_lookup else None
            contexts.append(TickerContext(
                ticker=ticker,
                last_price=data_pipeline.last_price(hist) or float("nan"),
                avg_volume_20d=data_pipeline.avg_volume_20d(hist) or 0.0,
                next_earnings=next_earn,
                price_history=(hist["Adj Close"] if "Adj Close" in hist.columns
                               else hist["Close"]).copy(),
                price_min=price_min, price_max=price_max,
            ))

        passing = []
        for ctx in contexts:
            result = filters.apply_all_filters(ctx, now=asof_dt)
            if not result.passed:
                continue
            ned = ((ctx.next_earnings - asof_dt).days
                   if ctx.next_earnings is not None else None)
            passing.append({
                "ticker": ctx.ticker, "last_price": ctx.last_price,
                "avg_volume_20d": ctx.avg_volume_20d,
                "next_earnings_days": ned,
                "price_history": ctx.price_history,
            })
        if candidates_by_date is not None:
            # Preserve the precomputed order (it IS the ranker's output);
            # rank_candidates would re-sort on a re-derived RV percentile.
            order = {t: i for i, t in enumerate(universe)}
            passing.sort(key=lambda c: order.get(c["ticker"], 10_000))
            ranked = [
                ranker.RankedCandidate(
                    ticker=c["ticker"], last_price=float(c["last_price"]),
                    rv_20d_annual=float("nan"), rv_percentile=float("nan"),
                    avg_volume_20d=float(c.get("avg_volume_20d") or 0.0),
                    next_earnings_days=c.get("next_earnings_days"),
                    rank=i + 1)
                for i, c in enumerate(passing[:config.MAX_CANDIDATES_IN_EMAIL])
            ]
        else:
            ranked = ranker.rank_candidates(
                passing, top_n=config.MAX_CANDIDATES_IN_EMAIL)

        # ---- 3. Open (same book rules as production)
        open_tickers = {p.ticker for p in book}
        for cand in ranked:
            if len(book) >= config.MAX_VIRTUAL_OPEN:
                break
            if cand.ticker in open_tickers:
                continue
            until = cooldown_until.get(cand.ticker)
            if until is not None and asof <= until:
                continue
            chain = day_chains.get(cand.ticker)
            if params.tier == "live":
                setup = setup_generator.generate_spread_setup(
                    cand.ticker, cand.last_price, chain,
                    target_delta=params.target_delta,
                    next_earnings_days=cand.next_earnings_days, today=asof)
            else:
                setup = setup_generator.generate_setup(
                    cand.ticker, cand.last_price, chain,
                    target_delta=params.target_delta,
                    next_earnings_days=cand.next_earnings_days, today=asof)
            if setup is None:
                continue
            book.append(OpenVirtualTrade(
                trade_id=f"{run_id}::{asof.isoformat()}::{setup.ticker}::"
                         f"{setup.strike}::{setup.expiration}",
                screen_id=run_id,
                opened_at=asof_dt,
                ticker=setup.ticker,
                spot_at_open=setup.spot_at_screen,
                expiration=datetime.fromisoformat(setup.expiration),
                dte_at_open=setup.dte,
                strike=setup.strike,
                credit_received=setup.estimated_credit_per_contract,
                max_loss=setup.max_loss_per_contract,
                breakeven=setup.breakeven,
                iv_at_open=setup.iv,
                structure=setup.structure,
                long_strike=setup.long_strike,
                tier=setup.tier,
            ))
            open_tickers.add(setup.ticker)

        equity_curve.append({"date": asof.isoformat(),
                             "cum_pnl": round(cum_pnl, 2), "open": len(book)})

    finally:
        exit_ctx.__exit__(None, None, None)

    # ---- Summary (band reporting only — MANIFEST rail)
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    market_marked = sum(1 for t in trades if t["mark_source"] == "market")
    summary = {
        "closed_trades": n,
        "data_ended_closes": sum(
            1 for t in trades if t["exit_reason"] == "data_ended"),
        "win_rate": round(wins / n, 4) if n else None,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "total_pnl_pessimistic": round(
            sum(t["pnl_pessimistic"] for t in trades), 2),
        "avg_pnl_per_trade": round(sum(t["pnl"] for t in trades) / n, 2) if n else None,
        "avg_pnl_pessimistic_per_trade": round(
            sum(t["pnl_pessimistic"] for t in trades) / n, 2) if n else None,
        "market_marked_share": round(market_marked / n, 4) if n else None,
        "still_open_at_end": len(book),
        "survivorship": stamp["survivorship"],
        "earnings_gate": stamp["earnings_gate"],
        "vix_gate": stamp["vix_gate"],
        "ex_div_gate": stamp["ex_div_gate"],
        "learning_ranker": stamp["learning_ranker"],
        # Coverage honesty: ranked candidates whose chains were never pulled
        # cannot become trades. A high rate would mean the study silently saw
        # a thinner opportunity set than production would have.
        "missing_chain_candidate_days": len(missing_chains),
        "ranking_source": ("precomputed" if candidates_by_date is not None
                           else "engine"),
        "entry_quotes": ("two_sided_required" if candidates_by_date is not None
                         else "production_default"),
    }

    result = {"run_id": run_id, "params": asdict(params), "summary": summary,
              "trades": trades, "equity_curve": equity_curve}
    if write_results:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR / f"{run_id}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, default=str)
        result["results_path"] = str(out_path)

    _append_run_log({
        "run_id": run_id, "status": "completed",
        "completed_at": datetime.now().isoformat(),
        "summary": summary,
    })
    return result
