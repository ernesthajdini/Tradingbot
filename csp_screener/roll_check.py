"""
ROLL EVALUATOR — the four choices for a challenged short put, priced.

The owner's spec is explicit and this module obeys it literally:

    "Do not assume that an ITM position must automatically be rolled."
    "Rolling does not erase losses."
    Any proposed roll must display: realised loss on the old contract,
    premium on the new, net debit/credit, new strike, new expiration,
    new assignment exposure, and CUMULATIVE P&L across the complete sequence.

So it presents A) hold, B) accept assignment, C) roll, D) close — each with
its own arithmetic — and recommends nothing. The decision is the owner's.

THE SEQUENCE IS THE UNIT. Pass --prior-pnl with everything already realised
on this chain of rolls, and every path is scored from the start of the
sequence rather than from the current leg. That is what makes a roll read
honestly: the 21 Aug roll was not a $1,498 win, it was -$469.54 followed by
+$1,498.46 = +$1,028.92 net.

Amendment 17 measured this decision over ~200 NVDA weekly sequences:
rolling FAILED validation and was WORSE than taking the loss
(-$474/sequence vs -$355). The numbers are printed on every run.

    python -m csp_screener.roll_check --ticker NVDA --strike 222.5 \
        --expiry 2026-09-11 --credit 1.36 --contracts 4 --prior-pnl 0
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from csp_screener.weekly_csp import (CONTRACT_MULTIPLIER, EVIDENCE,
                                     MIN_OPEN_INTEREST, fetch_weekly_chain)

logger = logging.getLogger(__name__)

COMMISSION = 1.00
MAX_ROLL_CANDIDATES = 4

ROLL_EVIDENCE = (
    "Amendment 17 (NVDA weeklies 2019-2023, ~200 sequences): rolling PASSED "
    "the search window (+$143/seq) and FAILED validation (-$474/seq vs -$355 "
    "for simply taking the loss). A net-credit roll is available after a "
    "sharp drop that reverses and unavailable during a sustained one — i.e. "
    "available when you do not need it.")


@dataclass
class Choice:
    label: str
    action: str
    realised_now: Optional[float] = None       # cash P&L if taken today
    sequence_pnl: Optional[float] = None       # incl. everything prior
    exposure: Optional[float] = None
    detail: str = ""
    notes: list = field(default_factory=list)


def _leg_pnl(credit, cost, contracts, legs_closed=2):
    """Cash P&L of closing one short put: credit taken minus cost to buy back,
    minus commission on both sides."""
    return ((credit - cost) * contracts * CONTRACT_MULTIPLIER
            - legs_closed * contracts * COMMISSION)


def buyback_quote(chains, expiry, strike):
    """Live ASK for the contract being closed — the price a buyer pays."""
    for exp, sides in chains.items():
        if exp.date() != expiry:
            continue
        for p in sides["puts"]:
            if abs(p.strike - strike) < 1e-6:
                return (p.bid, p.ask) if (p.bid > 0 and p.ask > 0) else None
    return None


def roll_candidates(chains, spot, buyback, current_expiry, limit=MAX_ROLL_CANDIDATES):
    """Next expiries, strikes below spot whose BID still exceeds the cost of
    closing the loser — that is what 'roll down and out for a credit' means."""
    out = []
    for exp in sorted(chains):
        if exp.date() <= current_expiry:
            continue
        for p in sorted(chains[exp]["puts"], key=lambda x: -x.strike):
            if (p.bid > buyback and p.strike < spot
                    and p.ask > 0 and p.open_interest >= MIN_OPEN_INTEREST):
                out.append((exp, p))
        if out:
            break                      # nearest expiry that offers a credit
    return out[:limit]


def evaluate_roll(ticker, strike, expiry, credit, contracts=1, prior_pnl=0.0,
                  buyback_override=None, cash=None):
    spot, chains = fetch_weekly_chain(ticker, dte_min=0, dte_max=45)
    out = {"ticker": ticker, "spot": spot, "strike": strike,
           "expiry": expiry.isoformat(), "credit": credit,
           "contracts": contracts, "prior_pnl": round(prior_pnl, 2),
           "choices": [], "error": None}
    if not spot:
        out["error"] = "no live price for the underlying"
        return out

    q = buyback_quote(chains, expiry, strike)
    if buyback_override is not None:
        cost, src = buyback_override, "supplied by you"
    elif q:
        cost, src = q[1], f"live ask (bid {q[0]:.2f} / ask {q[1]:.2f})"
    else:
        intrinsic = max(strike - spot, 0.0)
        out["error"] = (
            f"no two-sided quote for the {strike:g} put expiring {expiry}. "
            f"Intrinsic value is ${intrinsic:.2f}/share, but that is NOT a "
            f"tradable price. Re-run during market hours, or pass "
            f"--buyback <price> with the live ask from your platform.")
        return out

    out["spot"] = round(spot, 2)
    out["buyback"] = round(cost, 2)
    out["buyback_source"] = src
    out["moneyness"] = ("IN the money" if spot < strike else "out of the money")
    out["distance_pct"] = round(100 * (spot - strike) / strike, 2)

    close_pnl = _leg_pnl(credit, cost, contracts)
    exposure = strike * contracts * CONTRACT_MULTIPLIER
    breakeven = strike - credit

    choices = []

    # ---- A. HOLD
    choices.append(Choice(
        "A. HOLD to expiry", "do nothing",
        realised_now=None,
        sequence_pnl=None,
        exposure=exposure,
        detail=(f"breakeven ${breakeven:.2f}; assigned below ${strike:g}. "
                f"Marked right now, closing would cost ${cost:.2f}/share."),
        notes=["No P&L is realised until expiry or you act.",
               f"If {ticker} finishes above ${strike:g} the sequence ends at "
               f"${prior_pnl + credit * contracts * CONTRACT_MULTIPLIER - contracts * COMMISSION:,.2f}."]))

    # ---- B. ACCEPT ASSIGNMENT
    assign_pnl = prior_pnl + (credit * contracts * CONTRACT_MULTIPLIER
                              - contracts * COMMISSION)
    shares = contracts * CONTRACT_MULTIPLIER
    unreal = (spot - breakeven) * shares
    choices.append(Choice(
        "B. ACCEPT ASSIGNMENT", f"buy {shares} shares at ${strike:g}",
        realised_now=round(credit * contracts * CONTRACT_MULTIPLIER
                           - contracts * COMMISSION, 2),
        sequence_pnl=round(assign_pnl, 2),
        exposure=exposure,
        detail=(f"cost basis ${breakeven:.2f}/share after the credit; "
                f"${exposure:,.0f} of cash converts to {shares} shares"),
        notes=[f"Marked at ${spot:.2f} the stock position is "
               f"${unreal:+,.2f} against that basis.",
               "You then hold the stock and its full downside."]
        + ([] if cash is None or exposure <= cash else
           [f"WARNING: needs ${exposure:,.0f} but only ${cash:,.0f} is available."])))

    # ---- C. ROLL  (the spec's mandatory disclosure)
    cands = roll_candidates(chains, spot, cost, expiry)
    if not cands:
        choices.append(Choice(
            "C. ROLL down and out", "NOT AVAILABLE",
            detail=("no later strike below spot pays more than the "
                    f"${cost:.2f}/share it costs to close this one — there is "
                    "no net-credit roll here"),
            notes=["This is the ordinary case during a sustained decline.",
                   "A roll that costs a net DEBIT adds risk and pays you "
                   "nothing to take it."]))
    else:
        for exp, p in cands:
            new_credit = p.bid
            net = (new_credit - cost) * contracts * CONTRACT_MULTIPLIER
            new_exposure = p.strike * contracts * CONTRACT_MULTIPLIER
            seq = prior_pnl + close_pnl          # realised so far in the chain
            choices.append(Choice(
                f"C. ROLL to {exp:%d%b%y} ${p.strike:g}",
                f"buy back ${cost:.2f}, sell ${new_credit:.2f}",
                realised_now=round(close_pnl, 2),
                sequence_pnl=round(seq, 2),
                exposure=new_exposure,
                detail=(f"net CREDIT ${net:,.2f} "
                        f"(${new_credit:.2f} - ${cost:.2f} per share x "
                        f"{contracts} x 100)"),
                notes=[
                    f"REALISED LOSS ON THE OLD CONTRACT: ${close_pnl:,.2f} — "
                    f"this is locked in and the roll does NOT erase it.",
                    f"Premium on the new contract: "
                    f"${new_credit * contracts * CONTRACT_MULTIPLIER:,.2f}",
                    f"New assignment exposure: ${new_exposure:,.0f} "
                    f"({'lower' if new_exposure < exposure else 'HIGHER'} "
                    f"than the ${exposure:,.0f} you have now)",
                    f"CUMULATIVE SEQUENCE P&L SO FAR: ${seq:,.2f} — the new "
                    f"position must earn more than this to make the whole "
                    f"sequence profitable.",
                ]
                + ([] if cash is None or new_exposure <= cash else
                   [f"WARNING: needs ${new_exposure:,.0f} but only "
                    f"${cash:,.0f} is available."])))

    # ---- D. CLOSE
    choices.append(Choice(
        "D. CLOSE and realise the loss", f"buy back at ${cost:.2f}",
        realised_now=round(close_pnl, 2),
        sequence_pnl=round(prior_pnl + close_pnl, 2),
        exposure=0.0,
        detail=f"ends the sequence; ${exposure:,.0f} of cash is released",
        notes=["No further exposure to this position.",
               "Amendment 17's control arm — and it beat every roll arm out "
               "of sample."]))

    out["choices"] = [asdict(c) for c in choices]
    return out


def format_report(r) -> str:
    L = ["=" * 72,
         f"  ROLL EVALUATION — {r['ticker']} ${r['strike']:g} put exp {r['expiry']}",
         "=" * 72]
    if r.get("error"):
        L += [f"  CANNOT EVALUATE: {r['error']}", "=" * 72]
        return "\n".join(L)
    L += [
        f"  Underlying now            ${r['spot']:,.2f}  ({r['moneyness']}, "
        f"{r['distance_pct']:+.2f}% vs strike)",
        f"  Contracts                 {r['contracts']}",
        f"  Credit originally taken   ${r['credit']:.2f}/share",
        f"  Cost to close now         ${r['buyback']:.2f}/share  [{r['buyback_source']}]",
        f"  Already realised in this sequence   ${r['prior_pnl']:,.2f}",
        "", "-" * 72, "  YOUR FOUR CHOICES — no recommendation is made", "-" * 72]
    for c in r["choices"]:
        L.append(f"\n  {c['label']}   ({c['action']})")
        if c["detail"]:
            L.append(f"      {c['detail']}")
        if c["realised_now"] is not None:
            L.append(f"      Realised if taken now      ${c['realised_now']:,.2f}")
        if c["sequence_pnl"] is not None:
            L.append(f"      CUMULATIVE SEQUENCE P&L    ${c['sequence_pnl']:,.2f}")
        if c["exposure"] is not None:
            L.append(f"      Assignment exposure        ${c['exposure']:,.0f}")
        for n in c["notes"]:
            L.append(f"      - {n}")
    L += ["", "-" * 72, f"  ! {ROLL_EVIDENCE}", "=" * 72]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Price the four choices for a challenged short put")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strike", type=float, required=True)
    ap.add_argument("--expiry", required=True, help="YYYY-MM-DD of the open put")
    ap.add_argument("--credit", type=float, required=True,
                    help="credit per share originally received")
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--prior-pnl", type=float, default=0.0,
                    help="cash already realised earlier in THIS roll sequence")
    ap.add_argument("--buyback", type=float, default=None,
                    help="live ask to close, if the market is shut")
    ap.add_argument("--cash", type=float, default=None,
                    help="available cash, to flag exposure that exceeds it")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        exp = datetime.strptime(a.expiry, "%Y-%m-%d").date()
    except ValueError:
        print(f"--expiry must be YYYY-MM-DD, got {a.expiry!r}")
        return 2
    r = evaluate_roll(a.ticker.upper(), a.strike, exp, a.credit, a.contracts,
                      a.prior_pnl, a.buyback, a.cash)
    print(json.dumps(r, indent=1, default=str) if a.json else format_report(r))
    return 1 if r.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
