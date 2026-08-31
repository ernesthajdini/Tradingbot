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

---

# AMENDMENT 2 — the capital-scale test (declared 2026-08-29, BEFORE running)

Phase 2's index leg returned 7-9 trades in five years. The funnel says why,
and it is not a market fact: measured on 395 real index spread quotes
(2018-2021), $1-2 wide spreads fit MAX_RISK_PER_SPREAD=$130 but net $7-16 at
their own take-profit against a $25 floor, while $5-10 wides net $39-70 —
clearing the floor 100% of the time — and breach the $130 cap 100% of the
time. At $1,200 of equity NO width satisfies both gates. The index leg was
therefore never a test of the strategy; it was a test of the account size.

That makes one question worth asking, and it is a capital question, not a
strategy search: **does index premium selling have an edge once the account
is large enough to trade it?** Declared here before running.

## A. Declared space (24 configurations)

Fixed: universe=index_etf, structure=spread, DTE window 25-45 (production).

| knob | values |
|---|---|
| account scale (spread width, max risk per spread) | ($2,$130) = today's $1.2K account; ($5,$400) ~ $8K account; ($10,$800) ~ $16K account |
| target delta | 0.30, 0.20 |
| force-exit DTE | 21, 7 |
| stop multiple | 2.0, none |

3 x 2 x 2 x 2 = **24 configurations.** The scale levels come from the
measured median risk per width divided by the playbook's own 5%-per-trade
rule — they are not tuned, they are read off the table above.

## B. Discipline (unchanged from Amendment 1)

TRAIN 2017-02-08..2021-12-31. VALIDATE 2022..2023, entered only by
configurations passing the SAME promotion rule (>=100 train trades, positive
pessimistic mean, positive median, no trade >40% of P&L). SEALED 2024+
untouched. alpha = 0.05 / promoted. Every run counts in runs_log.jsonl.

## C. What this test can and cannot say

It CAN say whether the index/spread structure shows an edge at a workable
size. It CANNOT authorise trading at $1,200 — the account still cannot hold
a $400-risk position under the 5% rule, and nothing here changes the
go-live gate. A positive result is a REASON TO KEEP DEPOSITING toward a
size where the strategy is mechanically possible; a negative result retires
premium selling as this account's destination regardless of size.

---

# AMENDMENT 3 — equity signal study (declared 2026-08-30, BEFORE running)

Options remain the destination. This leg exists because the options studies
kept failing on the SAME two mechanisms, and both are testable on stocks
without buying anything: (1) friction — $1/contract x 4 legs is ruinous on
$50-100 credits, while a stock round trip is ~$2 total; (2) SELECTION — the
screener ranks candidates by realized-vol percentile, and the 8-year study
gives no evidence that ranking picks winners. A signal that genuinely sorts
future returns is the missing input to any options strategy: selling puts on
names a validated signal likes strictly dominates selling puts on names
sorted by RV percentile. So this leg answers the owner's second criterion —
"are the signals correct?" — and feeds the first.

## A. Declared space (16 configurations)

Universe: the optionable US equities already downloaded (10,621 histories,
delisted names included), filtered as-of-date by production's own liquidity
floor: price >= $5 and 20-day average volume >= 1,000,000. Long only —
shorting is not fundable at this account size.

| knob | values |
|---|---|
| signal | 12-1 momentum; 1-month reversal; 200-day trend; 20-day low-volatility |
| holding period | 21 or 63 trading days |
| portfolio size | 5 or 10 equal-weight names |

4 x 2 x 2 = **16 configurations.** Rebalanced at each holding-period
boundary. Positions are exited at the last available price when a name stops
trading; that count is reported, never hidden.

Friction, charged on every entry and exit, in the same [base, pessimistic]
band convention the options studies use: $1.00 commission per trade plus
0.10% (base) / 0.25% (pessimistic) slippage of notional.

## B. Discipline (identical to Amendments 1-2)

TRAIN 2017-02-08..2021-12-31; VALIDATE 2022..2023 entered only by
configurations that pass the promotion rule; SEALED 2024+ untouched.
alpha = 0.05 / promoted.

## C. Promotion rule (fixed now, adapted to a portfolio strategy)

