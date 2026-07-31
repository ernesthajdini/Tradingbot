"""
The go-live gate, in code.

Until now this existed only in prose: OPTIONS_PROFIT_PLAYBOOK.md forbids
real money before 200+ paper trades and January 2027, but nothing in the
system enforced it — and the most persuasive email it sends was headed
"Real-money candidates (staged tickets)" with a subject reading
"TAKE {ticker}". The machine was inviting the exact action the plan
prohibits, and January would have been decided by feel.

DESIGN: this gate NEVER hides a signal (signal quality is the product —
see account.py's sizing split). It changes what the system CALLS a signal.
Gate open  -> "staged ticket, approve or reject in IBKR".
Gate shut  -> "research signal, not approved for real money", with the
              exact blockers and the distance to each.

Every threshold below comes from the playbook, not from taste.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Playbook M5-M6 / M7: the gate's own numbers.
MIN_CLOSED_PAPER_TRADES = 200      # "200+ pooled paper trades"
EARLIEST_LIVE_DATE = date(2027, 1, 1)   # M7 — "first live trade if gated in"
# Process metrics (M6): evidence must be honest, not just plentiful.
MIN_MARKET_MARKED_SHARE = 0.50     # half the closes priced off real quotes
# "P&L not materially negative under the pessimistic friction band"
MAX_PESSIMISTIC_LOSS_PER_TRADE = 0.0


def _pct(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def gate_status(today: Optional[date] = None) -> dict:
    """
    Evaluate every go-live condition. Returns a dict the email, dashboard
    and ticket text all read, so no surface can imply a different verdict.
    """
    today = today or date.today()
    from csp_screener import evaluator
    from csp_screener.queries_helpers import market_marked_share

    summary = evaluator.compute_summary()  # signal scoreboard (all trades)
    closed = summary.closed_count
    pessimistic_per_trade = (
        summary.total_pnl_pessimistic / closed if closed else 0.0)
    marked_share = market_marked_share()

    checks = [
        {
            "key": "sample",
            "label": f"{closed} of {MIN_CLOSED_PAPER_TRADES} closed paper trades",
            "passed": closed >= MIN_CLOSED_PAPER_TRADES,
            "detail": (f"{MIN_CLOSED_PAPER_TRADES - closed} more needed"
                       if closed < MIN_CLOSED_PAPER_TRADES else "met"),
        },
        {
            "key": "date",
            "label": f"Not before {EARLIEST_LIVE_DATE.isoformat()}",
            "passed": today >= EARLIEST_LIVE_DATE,
            "detail": (f"{(EARLIEST_LIVE_DATE - today).days} days away"
                       if today < EARLIEST_LIVE_DATE else "reached"),
        },
        {
            "key": "expectancy",
            "label": "Not materially negative at pessimistic fills",
            "passed": (closed == 0
                       or pessimistic_per_trade >= MAX_PESSIMISTIC_LOSS_PER_TRADE),
            "detail": (f"${pessimistic_per_trade:+.2f}/trade at the pessimistic band"
                       if closed else "no closed trades yet"),
        },
        {
            "key": "evidence_quality",
            "label": f"At least {MIN_MARKET_MARKED_SHARE:.0%} of closes marked on real quotes",
            "passed": marked_share >= MIN_MARKET_MARKED_SHARE,
            "detail": f"{marked_share:.0%} market-marked",
        },
    ]
    blockers = [c["label"] + f" — {c['detail']}" for c in checks if not c["passed"]]
    return {
        "passed": not blockers,
        "checks": checks,
        "blockers": blockers,
        "closed_trades": closed,
        "required_trades": MIN_CLOSED_PAPER_TRADES,
        "progress_pct": min(1.0, _pct(closed, MIN_CLOSED_PAPER_TRADES)),
        "earliest_date": EARLIEST_LIVE_DATE.isoformat(),
        "days_to_earliest": max(0, (EARLIEST_LIVE_DATE - today).days),
        "market_marked_share": marked_share,
        "pessimistic_per_trade": pessimistic_per_trade,
    }


def is_open(today: Optional[date] = None) -> bool:
    try:
        return gate_status(today)["passed"]
    except Exception as e:  # never let a gate error imply "go"
        logger.warning(f"go-live gate check failed ({e}) — treating as SHUT")
        return False


def headline(status: dict) -> str:
    """One line for a human, matching the gate's actual state."""
    if status["passed"]:
        return "✅ Go-live gate PASSED — staged tickets are approvable."
    return (f"🔒 Go-live gate SHUT — research signals only "
            f"({status['closed_trades']}/{status['required_trades']} paper "
            f"trades, opens {status['earliest_date']}).")
