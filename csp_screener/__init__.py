"""
CSP Screener — Sunday-night cash-secured put candidate screener.

Generates a weekly email with 5-10 underlying candidates suitable for selling
cash-secured puts. Tracks virtual outcomes of every suggestion to build a real
performance record without risking capital.

Design principles (locked):
  1. Email + journal only. No dashboard, no real-time alerts, no auto-execution.
  2. Underlying candidates, not contract picks. User chooses the contract.
  3. Self-evaluating: every suggestion is tracked as a virtual trade.
  4. Hard rules in config.py, locked by 14-day cooldown.
  5. Deadman switch: silence means broken.
"""