Promoted only if ALL hold on train:
1. >= 100 closed positions;
2. positive total return at the PESSIMISTIC friction band;
3. beats SPY buy-and-hold over the same window at the pessimistic band;
4. max drawdown <= 1.5x SPY's over the same window.

A FINDING requires, on VALIDATE: still positive at pessimistic fills, still
beating SPY, and a bootstrap 95% CI on mean per-position return excluding
zero at the corrected alpha.

## D. Stated in advance

Cross-sectional equity factors are the most data-mined subject in finance;
that they look good on a training window is the null hypothesis. The
economics are also hostile at this size: 5 positions of ~$240 paying ~$2
round trip is ~0.8% per rebalance, so a 21-day holding period concedes
roughly 10%/yr to friction before any signal is asked to work. If nothing
survives, the honest result is "no declared signal beats holding an index
fund at this account size", which is itself decision-relevant.

---

# AMENDMENT 4 — regime-conditional premium selling (declared 2026-08-30, BEFORE running)

The playbook calls the post-spike window "historically the richest
premium-selling regime a patient 1-lot trader can access", and no study has
ever tested it: the VIX kill switch blocks entries above 35, so the 2020
crash contributes ZERO trades to every result so far. This leg tests whether
WHEN you sell matters, holding everything else at production values.

## A. Declared space (10 configurations)

Fixed at production: 25-45 DTE, 0.30 delta, 21-DTE force exit, 2x stop.
Only the entry regime varies.

| knob | values |
|---|---|
| universe / structure | single_name/csp ; index_etf/spread at the $5-wide, $400-risk scale |
| entry regime | none (baseline) ; post_spike (playbook: VIX>35 within 10 sessions, now <30) ; vix_above_25 ; vix_top_quartile (trailing year) ; vix_falling (below its 10-day average) |

2 x 5 = **10 configurations.**

## B. POWER LIMIT, measured before running and stated as a caveat

Qualifying sessions, 2017-02-08..2023-12-31 (1,735 sessions):

| regime | sessions | share | per year |
|---|---|---|---|
| none | 1,735 | 100% | 252 |
| post_spike (playbook) | 46 | 2.7% | 7 |
| vix_above_25 | 325 | 18.7% | 47 |
| vix_top_quartile | 399 | 23.0% | 58 |
| vix_falling | 982 | 56.6% | 143 |

The baseline strategies make 13-17 trades a YEAR with every session
available. Restricting to 2.7% of sessions therefore cannot produce a
testable sample: **the playbook's post_spike hypothesis is expected to be
UNTESTABLE at this trade frequency, and that is a legitimate result** — a
regime that rare cannot carry an account regardless of its edge. The
vix_above_25 and vix_top_quartile arms will be thin (~3 trades/yr expected);
only vix_falling should approach a usable sample.

## C. Discipline

Unchanged: TRAIN 2017-02-08..2021-12-31, VALIDATE 2022-2023 on promotion
(>=100 train trades, positive pessimistic mean, positive median, no trade
>40% of P&L), SEALED 2024+ untouched, alpha = 0.05/promoted. Trade counts
are reported for every arm whether or not it promotes, so an untestable
hypothesis is visibly untestable rather than silently absent.

---

# AMENDMENT 5 — call structures and iron condors (declared 2026-08-30, BEFORE the data finished downloading)

Every study so far sold PUTS only, because puts were the only side ever
downloaded. Calls are pulling now, which opens the last major untested
branch — and one structure in it has a genuine arithmetic argument rather
than a hopeful one.

**Why the iron condor is the interesting case.** Measured on the real index
quotes (Amendment 2 evidence table), a $5-wide put spread pays ~$102 credit
against ~$398 risk. Adding a call spread on the same expiry collects a
SECOND credit while the max loss stays one-sided — only one wing can finish
in the money — so credit roughly doubles against roughly unchanged risk.
Friction also doubles (4 legs instead of 2, so ~$8 round trip instead of
~$4), but it doubles against a doubled credit, whereas every failure so far
came from friction eating a credit that could not grow. This is the first
structure whose economics differ in KIND from what has already failed.

## A. Declared space (12 configurations)

Universe: index_etf (calls were pulled for the 30 ETFs only).
Fixed at production: 25-45 DTE, 21-DTE force exit, 2x stop, all liquidity,
OI, spread-width, sanity and friction gates.

