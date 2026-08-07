"""
Walk-forward backtest harness (LEARNING_ACCELERATION_STUDY.md, "the big
prize"): replays the PRODUCTION gate code — filters, ranker, setup
generators, sanity caps, friction model, exit rules — over historical EOD
option chains, so the backtest tests the system that actually runs, not a
reimplementation of it.

READ MANIFEST.md BEFORE RUNNING ANYTHING. The manifest pre-registers the
only knobs allowed to move, the sealed test period, and the multiplicity
accounting. Every run is logged to runs_log.jsonl — that file's line count
is the honesty denominator.

Isolation guarantee: nothing in this package imports csp_screener.journal
or supabase_sync. Backtest output goes to backtest/results/ (gitignored);
it can never touch the production journals or the go-live gate.
"""
