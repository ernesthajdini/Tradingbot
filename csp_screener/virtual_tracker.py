"""
Virtual position tracker — the heart of self-evaluation.

For every weekly suggestion the screener makes, this module:
  1. Opens a virtual cash-secured put position (logs OPEN event)
  2. Re-prices it daily based on current underlying price
  3. Applies the same exit rules a disciplined trader would (TP / DTE / SL)
  4. Closes the virtual position when an exit triggers (logs CLOSE event)

The result is a real, measurable performance record for the screener — even
when the user takes ZERO real trades. Over months this answers the critical
question: "would this strategy have made money?"

We use a simplified pricing model for daily re-pricing:
  - For a sold cash-secured put: virtual P&L = credit_received - (current_put_price)
  - Current put price is computed via simplified Black-Scholes using:
      * spot = today's close
      * strike = stored strike
      * t = remaining DTE / 365
      * sigma = current realized vol of the underlying (proxy for IV)
      * rate = 0 (risk-free rate negligible at these timescales)

This is a PROXY, not the actual market option price. It's directionally correct
enough to evaluate whether the strategy works, which is the whole point.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from csp_screener import config, journal
from csp_screener.setup_generator import VirtualSetup

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Open / re-price / close
# ---------------------------------------------------------------------------

def open_virtual_position(
    setup: VirtualSetup,
    screen_id: str,
    rv_percentile: float | None = None,
    vix: float | None = None,
    portfolio_fit: bool | None = None,
) -> str:
    """
    Log an OPEN event. Returns the virtual trade_id.
    trade_id = "{screen_id}::{ticker}::{strike}::{expiration}".

    rv_percentile / vix stamp the ENTRY context onto the record. The feature
    analyzer buckets closed trades by these to learn which vol regimes win —
    without stamping them at open, those two learning dimensions can never
    populate (the journal is append-only; there is no backfill later).
    """
    trade_id = f"{screen_id}::{setup.ticker}::{setup.strike}::{setup.expiration}"
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
        # Two-tier extensions
        "structure": getattr(setup, "structure", "csp"),
        "long_strike": getattr(setup, "long_strike", None),
        "tier": getattr(setup, "tier", "sandbox"),
    }
    if rv_percentile is not None:
        record["rv_percentile_at_open"] = round(float(rv_percentile), 2)
    if vix is not None:
        record["vix_at_open"] = round(float(vix), 2)
    if portfolio_fit is not None:
        # Could the ACCOUNT have carried this at the time? Signal quality is
        # measured over every trade; account performance over the subset the
        # caps allowed. Two questions, two scoreboards, one journal.
        record["portfolio_fit"] = bool(portfolio_fit)
    if setup.affordable_contracts is not None:
        record["affordable_contracts_at_open"] = int(setup.affordable_contracts)
    journal.append("virtual_trades", record)
    logger.info(f"Virtual position opened [{record['tier']}/{record['structure']}]: {trade_id}")
    return trade_id


def close_economics(
    credit_received: float,
    final_put_price: float,
    structure: str = "csp",
) -> dict:
    """
    The one friction model, as a pure function. Gross PnL is the frictionless
    mark; "pnl" is NET at BASE slippage everywhere downstream;
    "pnl_pessimistic" is the wide-spread bound — paper fills contain no
    slippage information, so honest reporting is a band, not a point.
    Shared by the production close path, the shadow book and the backtest
    engine so no surface can compute a different P&L for the same trade.
    """
    pnl_gross = credit_received - final_put_price
    # Contracts crossing the tape round-trip: CSP = 2, spread = 4.
    legs_round_trip = 4 if structure == "put_credit_spread" else 2
    commissions = legs_round_trip * config.COMMISSION_PER_CONTRACT
    slip_base = config.SLIPPAGE_PCT_OF_PREMIUM * (credit_received + final_put_price)
    slip_pess = config.SLIPPAGE_PCT_PESSIMISTIC * (credit_received + final_put_price)
    friction = commissions + slip_base
    friction_pessimistic = commissions + slip_pess
    return {
        "pnl_gross": pnl_gross,
        "friction": friction,
        "friction_pessimistic": friction_pessimistic,
        "pnl": pnl_gross - friction,
        "pnl_pessimistic": pnl_gross - friction_pessimistic,
    }


def close_virtual_position(
    trade_id: str,
    exit_reason: str,
    exit_spot: float,
    final_put_price: float,
    credit_received: float,
    notes: str = "",
    ticker: Optional[str] = None,
    strike: Optional[float] = None,
    expiration: Optional[str] = None,
    structure: str = "csp",
    tier: str = "sandbox",
    long_strike: Optional[float] = None,
    eur_usd_rate: Optional[float] = None,
) -> dict:
    """
    Log a CLOSE event. PnL = credit - final_put_price (per contract).

    ticker/strike/expiration are REQUIRED downstream (Supabase has NOT NULL
    on ticker). If the caller doesn't pass them, we recover them from the
    trade_id, which is formatted "{screen_id}::{ticker}::{strike}::{expiration}".
    """
    if ticker is None or strike is None or expiration is None:
        parts = trade_id.split("::")
        if len(parts) == 4:
            ticker = ticker or parts[1]
            if strike is None:
                try:
                    strike = float(parts[2])
                except ValueError:
                    strike = None
            expiration = expiration or parts[3]

    econ = close_economics(credit_received, final_put_price, structure)
    pnl_gross = econ["pnl_gross"]
    friction = econ["friction"]
    pnl = econ["pnl"]
    pnl_pessimistic = econ["pnl_pessimistic"]

    pnl_eur = None
    if eur_usd_rate and eur_usd_rate > 0:
        pnl_eur = round(pnl / eur_usd_rate, 2)

    record = {
        "event": "close",
        "trade_id": trade_id,
        "ticker": ticker,
        "strike": strike,
        "expiration": expiration,
        "structure": structure,
        "tier": tier,
        "long_strike": long_strike,
        "closed_at": datetime.now().isoformat(),
        "exit_reason": exit_reason,
        "exit_spot": round(exit_spot, 4),
        "final_put_price": round(final_put_price, 4),
        "credit_received": round(credit_received, 4),
        "pnl_gross": round(pnl_gross, 2),
        "friction": round(friction, 2),
        "pnl": round(pnl, 2),
        "pnl_pessimistic": round(pnl_pessimistic, 2),
        "eur_usd_rate": eur_usd_rate,
        "pnl_eur": pnl_eur,
        "pnl_pct_of_credit": round(pnl / credit_received, 4) if credit_received else 0.0,
        "notes": notes,
    }
    journal.append("virtual_trades", record)
    logger.info(f"Virtual position closed: {trade_id} reason={exit_reason} pnl=${pnl:.2f}")
    return record


# ---------------------------------------------------------------------------
# State reconstruction from event log (the only way to know what's open)
# ---------------------------------------------------------------------------

@dataclass
class OpenVirtualTrade:
    trade_id: str
    screen_id: str
    opened_at: datetime
    ticker: str
    spot_at_open: float
    expiration: datetime
    dte_at_open: int
    strike: float
    credit_received: float
    max_loss: float
    breakeven: float
    iv_at_open: Optional[float]
    structure: str = "csp"
    long_strike: Optional[float] = None
    tier: str = "sandbox"


def get_open_virtual_trades() -> list[OpenVirtualTrade]:
    """
    Replay the event log and return currently-open virtual trades.
    A trade is open iff there's an OPEN event for trade_id without a matching
    CLOSE event.
    """
    from csp_screener.sanity import open_event_is_sane

    events = journal.read_all("virtual_trades")
    open_ids = {}
    quarantined = 0
    for ev in events:
        tid = ev.get("trade_id")
        if not tid:
            continue
        if ev.get("event") == "open":
            # Retroactive quarantine: garbage-quote trades (LCID incident)
            # are excluded from replay — never marked, never counted. The
            # journal rows stay untouched (append-only).
            if not open_event_is_sane(ev):
                quarantined += 1
                continue
            open_ids[tid] = ev
        elif ev.get("event") == "close":
            open_ids.pop(tid, None)
    if quarantined:
        logger.info(f"Quarantined {quarantined} open event(s) failing credit sanity")

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
            logger.warning(f"Skipping malformed open event {tid}: {e}")
    return out


# ---------------------------------------------------------------------------
# Black-Scholes proxy for daily re-pricing
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Cumulative normal CDF using erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_put_price(
    spot: float,
    strike: float,
    days_to_expiry: int,
    sigma: float,
    rate: float = 0.0,
) -> float:
    """
    Simplified BS put price. Returns price PER SHARE (multiply by 100 for
    contract-level dollars).

    sigma is annualized volatility (e.g., 0.45 for 45%).
    rate = 0 (negligible at these timescales and rate environments).
    """
    if days_to_expiry <= 0:
        # At expiry: put value = max(strike - spot, 0)
        return max(strike - spot, 0.0)
    if sigma <= 0 or spot <= 0 or strike <= 0:
        # Degenerate inputs — fall back to intrinsic value
        return max(strike - spot, 0.0)

    t = days_to_expiry / 365.0
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    put = strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(put, 0.0)


# ---------------------------------------------------------------------------
# Daily mark-to-market + exit logic
# ---------------------------------------------------------------------------

def evaluate_open_position(
    trade: OpenVirtualTrade,
    current_spot: float,
    current_iv: float,
    today: Optional[datetime] = None,
    market_price: Optional[float] = None,
) -> dict:
    """
    Compute current virtual P&L and check if any exit rule fires.

    market_price: the position's CURRENT market value per contract (from a
    real, zombie-filtered quote). When provided, it IS the mark and the BS
    model runs only for the model_put_price comparison field — the model
    'prices profits too early' by its own admission, so market marks are
    the honest input to the go-live gate.

    Returns dict:
      {
        'current_put_price': float,     # the mark actually used
        'model_put_price': float,       # BS/RV model value (comparison)
        'mark_source': 'market'|'model',
        'pnl': float,
        'pnl_pct_of_credit': float,
        'dte_remaining': int,
        'exit_now': bool,
        'exit_reason': str or None,
      }
    """
    today = today or datetime.now()
    dte_remaining = max(0, (trade.expiration.date() - today.date()).days)
    credit_per_share = trade.credit_received / 100.0

    # Sigma: blend current realized vol with the IV captured at entry.
    # Options systematically trade ABOVE realized vol (the vol risk premium),
    # so pricing with RV alone underprices the put, marks profits too early,
    # and inflates the win rate with take-profits that wouldn't have filled.
    sigma = current_iv
    if trade.iv_at_open:
        try:
            # Clamp legacy/garbage open IVs (the LCID zombie carried IV 4.85,
            # which priced the put down so fast the mark minted a fake +54%
            # "take-profit win" on day one). 2.5 is above any legit squeeze
            # IV seen in this universe.
            iv_open = min(float(trade.iv_at_open), 2.5)
            if iv_open > 0:
                sigma = 0.5 * current_iv + 0.5 * iv_open
        except (ValueError, TypeError):
            pass

    current_put_price_per_share = black_scholes_put_price(
        spot=current_spot,
        strike=trade.strike,
        days_to_expiry=dte_remaining,
        sigma=sigma,
    )
    # Put credit spread: position value = short put - long put (both re-priced)
    if trade.structure == "put_credit_spread" and trade.long_strike:
        long_price_per_share = black_scholes_put_price(
            spot=current_spot,
            strike=trade.long_strike,
            days_to_expiry=dte_remaining,
            sigma=sigma,
        )
        current_put_price_per_share = max(
            0.0, current_put_price_per_share - long_price_per_share
        )
    model_put_price = current_put_price_per_share * 100.0

    if market_price is not None and market_price >= 0:
        current_put_price = market_price
        mark_source = "market"
    else:
        current_put_price = model_put_price
        mark_source = "model"

    pnl = trade.credit_received - current_put_price
    pnl_pct = pnl / trade.credit_received if trade.credit_received else 0.0

    exit_now = False
    exit_reason: Optional[str] = None

    # Exit rule 1: 50% of max profit captured
    if pnl_pct >= config.VIRTUAL_TP_PCT:
        exit_now = True
        exit_reason = "take_profit_50pct"
    # Exit rule 2: <= 21 DTE
    elif dte_remaining <= config.VIRTUAL_FORCE_EXIT_DTE:
        exit_now = True
        exit_reason = "force_exit_dte"
    # Exit rule 3: loss reaches -2x credit (stop-loss)
    elif pnl <= -config.VIRTUAL_SL_MULTIPLE * trade.credit_received:
        exit_now = True
        exit_reason = "stop_loss_2x_credit"
    # Exit rule 4: at expiration
    elif dte_remaining == 0:
        exit_now = True
        exit_reason = "expired"

    return {
        "current_put_price": round(current_put_price, 4),
        "model_put_price": round(model_put_price, 4),
        "mark_source": mark_source,
        "pnl": round(pnl, 2),
        "pnl_pct_of_credit": round(pnl_pct, 4),
        "dte_remaining": dte_remaining,
        "exit_now": exit_now,
        "exit_reason": exit_reason,
    }


def recently_closed_tickers(days: int = 3) -> set:
    """
    Tickers whose most recent CLOSE is within `days` — used as a reopen
    cooldown. Production evidence (Jul 2026): T cycled open→close→reopen
    four times in four days, burning round-trip friction on every lap and
    stuffing the paper pool with correlated repeats. A trade that just
    closed teaches nothing new for a few days; a diverse pool does.
    """
    cutoff = datetime.now() - timedelta(days=days)
    out = set()
    for ev in journal.read_all("virtual_trades"):
        if ev.get("event") != "close":
            continue
        tk = ev.get("ticker")
        closed_at = ev.get("closed_at")
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


def market_quote_resolver(trade: "OpenVirtualTrade") -> Optional[float]:
    """
    Real-quote mark for an open position (per contract), or None to fall
    back to the model. Routed through the zombie/staleness defenses.
    """
    try:
        from csp_screener import options_data
        long_strike = (trade.long_strike
                       if trade.structure == "put_credit_spread" else None)
        return options_data.fetch_position_quote(
            trade.ticker,
            trade.expiration.date().isoformat(),
            trade.strike,
            long_strike,
        )
    except Exception as e:
        logger.debug(f"market quote unavailable for {trade.trade_id}: {e}")
        return None


def update_all_open_positions(
    spot_resolver,
    iv_resolver,
    today: Optional[datetime] = None,
    eur_usd_rate: Optional[float] = None,
    quote_resolver=None,
    market_open: Optional[bool] = None,
) -> dict:
    """
    Loop through every open virtual trade, re-price, close if exit triggers.

    spot_resolver(ticker) -> float: current spot price
    iv_resolver(ticker) -> float: current vol estimate (use realized vol)

    market_open: MARKET-HOURS EXIT EXECUTION. A real position can only be
    closed while the market trades — an exit rule crossing on an after-hours
    mark is a detection, not a fill (a GTC buyback fills intraday or not at
    all). When market_open is False, exits are DEFERRED: the position stays
    open and re-evaluates on the next market-open run, where the close can
    take a REAL quote instead of a model mark. (Root cause of the 0%%
    market-marked closes: DTE exits kept executing on the after-hours EOD
    cron — SLV/SOFI closed at 00:27 UTC on model marks their live quotes
    would have passed intraday.) Exceptions: expiration (dte 0) settles
    immediately — model price at expiry IS intrinsic; and market_open=None
    keeps the legacy close-immediately behavior for existing callers.

    Returns summary: {'updated': N, 'closed': N, 'deferred': N,
    'closed_pnl_total': $X}
    """
    today = today or datetime.now()
    open_trades = get_open_virtual_trades()

    summary = {
        "updated": 0,
        "closed": 0,
        "deferred": 0,
        "closed_pnl_total": 0.0,
        "details": [],
    }

    for trade in open_trades:
        try:
            spot = spot_resolver(trade.ticker)
            iv = iv_resolver(trade.ticker)
            if spot is None or iv is None:
                logger.warning(f"No spot/iv for {trade.ticker}; skipping eval")
                continue

            market_price = quote_resolver(trade) if quote_resolver else None
            result = evaluate_open_position(
                trade, spot, iv, today=today, market_price=market_price)
            summary["updated"] += 1

            if result["exit_now"] and market_open is False and result["dte_remaining"] > 0:
                # Market closed and not expiry: detection, not a fill. The
                # position re-evaluates fresh next run — if the crossing was
                # a model artifact that recedes by the open, no exit happens,
                # which is exactly what a real GTC order would have done.
                summary["deferred"] += 1
                summary["details"].append({
                    "trade_id": trade.trade_id,
                    "action": "exit_deferred",
                    "reason": result["exit_reason"],
                    "pnl_now": result["pnl"],
                    "dte_remaining": result["dte_remaining"],
                    "mark_source": result["mark_source"],
                })
                logger.info(
                    f"Exit {result['exit_reason']} for {trade.trade_id} "
                    f"deferred — market closed; executes on the next "
                    f"market-open run")
                continue

            if result["exit_now"]:
                close_record = close_virtual_position(
                    trade_id=trade.trade_id,
                    exit_reason=result["exit_reason"],
                    exit_spot=spot,
                    final_put_price=result["current_put_price"],
                    credit_received=trade.credit_received,
                    notes=f"days_held={(today.date() - trade.opened_at.date()).days}, "
                          f"dte_at_open={trade.dte_at_open}, "
                          f"mark_source={result['mark_source']}, "
                          f"model_price={result['model_put_price']:.2f}",
                    ticker=trade.ticker,
                    strike=trade.strike,
                    expiration=trade.expiration.date().isoformat(),
                    structure=trade.structure,
                    tier=trade.tier,
                    long_strike=trade.long_strike,
                    eur_usd_rate=eur_usd_rate,
                )
                summary["closed"] += 1
                summary["closed_pnl_total"] += close_record["pnl"]
                summary["details"].append({
                    "trade_id": trade.trade_id,
                    "action": "closed",
                    "pnl": close_record["pnl"],
                    "reason": result["exit_reason"],
                    "mark_source": result["mark_source"],
                })
            else:
                summary["details"].append({
                    "trade_id": trade.trade_id,
                    "action": "marked",
                    "pnl_now": result["pnl"],
                    "dte_remaining": result["dte_remaining"],
                    "mark_source": result["mark_source"],
                })
        except Exception as e:
            logger.error(f"Error processing virtual trade {trade.trade_id}: {e}")
            continue

    return summary