| knob | values |
|---|---|
| structure | put_spread (baseline, already measured) ; call_spread ; iron_condor |
| account scale (width, max risk) | ($5,$400) ; ($10,$800) |
| short-leg delta per side | 0.30 ; 0.20 |

3 x 2 x 2 = **12 configurations.**

## B. HONESTY COST, declared before running

Production's setup_generator builds PUT structures only. Call spreads and
condors therefore require NEW backtest-only code, so this leg does NOT have
the "replays the real production gates" property that Amendments 1-4 had.
The new generator mirrors the production gates deliberately — same friction
model, same net-credit-at-designed-exit floor, same OI and spread-width
gates, same never-sell-ITM rule applied to both wings, same sanity caps, same
exit rules — but it is a mirror, not the original. Every result from this leg
is stamped `generator: backtest_mirror`. If a condor configuration ever
survives validation, porting it to production is a REWRITE, and the live
system would need its own verification before trading it.

## C. Discipline

Unchanged: TRAIN 2017-02-08..2021-12-31; VALIDATE 2022-2023 on promotion
(>=100 train trades, positive pessimistic mean, positive median, no trade
>40% of P&L); SEALED 2024+ untouched; alpha = 0.05/promoted.

## D. Stated in advance

The put leg at these scales produced 27-66 trades over five years. A condor
needs BOTH wings to pass every gate on the same expiry, so its trade count
can only be LOWER than the put leg's, not higher. The most likely outcome is
therefore another under-powered sample — and if so, the finding is that the
structure is unreachable at this frequency, not that it was tested and
failed.

---

# AMENDMENT 6 — condor management, and the third window (declared 2026-08-31, BEFORE running)

Amendment 5's corrected condor is the only structure in 278 configurations
with a positive TRAIN result that is not an artifact: +$27.96/trade over 170
trades at $10-wide / $800-risk / 0.20-delta. Its validation returned
-$27.30/trade on 77 trades with a bootstrap CI of [-89.88, +29.64]. That
interval spans both outcomes: it is an UNDERPOWERED TEST, not a clean
rejection, and saying "it failed" would overstate the evidence exactly as
badly as saying "it worked".

Two things follow, and both are declared here before either runs.

## A. Management is the untested lever (4 configurations)

Every study so far takes maximum loss on a tested position. No practitioner
does that; they roll. The mechanism is real and specific: a short vertical
whose short strike is breached can be closed and re-opened at a later
expiry for a NET CREDIT, because the further-dated option carries more time
value. That either buys the position time to recover or compounds the loss —
which of the two is an empirical question this project has never asked.

Fixed at the best-performing cell (iron_condor, $10 wide, $800 risk, 0.20
delta, 25-45 DTE, 50% take-profit):

| knob | values |
|---|---|
| stop policy | 2x credit (production) ; none (hold to the 21-DTE exit) |
| on stop | close (current behaviour) ; roll — immediately re-open the same underlying at the next expiry, no cooldown |

2 x 2 = **4 configurations.** Longer-dated condors (45-70 DTE) were
considered and are NOT included: the index pull fetched a 50-day active
window per expiry, so no quotes exist at 70 DTE. Testing them would need a
re-pull, and inventing the data is not an option.

## B. The third window — a declared deviation from the sealed-period rule

The original rule reserved 2024+ for ONE configuration that survived
validation. Nothing survived, so under a literal reading the window stays
shut forever and the condor question stays permanently ambiguous.

I am opening it for the condor, ONCE, and the terms are fixed now:

1. Only the Amendment 5 corrected condor and the four Amendment 6 variants
   may touch 2024-01-01 onward. Nothing else, ever.
2. The result is BINDING IN BOTH DIRECTIONS. If the third window is
   positive with a CI excluding zero, the condor is the project's finding
   and goes to a production rewrite with its own verification. If it is
   negative or inconclusive, premium selling is CLOSED as this account's
   destination and no further options amendment will be written.
3. It is reported whatever it says, in full, including trade counts and
   confidence intervals.

This is a deviation, and calling it anything else would be dishonest: a
third window opened after a failed second window is the shape of window
shopping. What makes it defensible is that the terms, the configurations
and the binding consequence are fixed BEFORE the run, and that the window
is genuinely independent data neither the search nor the validation ever
touched.

