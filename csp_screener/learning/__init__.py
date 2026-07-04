"""
Self-learning layer for the CSP screener.

DESIGN PHILOSOPHY: This module never mutates config.py automatically. It
observes outcomes, surfaces patterns, and rank-orders future candidates
based on what worked. Threshold changes still require deliberate user
action (with the 14-day cooldown).

Why not auto-tune? With ~5-10 trades per week, the system can build a
~50-trade sample in 2-3 months. That's the bare minimum to spot real
patterns. Acting on noise is the more dangerous failure mode for a
small-account trader. We surface; you decide.

Modules:
  ticker_scorer:      rolling per-ticker win rate (deprioritize chronic losers)
  feature_analyzer:   win rate by delta band, DTE band, RV band, VIX regime
  recommender:        plain-English suggestions from analyzer output
  confidence:         composite per-candidate confidence (used by ranker)
"""
