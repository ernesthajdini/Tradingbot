"""
Computes screener performance metrics from the virtual_trades journal.

These metrics get included in every weekly email so you can see — concretely
and numerically — whether the screener has shown edge yet.

Key honest metrics:
  - virtual_win_rate: fraction of closed virtual trades with PnL > 0
  - avg_pnl_per_trade: dollar avg
  - profit_factor: gross profit / gross loss (ratio > 1 = positive expectancy)
  - expectancy_per_trade: win_rate * avg_win + loss_rate * avg_loss
  - by_exit_reason: breakdown of how trades exited
  - by_ticker: which underlying contributed most
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from csp_screener import journal


@dataclass
class PerformanceSummary:
    period_label: str
    closed_count: int = 0
    open_count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    by_exit_reason: dict = field(default_factory=dict)
    by_ticker: dict = field(default_factory=dict)
    open_unrealized_pnl: float = 0.0  # only set when called with open mark-to-market

    def as_dict(self) -> dict:
        return {
            "period": self.period_label,
            "closed_count": self.closed_count,
            "open_count": self.open_count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": round(self.avg_pnl, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy": round(self.expectancy, 4),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "by_exit_reason": self.by_exit_reason,
            "by_ticker": self.by_ticker,
            "open_unrealized_pnl": round(self.open_unrealized_pnl, 2),
        }


def _reconstruct_closed_trades() -> list[dict]:
    """
    Replay the event log and return closed virtual trades joined with their
    open events. Each returned dict has keys from both events.
    """
    events = journal.read_all("virtual_trades")
    opens = {}
    closes = {}
    for ev in events:
        tid = ev.get("trade_id")
        if not tid:
            continue
        if ev.get("event") == "open":
            opens[tid] = ev
        elif ev.get("event") == "close":
            closes[tid] = ev

    out = []
    for tid, close_ev in closes.items():
        open_ev = opens.get(tid)
        if not open_ev:
            continue  # orphan close, skip
        merged = {**open_ev, **close_ev}  # close fields win
        merged["trade_id"] = tid
        out.append(merged)
    return out


def _filter_by_period(trades: list[dict], days: Optional[int]) -> list[dict]:
    if not days:
        return trades
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for t in trades:
        closed_at = t.get("closed_at")
        if not closed_at:
            continue
        try:
            # Normalize tz-aware timestamps (Supabase-hydrated records carry
            # offsets) to naive — comparing aware vs naive raises TypeError
            # and silently emptied summaries.
            ts = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
            ts = ts.replace(tzinfo=None)
            if ts >= cutoff:
                out.append(t)
        except Exception:
            continue
    return out


def compute_summary(period_days: Optional[int] = None, period_label: str = "") -> PerformanceSummary:
    """Compute summary metrics for closed virtual trades in the given period."""
    all_closed = _reconstruct_closed_trades()
    closed = _filter_by_period(all_closed, period_days)
    closed_ids = {c["trade_id"] for c in all_closed}
    open_count = len([t for t in journal.read_all("virtual_trades")
                      if t.get("event") == "open"
                      and t["trade_id"] not in closed_ids])

    if not period_label:
        period_label = f"last {period_days}d" if period_days else "all-time"

    summary = PerformanceSummary(period_label=period_label)
    summary.open_count = open_count
    if not closed:
        return summary

    pnls = [float(t.get("pnl", 0) or 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    summary.closed_count = len(closed)
    summary.wins = len(wins)
    summary.losses = len(losses)
    summary.win_rate = len(wins) / len(closed)
    summary.total_pnl = sum(pnls)
    summary.avg_pnl = float(np.mean(pnls)) if pnls else 0.0
    summary.avg_win = float(np.mean(wins)) if wins else 0.0
    summary.avg_loss = float(np.mean(losses)) if losses else 0.0
    gross_profit = sum(w for w in wins)
    gross_loss = abs(sum(l for l in losses))
    summary.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    summary.expectancy = (
        summary.win_rate * summary.avg_win
        + (1 - summary.win_rate) * summary.avg_loss
    )
    summary.best_trade = max(pnls)
    summary.worst_trade = min(pnls)

    # By exit reason
    by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in closed:
        r = t.get("exit_reason", "unknown")
        by_reason[r]["count"] += 1
        by_reason[r]["pnl"] += float(t.get("pnl", 0) or 0)
    summary.by_exit_reason = {
        k: {"count": v["count"], "pnl": round(v["pnl"], 2)}
        for k, v in by_reason.items()
    }

    # By ticker
    by_ticker = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in closed:
        tk = t.get("ticker", "?")
        by_ticker[tk]["count"] += 1
        by_ticker[tk]["pnl"] += float(t.get("pnl", 0) or 0)
        if (t.get("pnl") or 0) > 0:
            by_ticker[tk]["wins"] += 1
    summary.by_ticker = {
        k: {
            "trades": v["count"],
            "wins": v["wins"],
            "win_rate": round(v["wins"] / v["count"], 3) if v["count"] else 0.0,
            "pnl": round(v["pnl"], 2),
        }
        for k, v in by_ticker.items()
    }

    return summary


def all_periods_summary() -> dict[str, PerformanceSummary]:
    """Compute summaries for several periods for the email."""
    return {
        "30d": compute_summary(30, "last 30 days"),
        "90d": compute_summary(90, "last 90 days"),
        "all": compute_summary(None, "all-time"),
    }