---

# AMENDMENT 7 — chart patterns: do they predict anything? (declared 2026-08-31, BEFORE running)

Amendment 6 closed premium selling. This asks the owner's question directly:
can a chart be READ? Amendment 3 tested the quantified core of chart reading
(momentum, reversal, trend, volatility) as tradeable portfolios and all four
failed after costs — but that conflated two questions this leg separates:

1. **Statistical:** does a pattern's occurrence shift the distribution of
   forward returns at all, versus the same stocks on all other days?
2. **Economic:** is that shift large enough to trade after $2 round-trip
   commissions and slippage?

A pattern can pass (1) and fail (2). That is worth knowing precisely,
because it says whether the chart carries information the account is simply
too small to monetise, or no information at all.

## A. Declared patterns (12) x horizons (3) = 36 tests

OHLC was never loaded before — the stock loader kept only Close, so no
range or candle pattern has ever been testable here. Raw files carry
open/high/low/close/volume.

| # | pattern |
|---|---|
| 1 | 20-day breakout (close > prior 20-day high) |
| 2 | 52-week high |
| 3 | 20-day breakdown (close < prior 20-day low) |
| 4 | gap up > 3% |
| 5 | gap down > 3% |
| 6 | inside day (high < prior high AND low > prior low) |
| 7 | NR7 (narrowest true range of the last 7 sessions) |
| 8 | golden cross (50-day crosses above 200-day) |
| 9 | RSI(14) < 30 |
| 10 | RSI(14) > 70 |
| 11 | volume spike (volume > 3x its 20-day average) |
| 12 | hammer (lower wick >= 2x body, close in the top third of the range) |

Forward horizons: 1, 5 and 21 sessions, measured on split-adjusted closes.

## B. Method

Universe and liquidity filters as production: price >= $5, 20-day average
volume >= 1,000,000, as-of-date, delisted names included. For each pattern
and horizon: mean forward return on signal days versus the mean over ALL
eligible stock-days (the base rate), Welch t-test, and the edge restated in
basis points. Effects are also reported net of a 0.83% round-trip cost
(the measured $2 commission plus slippage on a $240 position) so the
statistical and economic answers are visible side by side.

TRAIN 2017-02-08..2021-12-31 for discovery; any pattern significant there is
re-measured once on 2022-2023. Sealed 2024+ stays shut — Amendment 6B spent
the third window on condors and it is not reopened.

alpha = 0.05 / 36 = 0.00139. With ~9,000 names over five years these tests
have enormous sample sizes, so tiny and useless effects WILL reach
significance; the report leads with effect size, not p-values.

## C. Stated in advance

Published research finds most classic chart patterns carry little to no
out-of-sample edge, and that what remains is smaller than retail transaction
costs. The expected result is therefore "some patterns shift returns by a
few basis points, none by enough to trade at this account size." If that is
what comes back, it is the answer to the question, not a failure to look.

---

# AMENDMENT 8 — concentration + pattern signals (declared 2026-08-31, BEFORE running)

Amendment 7 found two real effects and I closed the question too early. Two
things were missed, and both are mechanical rather than hopeful.

**1. Cost is mostly FIXED per trade, so concentration collapses it.** IBKR
charges a $1.00 minimum per stock order. Every study so far used 5 or 10
positions — $120-240 each at this account size — where that $1 is 0.4-0.8%
EACH WAY. At 3 positions it is ~0.25% and at 2 positions ~0.17%. Measured
round trips: 10 names ~1.7%, 5 names ~0.83%, 3 names ~0.50%, 2 names ~0.33%.
The gap-up signal's absolute 21-day return was +170bp (train) / +94bp
(valid); it loses to an 83bp round trip and beats a 33bp one. Position count
was never a declared knob below 5 — that is an omission, not a finding.

**2. The 52-week-high effect needs no shorting to be useful.** Stocks at new
52-week highs underperformed by 104bp (train) and 150bp (valid). I dismissed
it as unshortable, but as an EXCLUSION filter on a long book it costs
nothing to apply.

## A. Declared space (24 configurations)

Universe and liquidity exactly as Amendment 3/7: price >= $5, 20-day average
volume >= 1M, as-of-date, delisted names included, long only.

