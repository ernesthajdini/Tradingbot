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
- **Fill-convention divergence (declared 2026-08-10):** the engine fills
  exits at the crossing day's EOD historical quote; the live paper record
  fills at the NEXT market-open run (market-hours exit execution), so a
  marginal crossing that recedes overnight never fills live. The backtest
  therefore realizes take-profits slightly earlier/more often than forward
  paper. Any backtest-vs-paper comparison must name this convention gap
  before attributing the difference to strategy decay.

## 5. What a "kill" result means

If the production configuration shows **negative expectancy at mid-fill
under the pessimistic band across the exploration window**, the strategy is
falsified: stop the 2026 paper year, do not spend further on data, report to
the owner. That outcome is a SUCCESS of this project, not a failure.

---

# AMENDMENT 1 — Phase 2 exploratory search (declared 2026-08-28, BEFORE running)

Phase 1 (the DTE x delta grid) is complete and its verdict is recorded in
STUDY_VERDICT: the production configuration's central estimate is negative
(-$6 to -$14/trade across corrected variants) but not statistically separable
from zero, on 119 trades whose sign rests on 4-9 tail events.

The owner has asked for a genuine search for a profitable configuration over
the purchased 8 years. That is a legitimate research goal and a multiplicity
hazard, so the search space, the splits and the correction are fixed HERE,
before any Phase 2 run executes. Phase 1's failure mode is explicitly on the
record as the reason: the study window was moved from 2016 to 2017 AFTER the
data was seen, and that undeclared choice manufactured the only "significant"
result in the whole study. It must not happen twice.

## A. Declared search space (the ONLY configurations Phase 2 may test)

| knob | values | count |
|---|---|---|
| universe | single_name, index_etf | 2 |
| structure | csp (naked), spread (defined risk) | 2 |
| DTE window | 25-45, 30-45 | 2 |
| target delta | 0.30, 0.25, 0.20, 0.15 | 4 |
| force-exit DTE | 21 (production), 7, 0 (hold to expiry) | 3 |
| stop multiple | 2.0 (production), 3.0, none | 3 |

Full grid = 2 x 2 x 2 x 4 x 3 x 3 = **288 configurations**. Not every cell is
runnable (index_etf + csp is unaffordable at this account size and will be
reported as skipped, not silently dropped); the multiplicity denominator is
the number of configurations ACTUALLY RUN, counted from runs_log.jsonl.

Everything else stays frozen at production values: take-profit 50%, friction
model, sanity caps, OI and spread gates, VIX kill switch, ranker, reopen
cooldown, position caps.

## B. Time discipline (fixed now, never moved again)

- **TRAIN 2017-02-08 -> 2021-12-31.** The entire search happens here. Any
  number quoted from this period is a search result, not evidence.
- **VALIDATE 2022-01-01 -> 2023-12-31.** Touched ONLY by configurations that
  survive the train-stage filter below, once each.
- **SEALED 2024-01-01 onward.** Untouched. Reserved for a single final
  confirmation of at most ONE configuration, if anything survives validation.

## C. Promotion rule (fixed now)

A configuration is promoted from TRAIN to VALIDATE only if ALL hold:
1. >= 100 closed trades on train (below that the estimate is noise);
2. positive mean P&L at the PESSIMISTIC friction band (not the base band —
   the optimistic band has never been the honest one);
3. positive median trade;
4. no single trade contributing more than 40% of total P&L.

A configuration is called a FINDING only if, on VALIDATE, it keeps a positive
pessimistic-band mean and its bootstrap 95% CI excludes zero at
alpha = 0.05 / (configurations promoted).

## D. Expected outcome, stated in advance

With 288 candidates and ~5 years of train data, several configurations WILL
look profitable on train by chance alone. That is the null hypothesis, not a
discovery. If nothing survives validation, the honest report is "no
configuration in the declared space demonstrated an edge" — and that is a
complete and publishable answer, not a failure to try harder.

## E. Clarification: the index_etf universe has no price band (declared with Amendment 1, before any index run)

The $5-25 / $20-60 price bands exist because a CASH-SECURED PUT ties up
strike x 100 in cash — at $1,200 of equity only cheap underlyings are
reachable. For a DEFINED-RISK SPREAD that constraint does not exist: max
loss is (width x 100 - credit), independent of the underlying's price. A
$2-wide SPY spread risks the same ~$150 as a $2-wide XLF spread.

So the index_etf universe applies NO price band; risk is bounded by the
existing MAX_RISK_PER_SPREAD ceiling, which is the gate that actually
governs affordability for spreads. Volume, OI, spread-width, earnings, VIX
and sanity gates all remain at production values. This is a clarification of
what "index_etf universe" means, declared before the first index run — not a
new knob, and it does not change the 288-configuration denominator.
