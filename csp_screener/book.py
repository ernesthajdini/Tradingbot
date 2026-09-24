"""
POSITION BOOK — the owner's own live weekly-CSP positions.

Append-only, replayed to state. A roll does not close a sequence; it adds a
leg to it. Sequence P&L is the sum of every leg, which is the only honest way
to score a roll chain and the thing his own spec demanded:

    21 Aug: 217.5P closed at a loss      -$469.54   <- leg 1
            210P sold in the same second
    27 Aug: 210P closed                 +$1,498.46  <- leg 2
            SEQUENCE                    +$1,028.92  <- what actually happened

SEPARATE FROM THE PAPER RECORD. This writes to the `weekly_book` topic. The
go-live gate and the evaluator read `virtual_trades` and never this, so a
real trade can never be mistaken for paper evidence or the reverse.

    python -m csp_screener.book open  --ticker NVDA --strike 215 \
        --expiry 2026-10-01 --credit 1.24 --contracts 4
    python -m csp_screener.book roll  --seq NVDA-20261001-215 --buyback 2.23 \
        --to-strike 210 --to-expiry 2026-10-08 --credit 4.02
    python -m csp_screener.book close --seq NVDA-20261001-215 --buyback 0.26
    python -m csp_screener.book status
    python -m csp_screener.book history --closed
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from csp_screener import journal

logger = logging.getLogger(__name__)

TOPIC = "weekly_book"
COMMISSION = 1.00
MULT = 100
REOPEN_COOLDOWN_DAYS = 3


# ---------------------------------------------------------------------------
# P&L — one formula, used everywhere
# ---------------------------------------------------------------------------

def leg_pnl(credit: float, cost: float, contracts: int,
            expired_worthless: bool = False) -> float:
    """Cash P&L of one short-put leg from open to close.

    expired_worthless: no buyback ticket, so only the opening commission is
    charged. Everything else pays both sides.
    """
    legs = 1 if expired_worthless else 2
    return ((credit - cost) * contracts * MULT) - (legs * contracts * COMMISSION)


def seq_id(ticker: str, expiration: str, strike: float) -> str:
    return f"{ticker}-{expiration.replace('-', '')}-{strike:g}"


# ---------------------------------------------------------------------------
# State by replay
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    strike: float
    expiration: str
    credit: float
    opened_at: str
    closed_at: Optional[str] = None
    buyback: Optional[float] = None
    expired_worthless: bool = False

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def pnl(self) -> Optional[float]:
        if self.is_open:
            return None
        return round(leg_pnl(self.credit, self.buyback or 0.0, self.contracts,
                             self.expired_worthless), 2)
    contracts: int = 1


@dataclass
class Sequence:
    seq: str
    ticker: str
    contracts: int
    legs: list = field(default_factory=list)
    status: str = "open"
    outcome: Optional[str] = None

    @property
    def current(self) -> Optional[Leg]:
        return next((l for l in self.legs if l.is_open), None)

    @property
    def realised(self) -> float:
        return round(sum(l.pnl or 0.0 for l in self.legs if not l.is_open), 2)

    @property
    def rolls(self) -> int:
        return max(0, len(self.legs) - 1)

    @property
    def exposure(self) -> float:
        c = self.current
        return round(c.strike * self.contracts * MULT, 2) if c else 0.0


def replay() -> dict:
    """{seq_id: Sequence} from the append-only log."""
    seqs: dict = {}
    for ev in journal.read_all(TOPIC):
        sid, kind = ev.get("seq"), ev.get("event")
        if not sid or not kind:
            continue
        if kind == "open":
            s = Sequence(sid, ev["ticker"], int(ev["contracts"]))
            s.legs.append(Leg(float(ev["strike"]), ev["expiration"],
                              float(ev["credit"]), ev["at"],
                              contracts=int(ev["contracts"])))
            seqs[sid] = s
        elif sid in seqs:
            s = seqs[sid]
            cur = s.current
            if kind == "roll" and cur:
                cur.closed_at = ev["at"]
                cur.buyback = float(ev["buyback"])
                s.legs.append(Leg(float(ev["to_strike"]), ev["to_expiration"],
                                  float(ev["credit"]), ev["at"],
                                  contracts=s.contracts))
            elif kind == "close" and cur:
                cur.closed_at = ev["at"]
                cur.buyback = float(ev.get("buyback") or 0.0)
                cur.expired_worthless = bool(ev.get("expired_worthless"))
                s.status = "closed"
                s.outcome = ev.get("outcome") or "closed"
    return seqs


def open_sequences() -> dict:
    return {k: v for k, v in replay().items() if v.status == "open"}


def has_open(ticker: str) -> bool:
    return any(s.ticker == ticker.upper() for s in open_sequences().values())


def in_cooldown(ticker: str, days: int = REOPEN_COOLDOWN_DAYS) -> Optional[str]:
    """Most recent close date for the ticker if it is still inside the
    reopen cooldown, else None. Stops the open/close/reopen churn that cost
    the paper book friction on every lap."""
    cutoff = datetime.now() - timedelta(days=days)
    latest = None
    for s in replay().values():
        if s.ticker != ticker.upper() or s.status != "closed":
            continue
        ends = [l.closed_at for l in s.legs if l.closed_at]
        if not ends:
            continue
        when = max(datetime.fromisoformat(e) for e in ends)
        if when >= cutoff and (latest is None or when > latest):
            latest = when
    return latest.strftime("%Y-%m-%d") if latest else None


# ---------------------------------------------------------------------------
# Writers — append only, never edit
# ---------------------------------------------------------------------------

def record_open(ticker, strike, expiration, credit, contracts, note=""):
    ticker = ticker.upper()
    sid = seq_id(ticker, expiration, strike)
    if sid in replay():
        raise ValueError(f"sequence {sid} already exists — roll or close it")
    if has_open(ticker):
        raise ValueError(f"{ticker} already has an open sequence; "
                         f"one per ticker")
    return journal.append(TOPIC, {
        "event": "open", "seq": sid, "ticker": ticker, "strike": float(strike),
        "expiration": expiration, "credit": float(credit),
        "contracts": int(contracts), "at": datetime.now().isoformat(),
        "note": note})


def record_roll(sid, buyback, to_strike, to_expiration, credit, note=""):
    s = replay().get(sid)
    if not s or s.status != "open" or not s.current:
        raise ValueError(f"no open sequence {sid}")
    return journal.append(TOPIC, {
        "event": "roll", "seq": sid, "ticker": s.ticker,
        "from_strike": s.current.strike, "from_expiration": s.current.expiration,
        "buyback": float(buyback), "to_strike": float(to_strike),
        "to_expiration": to_expiration, "credit": float(credit),
        "contracts": s.contracts, "at": datetime.now().isoformat(),
        "note": note})


def record_close(sid, buyback=0.0, expired_worthless=False, outcome="",
                 note=""):
    s = replay().get(sid)
    if not s or s.status != "open" or not s.current:
        raise ValueError(f"no open sequence {sid}")
    return journal.append(TOPIC, {
        "event": "close", "seq": sid, "ticker": s.ticker,
        "strike": s.current.strike, "expiration": s.current.expiration,
        "buyback": float(buyback), "expired_worthless": bool(expired_worthless),
        "outcome": outcome or ("expired worthless" if expired_worthless
                               else "bought back"),
        "at": datetime.now().isoformat(), "note": note})


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_status() -> str:
    seqs = replay()
    op = {k: v for k, v in seqs.items() if v.status == "open"}
    cl = {k: v for k, v in seqs.items() if v.status == "closed"}
    L = ["=" * 72, "  POSITION BOOK", "=" * 72]
    if not op:
        L.append("  No open sequences.")
    for s in op.values():
        c = s.current
        dte = (date.fromisoformat(c.expiration) - date.today()).days
        L += [f"\n  {s.seq}   [{s.ticker}]",
              f"    open leg      {s.contracts}x ${c.strike:g} put exp "
              f"{c.expiration}  ({dte}d)  credit ${c.credit:.2f}",
              f"    rolls so far  {s.rolls}",
              f"    realised      ${s.realised:,.2f}   <- carried into this leg",
              f"    exposure      ${s.exposure:,.0f} if assigned"]
        if s.rolls:
            L.append(f"    the sequence must beat ${-s.realised:,.2f} on this "
                     f"leg just to break even")
    if cl:
        tot = sum(s.realised for s in cl.values())
        wins = sum(1 for s in cl.values() if s.realised > 0)
        L += ["", "-" * 72,
              f"  CLOSED: {len(cl)} sequences, {wins} profitable, "
              f"total ${tot:,.2f}"]
        rolled = [s for s in cl.values() if s.rolls]
        if rolled:
            rt = sum(s.realised for s in rolled)
            L.append(f"  of which {len(rolled)} involved a roll, totalling "
                     f"${rt:,.2f}")
    L.append("=" * 72)
    return "\n".join(L)


def format_history(closed_only=False) -> str:
    L = ["=" * 72, "  SEQUENCE HISTORY", "=" * 72]
    for s in replay().values():
        if closed_only and s.status != "closed":
            continue
        L.append(f"\n  {s.seq}  [{s.status}]  {s.contracts} contracts")
        for i, l in enumerate(s.legs, 1):
            if l.is_open:
                L.append(f"    leg {i}  ${l.strike:g} exp {l.expiration}  "
                         f"credit ${l.credit:.2f}  -> OPEN")
            else:
                how = ("expired worthless" if l.expired_worthless
                       else f"bought back ${l.buyback:.2f}")
                L.append(f"    leg {i}  ${l.strike:g} exp {l.expiration}  "
                         f"credit ${l.credit:.2f}  {how}  "
                         f"-> ${l.pnl:,.2f}")
        L.append(f"    SEQUENCE P&L  ${s.realised:,.2f}"
                 + ("" if s.status == "closed" else "  (still open)"))
    L.append("=" * 72)
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live weekly-CSP position book")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="record a new short put")
    o.add_argument("--ticker", required=True)
    o.add_argument("--strike", type=float, required=True)
    o.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    o.add_argument("--credit", type=float, required=True, help="per share")
    o.add_argument("--contracts", type=int, default=1)
    o.add_argument("--note", default="")

    r = sub.add_parser("roll", help="close the open leg and open a new one")
    r.add_argument("--seq", required=True)
    r.add_argument("--buyback", type=float, required=True, help="per share paid")
    r.add_argument("--to-strike", type=float, required=True)
    r.add_argument("--to-expiry", required=True, help="YYYY-MM-DD")
    r.add_argument("--credit", type=float, required=True, help="per share")
    r.add_argument("--note", default="")

    c = sub.add_parser("close", help="end the sequence")
    c.add_argument("--seq", required=True)
    c.add_argument("--buyback", type=float, default=0.0, help="per share paid")
    c.add_argument("--expired", action="store_true",
                   help="expired worthless; no buyback ticket")
    c.add_argument("--note", default="")

    sub.add_parser("status", help="open sequences and the closed summary")
    h = sub.add_parser("history", help="every leg of every sequence")
    h.add_argument("--closed", action="store_true")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        if a.cmd == "open":
            ev = record_open(a.ticker, a.strike, a.expiry, a.credit,
                             a.contracts, a.note)
            print(f"recorded: {ev['seq']}")
        elif a.cmd == "roll":
            ev = record_roll(a.seq, a.buyback, a.to_strike, a.to_expiry,
                             a.credit, a.note)
            s = replay()[a.seq]
            print(f"rolled {a.seq}: realised so far ${s.realised:,.2f}; "
                  f"this leg must beat ${-s.realised:,.2f} to break even")
        elif a.cmd == "close":
            record_close(a.seq, a.buyback, a.expired, note=a.note)
            s = replay()[a.seq]
            print(f"closed {a.seq}: SEQUENCE P&L ${s.realised:,.2f} "
                  f"over {len(s.legs)} leg(s)")
        elif a.cmd == "status":
            print(format_status())
        elif a.cmd == "history":
            print(format_history(a.closed))
    except ValueError as e:
        print(f"refused: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