| knob | values |
|---|---|
| entry signal | gap_up_3pct ; gap_up_3pct AND NOT at a 52-week high ; 20-day breakout AND NOT at a 52-week high ; none (random eligible = control) |
| portfolio size | 2 ; 3 ; 5 |
| holding period | 21 ; 63 sessions |

4 x 3 x 2 = **24 configurations.** The "none" arm is a control, not a
strategy: if a signal cannot beat randomly chosen eligible names at the same
concentration and cost, the signal is doing nothing and the result is
concentration luck.

Friction is charged as measured: $1.00 per trade each way plus 0.10% (base)
/ 0.25% (pessimistic) slippage — so the cost advantage of concentration
appears in the P&L rather than being assumed.

## B. Discipline

TRAIN 2017-02-08..2021-12-31; VALIDATE 2022-2023 on promotion; SEALED 2024+
stays shut (spent on condors in Amendment 6B). alpha = 0.05/promoted.

Promotion requires ALL of: >= 100 closed positions; positive total at the
PESSIMISTIC band; beats SPY buy-and-hold over the same window; max drawdown
<= 1.5x SPY's; AND beats its own matched control arm.

## C. Stated in advance

Concentration cuts cost and multiplies variance in equal measure: 2 names
means one bad holding is half the book. Expect higher returns AND far worse
drawdowns, and expect the drawdown constraint to be what kills these, not
the return. The control arm exists precisely so a concentration-driven
result cannot be mistaken for a signal-driven one.

---

# AMENDMENT 9 — long puts on a validated signal (declared 2026-08-31, BEFORE running)

## Deviation, stated plainly

Amendment 6B said that if the third window failed, "no further options
amendment will be written". It failed and I honoured that. The owner has now
directed that options are the sole focus, so this amendment exists by his
instruction. The 6B commitment was written about premium SELLING — the thing
actually tested — and I phrased it more broadly than the evidence warranted.
What is closed stays closed: nothing here re-litigates selling premium.
This is the opposite side of the trade.

## Why this specific test

Across 306 configurations exactly ONE effect survived out-of-sample
validation: stocks at a 52-week high UNDERPERFORM the universe by 104bp
(train) and 150bp (validate) over the following 21 sessions, with the same
sign and similar size in both windows. I set it aside because monetising a
negative view needs shorting, which is neither fundable nor practical at
$1,200.

That was the wrong conclusion. A LONG PUT expresses exactly that view with
defined risk, costs $50-200 rather than requiring margin, and single-name
put chains are already on disk. This is the first test in the project where
a validated signal meets an instrument that suits the account.

The honest counter-argument, stated before running: a bought option bleeds
time value every day, and 130bp a month of drift may simply not cover it.
That is the question.

## A. Declared space (18 configurations)

Universe: the single-name option universe already pulled, production
liquidity gates, delisted names included, 2017-02-08 onward.

| knob | values |
|---|---|
| entry signal | at_52w_high ; at_52w_high AND rsi>70 ; none (CONTROL: random eligible names) |
| put delta | 0.30 ; 0.40 (nearer the money = more delta, less leverage) |
| exit | hold to 21 DTE ; take profit at +100% of premium, else 21 DTE ; hold to expiry |

3 x 2 x 3 = **18 configurations.** DTE window fixed at production 25-45.
Position sizing: one contract per signal, capped at 2 concurrent, since a
long put's max loss is its premium.

Friction: the production model — $1.00 per contract per leg (2 legs round
trip) plus the standard slippage band on premium paid and received.

## B. Discipline

TRAIN 2017-02-08..2021-12-31; VALIDATE 2022-2023 on promotion; SEALED 2024+
stays shut. alpha = 0.05/promoted. Promotion needs >= 100 closed trades,
positive mean at the PESSIMISTIC band, positive median, no single trade
above 40% of total P&L, AND beating its own matched control arm.

## C. Stated in advance

Long options have a structural headwind: the variance risk premium exists
because buyers overpay on average, which is the same fact that made SELLING
look attractive for 300 configurations. A long-put strategy must therefore
beat not just zero but that premium. The expected result is that the signal
is real and the time decay eats it. The control arm decides whether any
result is the signal or just a directional bet that happened to pay.
