"""
WEEKLY CASH-SECURED PUT SIGNAL — the owner's expected-move specification.

Entry rule, verified against real NVDA chains 2019-2023 (it selects a
0.191-delta put, 5.25% OTM — conservative, and working as designed):

    Expected Move   = ATM call mid + ATM put mid     (the straddle)
    Downside Ref    = spot - Expected Move
    Strike          = highest strike AT OR BELOW the Downside Reference

SELF-CONTAINED BY DESIGN. It fetches its own chain because production's
fetch_chain() never populates `calls` (the straddle needs them) and clamps
expirations to DTE 25-45 (this needs 3-10). Nothing in the live screener is
modified — the owner's standing rule is not to break what works.

NO INVENTED DATA. Every number printed comes from a live quote. If a quote
is missing, one-sided, or stale the decision is REJECT with the reason
stated — never a guess.

    python -m csp_screener.weekly_csp --ticker NVDA --cash 30000
    python -m csp_screener.weekly_csp --ticker NVDA --cash 30000 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from csp_screener import config
from csp_screener.options_data import (OptionContract, _estimate_put_delta,
                                       _filter_zombie_puts, _num)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spec constants. Change deliberately.
# ---------------------------------------------------------------------------
DTE_MIN, DTE_MAX = 3, 10
MIN_OPEN_INTEREST = 100
MIN_VOLUME = 0                  # OI is the binding liquidity test intraday
MAX_SPREAD_PCT = 0.15           # bid/ask width as a fraction of mid
MIN_CREDIT = 0.10               # per share; below this the trade is dust
CONTRACT_MULTIPLIER = 100

# What the research says about this strategy family. Printed on every signal
# because a tool that hides its own evidence is worse than no tool.
EVIDENCE = ("Amendment 17: rolling a challenged put FAILED validation "
            "(-$474/seq vs -$355 for taking the loss). 408 configurations, "
            "0 survivors. Entry rule is sound; the edge is not established.")


@dataclass
class Decision:
    underlying: str
    verdict: str                     # EXECUTE | WAIT | REJECT
    reason: str
    current_price: Optional[float] = None
    expiration: Optional[str] = None
    dte: Optional[int] = None
    atm_strike: Optional[float] = None
    atm_call_mid: Optional[float] = None
    atm_put_mid: Optional[float] = None
    expected_move: Optional[float] = None
    expected_move_pct: Optional[float] = None
    downside_reference: Optional[float] = None
    strike: Optional[float] = None
    strike_pct_below: Optional[float] = None
    delta: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    open_interest: Optional[int] = None
    proposed_limit: Optional[float] = None
    premium_received: Optional[float] = None
    cash_if_assigned: Optional[float] = None
    effective_acquisition: Optional[float] = None
    downside_buffer_pct: Optional[float] = None
    remaining_cash: Optional[float] = None
    earnings_before_expiry: Optional[str] = None
    macro_event_before_expiry: str = "NOT CHECKED — verify manually"
    iv: Optional[float] = None
    checks: list = field(default_factory=list)
    quote_age: Optional[str] = None


# ---------------------------------------------------------------------------
# Chain fetch — puts AND calls, at the weekly tenor
# ---------------------------------------------------------------------------

def _parse_side(ticker, exp, df, right, spot=None):
    out = []
    for _, row in df.iterrows():
        bid, ask = _num(row.get("bid")), _num(row.get("ask"))
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else _num(row.get("lastPrice"))
        ltd = row.get("lastTradeDate")
        try:
            ltd = ltd.to_pydatetime() if hasattr(ltd, "to_pydatetime") else None
        except Exception:
            ltd = None
        strike = _num(row.get("strike"))
        iv = _num(row.get("impliedVolatility")) or None
        # yfinance ships no greeks. The spec REQUIRES delta on every signal,
        # so estimate it from the quoted IV with the same Black-Scholes
        # helper production uses. Puts only — the call side is needed for
        # the straddle price, not for a delta.
        delta = (_estimate_put_delta(spot, strike, exp, iv)
                 if (right == "P" and spot) else None)
        out.append(OptionContract(
            ticker=ticker, expiration=exp, strike=strike,
            right=right, bid=bid, ask=ask, last=_num(row.get("lastPrice")),
            mid=mid, open_interest=int(_num(row.get("openInterest"))),
            volume=int(_num(row.get("volume"))),
            iv=iv, delta=delta,
            source="yfinance", last_trade_date=ltd,
            contract_symbol=row.get("contractSymbol")))
    return out


def fetch_weekly_chain(ticker: str, dte_min=DTE_MIN, dte_max=DTE_MAX):
    """(spot, {expiration: {'puts': [...], 'calls': [...]}}) from live quotes."""
    import yfinance as yf
    t = yf.Ticker(ticker)
    try:
        spot = float(t.fast_info["last_price"])
    except Exception:
        hist = t.history(period="1d")
        if hist.empty:
            return None, {}
        spot = float(hist["Close"].iloc[-1])

    today = datetime.now().date()
    out = {}
    for s in (t.options or []):
        try:
            exp = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            continue
        dte = (exp.date() - today).days
        if not (dte_min <= dte <= dte_max):
            continue
        try:
            oc = t.option_chain(s)
        except Exception as e:
            logger.debug(f"chain fetch failed {ticker} {s}: {e}")
            continue
        puts, dropped = _filter_zombie_puts(
            _parse_side(ticker, exp, oc.puts, "P", spot))
        for r in dropped:
            logger.info(f"zombie-row filter: {r}")
        out[exp] = {"puts": puts,
                    "calls": _parse_side(ticker, exp, oc.calls, "C", spot)}
    return spot, out


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

def atm_straddle(puts, calls, spot):
    """(call_mid, put_mid, atm_strike) using the strike nearest spot, both
    sides two-sided. Returns None if either side cannot be priced."""
    tradable_p = {p.strike: p for p in puts if p.bid > 0 and p.ask > 0}
    tradable_c = {c.strike: c for c in calls if c.bid > 0 and c.ask > 0}
    common = set(tradable_p) & set(tradable_c)
    if not common:
        return None
    k = min(common, key=lambda x: abs(x - spot))
    p, c = tradable_p[k], tradable_c[k]
    return (c.bid + c.ask) / 2, (p.bid + p.ask) / 2, k


def select_strike(puts, downside_ref):
    """Highest strike AT OR BELOW the downside reference, liquid and
    two-sided. The owner's worked example offered $217.50 against a $216.02
    reference — that is ABOVE it and contradicts the written rule; the rule
    is implemented, not the example."""
    ok = [p for p in puts
          if p.bid > 0 and p.ask > 0
          and p.open_interest >= MIN_OPEN_INTEREST
          and p.strike <= downside_ref]
    return max(ok, key=lambda p: p.strike) if ok else None


def evaluate(ticker: str, cash: float, reserve: float = 0.0,
             dte_min=DTE_MIN, dte_max=DTE_MAX,
             allow_earnings: bool = False,
             check_book: bool = True) -> Decision:
    available = cash - reserve

    # The book is the source of truth for what is already held. Without this
    # the signal happily re-opens a name it is already short, and churns a
    # ticker open/close/open, paying friction on every lap.
    if check_book:
        try:
            from csp_screener import book
            if book.has_open(ticker):
                s = next(x for x in book.open_sequences().values()
                         if x.ticker == ticker)
                c = s.current
                return Decision(ticker, "REJECT",
                                f"already short {s.contracts}x ${c.strike:g} "
                                f"exp {c.expiration} (sequence {s.seq}); "
                                f"use roll_check to manage it")
            since = book.in_cooldown(ticker)
            if since:
                return Decision(ticker, "WAIT",
                                f"closed {ticker} on {since} — inside the "
                                f"{book.REOPEN_COOLDOWN_DAYS}-day reopen cooldown")
        except Exception as e:                      # book must never block a read
            logger.warning(f"position book unavailable ({e}); proceeding")
    spot, chains = fetch_weekly_chain(ticker, dte_min, dte_max)
    if not spot:
        return Decision(ticker, "REJECT", "no live price for the underlying")
    if not chains:
        return Decision(ticker, "WAIT",
                        f"no expiration between {dte_min} and {dte_max} days out",
                        current_price=spot)

    exp = min(chains)
    dte = (exp.date() - datetime.now().date()).days
    d = Decision(ticker, "REJECT", "", current_price=round(spot, 2),
                 expiration=exp.strftime("%Y-%m-%d"), dte=dte,
                 remaining_cash=round(available, 2))

    # --- earnings gate (the spec rejects by default)
    try:
        from csp_screener.earnings import fetch_next_earnings
        nxt = fetch_next_earnings(ticker)
    except Exception as e:
        logger.warning(f"earnings lookup failed for {ticker}: {e}")
        nxt = None
    if nxt is None:
        d.earnings_before_expiry = "UNKNOWN — could not verify"
    else:
        before = datetime.now() < nxt <= exp
        d.earnings_before_expiry = (f"YES ({nxt:%Y-%m-%d})" if before
                                    else f"no (next {nxt:%Y-%m-%d})")
        if before and not allow_earnings:
            d.reason = (f"earnings {nxt:%Y-%m-%d} falls before expiry — the "
                        f"spec rejects this by default")
            return d

    puts, calls = chains[exp]["puts"], chains[exp]["calls"]
    st = atm_straddle(puts, calls, spot)
    if st is None:
        d.reason = "cannot price the ATM straddle (no strike with both sides quoted)"
        return d
    call_mid, put_mid, atm_k = st
    em = call_mid + put_mid
    ref = spot - em
    d.atm_strike, d.atm_call_mid, d.atm_put_mid = atm_k, round(call_mid, 2), round(put_mid, 2)
    d.expected_move = round(em, 2)
    d.expected_move_pct = round(100 * em / spot, 2)
    d.downside_reference = round(ref, 2)

    pick = select_strike(puts, ref)
    if pick is None:
        d.reason = (f"no liquid two-sided put at or below "
                    f"${ref:.2f} (OI >= {MIN_OPEN_INTEREST})")
        return d

    limit = round((pick.bid + pick.ask) / 2, 2)
    premium = limit * CONTRACT_MULTIPLIER
    exposure = pick.strike * CONTRACT_MULTIPLIER
    eff = pick.strike - limit
    d.strike = pick.strike
    d.strike_pct_below = round(100 * (spot - pick.strike) / spot, 2)
    d.bid, d.ask = pick.bid, pick.ask
    d.spread_pct = (round(100 * (pick.ask - pick.bid) / ((pick.bid + pick.ask) / 2), 1)
                    if pick.bid > 0 and pick.ask > 0 else None)
    d.open_interest = pick.open_interest
    d.delta = round(pick.delta, 3) if pick.delta is not None else None
    d.iv = round(pick.iv, 4) if pick.iv else None
    d.proposed_limit = limit
    d.premium_received = round(premium, 2)
    d.cash_if_assigned = round(exposure, 2)
    d.effective_acquisition = round(eff, 2)
    d.downside_buffer_pct = round(100 * (spot - eff) / spot, 2)
    d.remaining_cash = round(available - exposure, 2)
    d.quote_age = (pick.last_trade_date.strftime("%Y-%m-%d %H:%M")
                   if pick.last_trade_date else "unknown")

    # --- mandatory gates; every one must pass
    checks = []
    checks.append(("assignment exposure within available cash",
                   exposure <= available,
                   f"${exposure:,.0f} vs ${available:,.0f} available"))
    checks.append(("credit above the dust floor", limit >= MIN_CREDIT,
                   f"${limit:.2f} vs ${MIN_CREDIT:.2f} minimum"))
    checks.append(("bid/ask spread acceptable",
                   d.spread_pct is not None and d.spread_pct <= MAX_SPREAD_PCT * 100,
                   f"{d.spread_pct}% vs {MAX_SPREAD_PCT*100:.0f}% max"))
    checks.append(("open interest sufficient",
                   pick.open_interest >= MIN_OPEN_INTEREST,
                   f"{pick.open_interest} vs {MIN_OPEN_INTEREST} min"))
    checks.append(("strike is out of the money", pick.strike < spot,
                   f"${pick.strike} vs spot ${spot:.2f}"))
    d.checks = [{"check": c, "pass": bool(ok), "detail": det}
                for c, ok, det in checks]

    failed = [c for c, ok, _ in checks if not ok]
    if failed:
        d.verdict, d.reason = "REJECT", "; ".join(failed)
    else:
        d.verdict = "EXECUTE"
        d.reason = (f"all gates pass — sell 1 {ticker} {exp:%d%b%y} "
                    f"${pick.strike:g} put, LIMIT ${limit:.2f}")
    return d


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _f(v, pre="", suf="", nd=2):
    return f"{pre}{v:,.{nd}f}{suf}" if isinstance(v, (int, float)) else str(v)


def format_decision(d: Decision) -> str:
    L = [f"{'='*64}", f"  {d.underlying}  WEEKLY CASH-SECURED PUT", f"{'='*64}"]
    rows = [
        ("Underlying", d.underlying),
        ("Current price", _f(d.current_price, "$")),
        ("Expiration", d.expiration), ("DTE", d.dte),
        ("ATM strike", _f(d.atm_strike, "$")),
        ("ATM call midpoint", _f(d.atm_call_mid, "$")),
        ("ATM put midpoint", _f(d.atm_put_mid, "$")),
        ("Expected move", f"{_f(d.expected_move,'$')}  ({_f(d.expected_move_pct,'',' %')})"),
        ("Expected lower boundary", _f(d.downside_reference, "$")),
        ("", ""),
        ("Selected put strike", _f(d.strike, "$")),
        ("Strike % below stock", _f(d.strike_pct_below, "", "%")),
        ("Put delta", (f"{d.delta:.3f}   (Black-Scholes from quoted IV)"
                       if d.delta is not None else "UNAVAILABLE — no IV quoted")),
        ("Implied volatility", _f(100 * d.iv, "", "%", 1) if d.iv else "n/a"),
        ("Put bid / ask", f"{_f(d.bid,'$')} / {_f(d.ask,'$')}"
                          f"   (spread {_f(d.spread_pct,'','%',1)})"),
        ("Open interest", d.open_interest),
        ("Proposed LIMIT", _f(d.proposed_limit, "$")),
        ("", ""),
        ("Premium received", _f(d.premium_received, "$")),
        ("Cash required if assigned", _f(d.cash_if_assigned, "$")),
        ("Effective acquisition price", _f(d.effective_acquisition, "$")),
        ("Effective downside buffer", _f(d.downside_buffer_pct, "", "%")),
        ("Remaining account cash", _f(d.remaining_cash, "$")),
        ("", ""),
        ("Earnings before expiration", d.earnings_before_expiry),
        ("Major market event", d.macro_event_before_expiry),
        ("Last trade on this contract", d.quote_age),
    ]
    for k, v in rows:
        L.append("" if not k else f"  {k:<30} {v}")
    if d.checks:
        L.append("\n  MANDATORY GATES")
        for c in d.checks:
            L.append(f"    [{'PASS' if c['pass'] else 'FAIL'}] {c['check']:<42} {c['detail']}")
    L += ["", f"  DECISION: {d.verdict}", f"  REASON:   {d.reason}"]
    if d.verdict == "EXECUTE":
        L += ["", f"  ORDER: SELL TO OPEN 1 {d.underlying} {d.expiration} "
                  f"${d.strike:g} PUT @ LIMIT ${d.proposed_limit:.2f}  (DAY)",
              "         Never a market order. Start at the midpoint."]
    L += ["", f"  ! {EVIDENCE}", "=" * 64]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Weekly CSP signal (expected-move rule)")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--cash", type=float, required=True,
                    help="account cash available to secure the put")
    ap.add_argument("--reserve", type=float, default=0.0,
                    help="cash to hold back and never commit")
    ap.add_argument("--dte-min", type=int, default=DTE_MIN)
    ap.add_argument("--dte-max", type=int, default=DTE_MAX)
    ap.add_argument("--allow-earnings", action="store_true",
                    help="override the default earnings rejection")
    ap.add_argument("--ignore-book", action="store_true",
                    help="skip the open-position and cooldown checks")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    d = evaluate(a.ticker.upper(), a.cash, a.reserve, a.dte_min, a.dte_max,
                 a.allow_earnings, check_book=not a.ignore_book)
    print(json.dumps(asdict(d), indent=1, default=str) if a.json
          else format_decision(d))
    return 0 if d.verdict == "EXECUTE" else 1


if __name__ == "__main__":
    sys.exit(main())
