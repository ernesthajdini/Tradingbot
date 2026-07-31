"""
Account facts and equity-scaled risk caps.

WHY THIS FILE EXISTS (2026-07-30): the system was displaying "$X of $200
budget" in a way that read as if $200 were the whole account. It is not:
$200 is the MONTHLY DEPOSIT. The account starts at $1,200.

Worse, the absolute caps were sized for a FUTURE balance. config.py says
    MAX_RISK_PER_SPREAD = 130.0  # ~5% of a $2.6K account; playbook M7 sizing
$2.6K is the projected January-2027 balance after a year of deposits. On
today's $1,200 that same $130 is 10.8% of the account — more than double
the playbook's stated intent, and the playbook is explicit that
"Concentration caps are percentages, invariant to balance" (Section 4).

So the effective cap is the STRICTER of:
  - the absolute dollar cap in config.py (the future-state ceiling), and
  - the playbook's percentage of TODAY's equity.
This can only ever tighten risk relative to config.py, never loosen it —
which keeps it playbook-legal and outside the anti-tinker cooldown that
governs config.py's tunable thresholds. As deposits grow equity, the caps
rise on their own toward the ceilings.

UPDATING EQUITY: set CURRENT_EQUITY when a deposit lands or the balance
moves materially. It is deliberately NOT auto-inflated by assumed deposits
— an assumed deposit that never happened would silently raise your risk.
"""

from __future__ import annotations

from csp_screener import config

# ---------------------------------------------------------------------------
# Facts about the account (update CURRENT_EQUITY as reality changes)
# ---------------------------------------------------------------------------
ACCOUNT_START_CAPITAL: float = 1_200.0   # starting balance
MONTHLY_DEPOSIT: float = 200.0           # the deposits — NOT a risk budget
CURRENT_EQUITY: float = 1_200.0          # update on deposit / material move

# ---------------------------------------------------------------------------
# Playbook percentages (Section 4 / M7 / M11) — the real caps
# ---------------------------------------------------------------------------
# M7: "max risk ≤$130 (~5% of account, quarter-to-half Kelly)"
MAX_RISK_PCT_PER_TRADE: float = 0.05
# M11: "2 concurrent spreads, total open risk ≤$250 (~8%)"
MAX_TOTAL_RISK_PCT: float = 0.08


def current_equity() -> float:
    return CURRENT_EQUITY


def max_risk_per_trade() -> float:
    """Stricter of the absolute ceiling and 5% of today's equity."""
    return min(config.MAX_RISK_PER_SPREAD,
               MAX_RISK_PCT_PER_TRADE * current_equity())


def max_total_risk() -> float:
    """Stricter of the absolute ceiling and 8% of today's equity."""
    return min(config.MAX_TOTAL_PREMIUM_AT_RISK,
               MAX_TOTAL_RISK_PCT * current_equity())


def risk_summary() -> dict:
    """Everything a display needs, so no surface invents its own numbers."""
    eq = current_equity()
    per_trade = max_risk_per_trade()
    total = max_total_risk()
    return {
        "equity": eq,
        "monthly_deposit": MONTHLY_DEPOSIT,
        "max_risk_per_trade": per_trade,
        "max_total_risk": total,
        "max_positions": config.MAX_OPEN_POSITIONS,
        "per_trade_pct_of_equity": per_trade / eq if eq else 0.0,
        "total_pct_of_equity": total / eq if eq else 0.0,
        # True while equity is below the balance the absolute caps assume —
        # i.e. the percentage rule is the binding constraint, not config.py.
        "pct_rule_binding": per_trade < config.MAX_RISK_PER_SPREAD,
    }
