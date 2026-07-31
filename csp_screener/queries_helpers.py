"""
Small read-side helpers shared by the gate and the reporting surfaces.

Kept separate from evaluator.py so the go-live gate can ask narrow
questions ("how much of the evidence is real?") without widening the
evaluator's API.
"""

from __future__ import annotations


def market_marked_share() -> float:
    """
    Fraction of closed virtual trades whose exit was priced off a REAL
    option quote rather than the Black-Scholes fallback.

    Why the gate cares: the model admits in its own comments that it
    "prices profits too early". Evidence built on model marks can look
    profitable while the market would never have filled it, so a paper
    record dominated by model marks is not evidence at all.

    Provenance is recorded in the close note ("…, mark_source=market|model,
    …") — see virtual_tracker.close_virtual_position.
    """
    from csp_screener.evaluator import _reconstruct_closed_trades

    closed = _reconstruct_closed_trades()
    if not closed:
        return 0.0
    market = sum(
        1 for t in closed
        if "mark_source=market" in str(t.get("notes") or "")
    )
    return market / len(closed)
