# Backtest Pre-Registration Manifest — FROZEN BEFORE DATA

**Committed 2026-08-07, before any historical options data was downloaded.**
This file is the overfitting defense the LEARNING_ACCELERATION_STUDY red-team
made a hard condition: config.py has ~15 tunable knobs and a research loop can
re-run any of them in seconds against the exact regimes (post-spike!) we would
be tempted to condition on. Without pre-registration, the backtest harness is
a structural bypass of the 14-day anti-tinker cooldown. With it, every result
is interpretable.

**Amending this manifest after data has been downloaded requires the same
discipline as a config.py change: a written rationale in this file's history
and a fresh Bonferroni denominator. Silent edits void every result produced
after the edit.**

---

## 1. Declared knobs (the ONLY parameters allowed to vary)

| # | Knob | Values under study | Production value |
|---|------|--------------------|------------------|
| 1 | DTE window | {25–45, 30–45} | 25–45 |
| 2 | Short-leg target delta | {0.20, 0.25, 0.30} | 0.30 |
| 3 | Spread-width policy | {production ($1 ≤ $30 spot, else $2)} — *declared for a later amendment; NOT yet wired, engine rejects any other value* | production |
| 4 | Sandbox credit floor (net at designed exit) | {production} — *same status as #3* | > $0 |

**Grid as wired today: 2 × 3 × 1 × 1 = 6 cells.** If knobs 3–4 are ever
wired, the full declared grid is 2 × 3 × 2 × 2 = 24 cells and the correction
denominator grows accordingly *from that run onward*.

Everything else — exit rules (50% TP / 21-DTE / −2× stop), friction model,
sanity caps, zombie defenses, OI floor, spread gates, VIX kill switch,
universe bands, ranker — is FROZEN at the production values in the commit
that introduced this file. The engine snapshots and hashes those values into
every run log so drift is detectable.

## 2. Time discipline

- **Exploration:** 2016-01-01 → 2023-12-31 (includes Mar-2020 crash and the
  2022 bear — the two regimes the data purchase exists to capture).
- **Validation:** 2024-01-01 → 2024-12-31 (touched only to confirm
  exploration-period conclusions, never to search).
- **SEALED TEST: 2025-01-01 onward. Opened ONCE, for the final
  pre-registered configuration only.** The engine refuses sealed dates
  unless explicitly unlocked, and the unlock is logged.

## 3. Multiplicity accounting

- Every engine run appends one line to `runs_log.jsonl` (committed to git).
  **The line count of that file is the denominator.** A run that crashed
  still counts — it was still a look at the data.
- Cell comparisons use paired same-day contrasts (both arms screen the same
  dates), which cancels the shared market factor.
- Significance threshold: α = 0.05 / (number of declared cells), i.e.
  0.05/6 ≈ 0.0083 as wired today.

## 4. Honesty rails (inherited from the playbook, enforced in code)

- All P&L reported as a **[pessimistic, base] band** — backtest fills are as
  slippage-blind as paper fills. A backtest can NEVER prove positive live
  expectancy; it calibrates knobs and falsifies configurations.
- **No backtest row ever touches `virtual_trades`, `shadow_trades`, the
  screens journal, Supabase, or golive.py's counters.** The engine has no
  code path to any of them (it never imports `journal`).
- **EARLIEST_LIVE_DATE and the 200-trade gate are forward-operational and
  cannot be backfilled with simulated history.**
- **Survivorship:** results computed on a universe that excludes delisted
  names MUST be labeled `survivorship: biased` (the engine stamps this from
  the loader's metadata) and are directional-only — short-put left tails
  live exactly in the names that died.
- **Earnings gate:** without a historical earnings calendar the blackout
  filter cannot replay; such runs are stamped `earnings_gate: unavailable`
  and cannot be used to tune anything earnings-adjacent.

## 5. What a "kill" result means

If the production configuration shows **negative expectancy at mid-fill
under the pessimistic band across the exploration window**, the strategy is
falsified: stop the 2026 paper year, do not spend further on data, report to
the owner. That outcome is a SUCCESS of this project, not a failure.
