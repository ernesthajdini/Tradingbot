"""
Shadow book — counterfactual tracking of candidates the screener did NOT take.

WHY (LEARNING_ACCELERATION_STUDY.md): every run passes 11-19 candidates
through the filters and takes only the ranked top-5. The discards were, until
now, lost forever. Tracking them buys two things the production book cannot:

  1. RANKER VALIDATION — taken vs discarded is a same-day controlled
     contrast: both cohorts share the market regime, so the market factor
     cancels and the comparison isolates whether the RANKING adds value.
  2. EARNINGS AUTOPSY — candidates blocked ONLY by the earnings blackout get
     a counterfactual position held through the report, so the blackout's
     saved-vs-cost verdict accrues real data (~20 quasi-independent
     name-quarter events per season).

HARD RULES (each one is load-bearing — verified against golive.py):
  * Shadow events live in the 'shadow_trades' journal topic and Supabase
    table, NEVER in 'virtual_trades'. golive.gate_status() and the evaluator
    pool every close in virtual_trades; one shadow row there corrupts both
    the 200-trade counter and the market-marked share.
  * Shadow trades NEVER stage tickets, never alert, never count toward any
    gate. They are research rows, full stop.
  * Failure anywhere in this module must never break a production run —
    callers wrap every entry point in try/except.
  * Market-quote fetches are BUDGETED (the chain-fetch budget is shared
    with production marking). Free rides on production's per-run quote
    cache are always allowed; fresh fetches are capped per run and go to
    the shadows closest to an exit decision, where mark quality matters
    most. Everything else takes a model mark, honestly labeled — the
    taken-vs-shadow contrast is run on the SAME mark basis for both
    cohorts, so first-order mark bias cancels.

Cohort symmetry: shadow opens obey the SAME book rules as production (one
open per ticker, 3-day reopen cooldown, sanity + friction gates via the same
setup generators) — a fair comparison needs identical rules on both sides.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from csp_screener import config, journal, options_data
from csp_screener.virtual_tracker import (
    OpenVirtualTrade,
    close_economics,
    evaluate_open_position,
    market_quote_resolver,
)

logger = logging.getLogger(__name__)

# Structural bounds (deliberately outside config.py, like sanity.py):
# these police data-collection cost, not strategy.
MAX_NEW_RANK_SHADOWS_PER_RUN = 15    # rank-cutoff cohort, per run
MAX_NEW_EARNINGS_SHADOWS_PER_RUN = 6  # earnings-autopsy cohort, per run
# One shared CHAIN-FETCH budget for opening shadows: the 15:05 screen already
# carries ~70 yfinance calls before this module runs, inside a 15-minute job
# timeout. Earnings cohort draws first (it is the scarcer experiment).
SHADOW_OPEN_CHAIN_BUDGET = 15
SHADOW_QUOTE_BUDGET_PER_RUN = 8      # fresh quote fetches for shadow marks
SHADOW_REOPEN_COOLDOWN_DAYS = 3      # mirrors main.REOPEN_COOLDOWN_DAYS
MAX_SHADOW_OPEN = 120                # runaway backstop, not a target

TOPIC = "shadow_trades"

REASON_RANK_CUTOFF = "rank_cutoff"
REASON_EARNINGS_BLACKOUT = "earnings_blackout"


# ---------------------------------------------------------------------------
# State reconstruction (same replay pattern as the production tracker)
# ---------------------------------------------------------------------------

def get_open_shadow_trades() -> list[OpenVirtualTrade]:
    """Replay the shadow event log; return open shadow trades."""
    from csp_screener.sanity import open_event_is_sane

    events = journal.read_all(TOPIC)
    open_ids: dict = {}
    quarantined = 0
    for ev in events:
        tid = ev.get("trade_id")
        if not tid:
            continue
        if ev.get("event") == "open":
            if not open_event_is_sane(ev):
                quarantined += 1
                continue
            open_ids[tid] = ev
        elif ev.get("event") == "close":
            open_ids.pop(tid, None)
    if quarantined:
        logger.info(f"Shadow book: quarantined {quarantined} insane open event(s)")

    out = []
    for tid, ev in open_ids.items():
        try:
            out.append(OpenVirtualTrade(
                trade_id=tid,
                screen_id=ev.get("screen_id", ""),
                opened_at=datetime.fromisoformat(ev["opened_at"]),
                ticker=ev["ticker"],
                spot_at_open=float(ev["spot_at_open"]),
                expiration=datetime.fromisoformat(ev["expiration"]),
                dte_at_open=int(ev["dte_at_open"]),
                strike=float(ev["strike"]),
                credit_received=float(ev["credit_received"]),
                max_loss=float(ev["max_loss"]),
                breakeven=float(ev["breakeven"]),
                iv_at_open=ev.get("iv_at_open"),
                structure=ev.get("structure", "csp"),
                long_strike=(float(ev["long_strike"])
                             if ev.get("long_strike") is not None else None),
                tier=ev.get("tier", "sandbox"),
            ))
        except Exception as e:
            logger.warning(f"Skipping malformed shadow open event {tid}: {e}")
    return out


def recently_closed_shadow_tickers(days: int = SHADOW_REOPEN_COOLDOWN_DAYS) -> set:
    """Tickers whose most recent shadow CLOSE is within `days` (cooldown)."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    out = set()
    for ev in journal.read_all(TOPIC):
        if ev.get("event") != "close":
            continue
        tk, closed_at = ev.get("ticker"), ev.get("closed_at")
        if not tk or not closed_at:
            continue
        try:
            ts = datetime.fromisoformat(
                str(closed_at).replace("Z", "+00:00")).replace(tzinfo=None)
            if ts >= cutoff:
                out.add(tk)
        except (ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Open / close (shadow topic only)
# ---------------------------------------------------------------------------

def open_shadow_position(
    setup,
    screen_id: str,
    shadow_reason: str,
    rank_at_open: Optional[int] = None,
    rv_percentile: Optional[float] = None,
    vix: Optional[float] = None,
    next_earnings_days: Optional[int] = None,
) -> str:
    """Log a shadow OPEN. trade_id carries a 'shadow' segment so a shadow id
    can never collide with (or be mistaken for) a production trade id."""
    trade_id = (f"{screen_id}::shadow::{setup.ticker}::"
                f"{setup.strike}::{setup.expiration}")
    record = {
        "event": "open",
        "trade_id": trade_id,
        "screen_id": screen_id,
        "opened_at": datetime.now().isoformat(),
        "ticker": setup.ticker,
        "spot_at_open": setup.spot_at_screen,
        "expiration": setup.expiration,
        "dte_at_open": setup.dte,
        "strike": setup.strike,
        "credit_received": setup.estimated_credit_per_contract,
        "max_loss": setup.max_loss_per_contract,
        "breakeven": setup.breakeven,
        "delta_at_open": setup.delta,
        "iv_at_open": setup.iv,
        "data_quality": setup.data_quality,
        "structure": getattr(setup, "structure", "csp"),
        "long_strike": getattr(setup, "long_strike", None),
        "tier": getattr(setup, "tier", "sandbox"),
        "shadow_reason": shadow_reason,
    }
    if rank_at_open is not None:
        record["rank_at_open"] = int(rank_at_open)
    if rv_percentile is not None:
        record["rv_percentile_at_open"] = round(float(rv_percentile), 2)
    if vix is not None:
        record["vix_at_open"] = round(float(vix), 2)
    if next_earnings_days is not None:
        # Earnings-autopsy cohort: the hold intentionally spans this date —
        # the analyzer needs it to line closes up against the report.
        record["next_earnings_days_at_open"] = int(next_earnings_days)
    journal.append(TOPIC, record)
    logger.info(f"Shadow opened [{shadow_reason}]: {trade_id}")
    return trade_id


def close_shadow_position(
    trade: OpenVirtualTrade,
    exit_reason: str,
    exit_spot: float,
    final_put_price: float,
    notes: str = "",
    eur_usd_rate: Optional[float] = None,
) -> dict:
    """Log a shadow CLOSE — same economics as production closes, by
    construction (shared close_economics), different topic."""
    econ = close_economics(trade.credit_received, final_put_price, trade.structure)
    pnl = econ["pnl"]
    pnl_eur = None
    if eur_usd_rate and eur_usd_rate > 0:
        pnl_eur = round(pnl / eur_usd_rate, 2)
    record = {
        "event": "close",
        "trade_id": trade.trade_id,
        "ticker": trade.ticker,
        "strike": trade.strike,
        "expiration": trade.expiration.date().isoformat(),
        "structure": trade.structure,
        "tier": trade.tier,
        "long_strike": trade.long_strike,
        "closed_at": datetime.now().isoformat(),
        "exit_reason": exit_reason,
        "exit_spot": round(exit_spot, 4),
        "final_put_price": round(final_put_price, 4),
        "credit_received": round(trade.credit_received, 4),
        "pnl_gross": round(econ["pnl_gross"], 2),
        "friction": round(econ["friction"], 2),
        "pnl": round(pnl, 2),
        "pnl_pessimistic": round(econ["pnl_pessimistic"], 2),
        "eur_usd_rate": eur_usd_rate,
        "pnl_eur": pnl_eur,
        "pnl_pct_of_credit": (round(pnl / trade.credit_received, 4)
                              if trade.credit_received else 0.0),
        "notes": notes,
    }
    journal.append(TOPIC, record)
    logger.info(f"Shadow closed: {trade.trade_id} reason={exit_reason} pnl=${pnl:.2f}")
    return record


# ---------------------------------------------------------------------------
# Opening shadows from a screen run
# ---------------------------------------------------------------------------

def record_run_shadows(
    screen_id: str,
    discarded: list[dict],
    earnings_blocked: list[dict],
    production_open_tickers: set,
    production_cooldown_tickers: set,
    vix: Optional[float] = None,
) -> dict:
    """
    Open shadow positions for this run's discards. Called AFTER production
    opens, from run_weekly_screen, inside a try/except (never breaks a run).

    discarded: filter-passers ranked BELOW the top-N — dicts with ticker,
        last_price, rank_full (1-based position in the full ranking),
        rv_percentile, rv_20d_annual, next_earnings_days, tier.
    earnings_blocked: tickers rejected ONLY by the earnings blackout — dicts
        with ticker, last_price, next_earnings_days, tier. Their setups are
        generated with the earnings gate OFF (that is the counterfactual).
    production_open_tickers: tickers with an open PRODUCTION position or
        opened this run — those belong to the taken cohort, never both books.
    """
    # Cloud-persistence probe: on stateless CI runners the local journal dies
    # with the runner, so if Supabase IS configured but the shadow table
    # doesn't exist yet, every run would spend the full chain-fetch budget
    # re-opening shadows that evaporate — cost with zero data. Refuse until
    # supabase/shadow_schema.sql has been run. (Supabase not configured at
    # all = local dev, where the JSONL journal persists — proceed.)
    try:
        from csp_screener import supabase_sync
        if supabase_sync.is_enabled():
            client = supabase_sync._get_client()
            client.table("shadow_trades").select("id").limit(1).execute()
    except Exception as e:
        logger.warning(
            f"Shadow book INERT: Supabase is configured but the shadow_trades "
            f"table is unreachable ({e}) — run supabase/shadow_schema.sql in "
            f"the SQL Editor. Skipping shadow opens to avoid burning fetches "
            f"on data that cannot persist.")
        return {"opened": [], "skipped": 0, "capped": 0, "no_setup": 0,
                "dropped_by_cap": 0, "inert": True}

    open_shadows = get_open_shadow_trades()
    shadow_open_tickers = {t.ticker for t in open_shadows}
    already_open_keys = {
        (t.ticker, round(float(t.strike), 4), t.expiration.date().isoformat())
        for t in open_shadows
    }
    cooldown = recently_closed_shadow_tickers()
    open_count = len(open_shadows)

    summary = {"opened": [], "skipped": 0, "capped": 0, "no_setup": 0,
               "not_spanning": 0}
    fetch_budget = [SHADOW_OPEN_CHAIN_BUDGET]

    def _blocked(ticker: str) -> bool:
        return (ticker in shadow_open_tickers
                or ticker in production_open_tickers
                or ticker in production_cooldown_tickers
                or ticker in cooldown)

    def _ex_div_blocked(ticker: str) -> bool:
        """Mirror production's live-tier ex-dividend blackout (main.py
        step_generate_setups) — a fair taken-vs-shadow contrast needs the
        same rules on both sides."""
        try:
            from csp_screener import earnings as earn_mod
            ex_div = earn_mod.fetch_ex_dividend(ticker)
            if ex_div is not None:
                days_out = (ex_div.date() - datetime.now().date()).days
                return 0 <= days_out <= config.DTE_MAX
        except Exception as e:
            logger.debug(f"shadow ex-div check failed for {ticker}: {e}")
        return False

    def _try_open(cand: dict, reason: str, rank_at_open: Optional[int]) -> None:
        nonlocal open_count
        ticker = cand["ticker"]
        if _blocked(ticker):
            summary["skipped"] += 1
            return
        if open_count >= MAX_SHADOW_OPEN or fetch_budget[0] <= 0:
            summary["capped"] += 1
            return
        try:
            if cand.get("tier") == "live" and _ex_div_blocked(ticker):
                summary["skipped"] += 1
                return
            fetch_budget[0] -= 1
            chain = options_data.fetch_chain(ticker, cand["last_price"])
            from csp_screener import setup_generator
            # Earnings-autopsy cohort: the blackout IS the thing under test,
            # so its expiry gate is disabled (next_earnings_days=None) and
            # the true earnings distance is stamped on the record instead.
            ned = (None if reason == REASON_EARNINGS_BLACKOUT
                   else cand.get("next_earnings_days"))
            if cand.get("tier") == "live":
                setup = setup_generator.generate_spread_setup(
                    ticker, cand["last_price"], chain, next_earnings_days=ned)
            else:
                setup = setup_generator.generate_setup(
                    ticker, cand["last_price"], chain, next_earnings_days=ned)
            if setup is None:
                summary["no_setup"] += 1
                return
            if reason == REASON_EARNINGS_BLACKOUT:
                # The counterfactual must actually HOLD THROUGH the report:
                # max hold = dte - 21-DTE forced exit. A candidate whose
                # chain only offers expirations that close BEFORE earnings
                # teaches nothing and would dilute the cohort.
                ed = cand.get("next_earnings_days")
                hold = max(0, setup.dte - config.VIRTUAL_FORCE_EXIT_DTE)
                if ed is None or ed > hold:
                    summary["not_spanning"] += 1
                    return
            key = (setup.ticker, round(float(setup.strike), 4), setup.expiration)
            if key in already_open_keys:
                summary["skipped"] += 1
                return
            tid = open_shadow_position(
                setup, screen_id, shadow_reason=reason,
                rank_at_open=rank_at_open,
                rv_percentile=cand.get("rv_percentile"), vix=vix,
                next_earnings_days=(cand.get("next_earnings_days")
                                    if reason == REASON_EARNINGS_BLACKOUT
                                    else None),
            )
            summary["opened"].append(tid)
            shadow_open_tickers.add(ticker)
            already_open_keys.add(key)
            open_count += 1
        except Exception as e:
            logger.warning(f"Shadow open failed for {ticker} (non-fatal): {e}")

    # EARNINGS COHORT FIRST — it is the scarcer experiment (~20 usable
    # name-quarter events per season) and must never be starved by the
    # rank cohort under the shared caps. Nearest earnings first: with the
    # span guard those are the counterfactuals most certain to matter.
    def _days_to_earnings(c):
        d = c.get("next_earnings_days")
        return d if d is not None else 999
    earnings_sorted = sorted(earnings_blocked, key=_days_to_earnings)
    for cand in earnings_sorted[:MAX_NEW_EARNINGS_SHADOWS_PER_RUN]:
        _try_open(cand, REASON_EARNINGS_BLACKOUT, None)
    dropped_earn = max(0, len(earnings_sorted) - MAX_NEW_EARNINGS_SHADOWS_PER_RUN)

    # Rank-cutoff cohort: best-ranked discards first (nearest misses teach
    # the most about the cutoff), capped per run.
    ranked_discards = sorted(discarded, key=lambda c: c.get("rank_full") or 999)
    for cand in ranked_discards[:MAX_NEW_RANK_SHADOWS_PER_RUN]:
        _try_open(cand, REASON_RANK_CUTOFF, cand.get("rank_full"))
    dropped_rank = max(0, len(ranked_discards) - MAX_NEW_RANK_SHADOWS_PER_RUN)

    # No silent caps: what was dropped is part of the record.
    if dropped_rank or dropped_earn:
        logger.info(
            f"Shadow book: per-run caps dropped {dropped_rank} rank-cutoff "
            f"and {dropped_earn} earnings candidates (not tracked this run)")
    summary["dropped_by_cap"] = dropped_rank + dropped_earn
    logger.info(
        f"Shadow book: opened {len(summary['opened'])} "
        f"(skipped {summary['skipped']}, no-setup {summary['no_setup']}, "
        f"capped {summary['capped']})")
    return summary


# ---------------------------------------------------------------------------
# Marking (budgeted quotes + model fallback)
# ---------------------------------------------------------------------------

def mark_open_shadows(
    spot_resolver,
    iv_resolver,
    eur_usd_rate: Optional[float] = None,
    market_open: bool = False,
    quote_budget: int = SHADOW_QUOTE_BUDGET_PER_RUN,
) -> dict:
    """
    Re-price every open shadow and close the ones whose exits fire.

    Quote policy (two passes):
      pass 1 — model-mark everything (free) and rank shadows by how close
               the model says they are to an exit decision;
      pass 2 — spend the fetch budget on the closest-to-exit shadows, plus
               FREE rides for any (ticker, expiration) already in the
               production quote cache from this run's marking.
    Model-marked closes are labeled mark_source=model exactly like
    production, so no shadow number ever launders its provenance.
    """
    trades = get_open_shadow_trades()
    summary = {"updated": 0, "closed": 0, "closed_pnl_total": 0.0,
               "quotes_spent": 0, "details": []}
    if not trades:
        return summary

    # Pass 1: model marks + exit proximity
    staged = []
    for trade in trades:
        try:
            spot = spot_resolver(trade.ticker)
            iv = iv_resolver(trade.ticker)
            if spot is None or iv is None:
                continue
            model_result = evaluate_open_position(trade, spot, iv)
            # Exit proximity: exits fire on TP% / DTE / SL — distance to the
            # nearest boundary decides who deserves a real quote.
            pnl_pct = model_result["pnl_pct_of_credit"]
            dist_tp = abs(config.VIRTUAL_TP_PCT - pnl_pct)
            dist_sl = abs(pnl_pct + config.VIRTUAL_SL_MULTIPLE)
            dte_pressure = max(0, model_result["dte_remaining"]
                               - config.VIRTUAL_FORCE_EXIT_DTE)
            proximity = min(dist_tp, dist_sl) + 0.01 * dte_pressure
            if model_result["exit_now"]:
                proximity = -1.0  # exits first in line for a real quote
            staged.append((proximity, trade, spot, iv))
        except Exception as e:
            logger.warning(f"Shadow mark failed for {trade.trade_id}: {e}")

    staged.sort(key=lambda x: x[0])

    # Pass 2: budgeted quotes, then final evaluation + closes
    budget = [max(0, quote_budget) if market_open else 0]

    def _quoted_mark(trade) -> Optional[float]:
        if not market_open:
            return None
        key = (trade.ticker, trade.expiration.date().isoformat())
        cached = key in options_data._position_quote_cache
        if not cached and budget[0] <= 0:
            return None
        if not cached:
            budget[0] -= 1
            summary["quotes_spent"] += 1
        return market_quote_resolver(trade)

    for _, trade, spot, iv in staged:
        try:
            market_price = _quoted_mark(trade)
            result = evaluate_open_position(
                trade, spot, iv, market_price=market_price)
            summary["updated"] += 1
            if result["exit_now"]:
                close_record = close_shadow_position(
                    trade,
                    exit_reason=result["exit_reason"],
                    exit_spot=spot,
                    final_put_price=result["current_put_price"],
                    notes=f"days_held={(datetime.now().date() - trade.opened_at.date()).days}, "
                          f"dte_at_open={trade.dte_at_open}, "
                          f"mark_source={result['mark_source']}, "
                          f"model_price={result['model_put_price']:.2f}",
                    eur_usd_rate=eur_usd_rate,
                )
                summary["closed"] += 1
                summary["closed_pnl_total"] += close_record["pnl"]
                summary["details"].append({
                    "trade_id": trade.trade_id, "action": "closed",
                    "pnl": close_record["pnl"],
                    "reason": result["exit_reason"],
                    "mark_source": result["mark_source"],
                })
        except Exception as e:
            logger.warning(f"Shadow eval failed for {trade.trade_id}: {e}")

    logger.info(
        f"Shadow marks: {summary['updated']} updated, {summary['closed']} "
        f"closed (PnL ${summary['closed_pnl_total']:+.2f}), "
        f"{summary['quotes_spent']} fresh quotes spent")
    return summary


# ---------------------------------------------------------------------------
# The report: what the shadows are FOR
# ---------------------------------------------------------------------------

def cohort_summary() -> dict:
    """
    Taken-vs-shadow comparison + earnings autopsy, with provenance labels.

    HONESTY NOTES baked into the output:
      * shadow closes are more often model-marked than production closes
        (budgeted quotes) — mark_source shares are reported for both cohorts
        so nobody compares apples to unlabeled oranges;
      * counts are RAW trades, not effective sample (same-day shadows are
        correlated); the study's rule of thumb: 5x volume ≈ 1.3x information.
    """
    from csp_screener.evaluator import _reconstruct_closed_trades

    def _mark_share(closes: list[dict]) -> Optional[float]:
        import re
        seen = market = 0
        for c in closes:
            m = re.search(r"mark_source=(\w+)", str(c.get("notes") or ""))
            if m:
                seen += 1
                if m.group(1) == "market":
                    market += 1
        return (market / seen) if seen else None

    def _stats(closes: list[dict]) -> dict:
        n = len(closes)
        pnls = [float(c.get("pnl") or 0) for c in closes]
        pess = [float(c.get("pnl_pessimistic") or 0) for c in closes]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "closed": n,
            "win_rate": round(wins / n, 4) if n else None,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / n, 2) if n else None,
            "total_pnl_pessimistic": round(sum(pess), 2),
            "market_marked_share": _mark_share(closes),
        }

    # Shadow closes joined to their opens for reason + rank
    opens_by_id = {}
    shadow_closes = []
    for ev in journal.read_all(TOPIC):
        if ev.get("event") == "open":
            opens_by_id[ev.get("trade_id")] = ev
        elif ev.get("event") == "close":
            shadow_closes.append(ev)
    for c in shadow_closes:
        o = opens_by_id.get(c.get("trade_id")) or {}
        c["shadow_reason"] = o.get("shadow_reason")
        c["rank_at_open"] = o.get("rank_at_open")

    rank_cohort = [c for c in shadow_closes
                   if c.get("shadow_reason") == REASON_RANK_CUTOFF]
    earnings_cohort = [c for c in shadow_closes
                       if c.get("shadow_reason") == REASON_EARNINGS_BLACKOUT]

    # SAME-WINDOW comparison: the whole point of the contrast is that both
    # cohorts share the market regime. Production closes from before the
    # first shadow ever opened would mix months of different regimes into
    # the "taken" side — restrict it to the shadow era.
    taken = _reconstruct_closed_trades()
    shadow_era_start = min(
        (o.get("opened_at") for o in opens_by_id.values() if o.get("opened_at")),
        default=None)
    if shadow_era_start:
        taken = [t for t in taken
                 if str(t.get("closed_at") or "") >= str(shadow_era_start)]

    return {
        "taken": _stats(taken),
        "taken_window_start": shadow_era_start,
        "shadow_rank_cutoff": _stats(rank_cohort),
        "shadow_earnings_blackout": _stats(earnings_cohort),
        "open_shadows": len(get_open_shadow_trades()),
        "caveats": [
            "Counts are raw trades, not effective sample — same-day trades "
            "share the market factor (5x volume ≈ 1.3x information).",
            "Compare cohorts on the same mark basis; mark_source shares "
            "are reported for exactly that reason.",
            "The earnings cohort holds THROUGH earnings by design — its "
            "P&L is the counterfactual cost/saving of the blackout.",
            "The taken cohort is restricted to closes after the first "
            "shadow open (taken_window_start) so both sides share regimes.",
        ],
    }
