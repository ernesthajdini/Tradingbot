# Learning-Acceleration & Adaptive-Sizing Study (Aug 2026)

**Question studied:** (1) Can we give the system "full power to learn as fast as
possible, as much as possible"? (2) Should sizing adapt to starting capital +
monthly deposits (fractional Kelly on the measured edge)?

**Method:** 3 independent research lenses (backtest/data feasibility, learning
statistics, Kelly sizing) → adversarial red-team that re-executed every
computation and re-fetched every price → synthesis. 28 findings; each carries a
red-team verdict (sound / weakened / demolished).

---

## VERDICT

**Half great, half trap — and the great half is not the half that feels powerful.**

The hard physical limit: same-day short-put trades share one market factor.
The market serves only ~25 independent 2-week windows per year — roughly 42–84
*effective* observations annually no matter how many paper trades are logged.
**5x trade volume = ~1.3x information.** (Computed: n_eff = n/(1+(n−1)ρ) at
ρ = 0.3–0.6; red-team reproduced.)

That kills most of the "full power" menu (wider universe, fine-grained arm
grids, volume-as-evidence). What survives is genuinely excellent — and cheap.

The adaptive-sizing half is **premature by construction**, on four independent
grounds (each alone sufficient):

1. **Base-case Kelly says bet zero.** The live tier's designed payoff
   (win ~$27.5 at p≈0.725, lose $130) has EV = −$15.81/contract; breakeven win
   rate 82.5%. There is nothing to size until the edge is measured.
2. **The Kelly fraction is unidentifiable at every achievable sample.** At
   n=200 closed trades with 150 wins, the 95% CI on f* is **[0.000, 0.516]** —
   the data cannot distinguish "never bet" from "bet half the account". Still
   ~5x too wide at n=500. Uncertainty-honest fractional Kelly computes to
   ≈ the existing 5% rule — *the "upgrade" is the incumbent*.
3. **Adaptive sizing amplifies mark-optimism bias ~7x.** A +5pt optimistic
   model-mark bias turns a truly flat book into perceived +$3.35/contract EV;
   estimated Kelly then sizes a flat book to ~17% of bankroll 84% of the time.
   Fixed-5% merely ignores the bias; Kelly weaponizes it. Market-marking
   (map #2, now shipped) is a hard prerequisite even for the conversation.
4. **Any variant ≥ half-Kelly trips the playbook's 20%-from-peak wire ~46% of
   the time** in the only scenario where it outperforms — each trip costs a
   90-day exile of the scarcest resource (live evidence at ~6 trades/yr).

And the closer: **account.py's 5%/8% rule already IS quarter-to-half Kelly**
($130 ≈ 5% of equity ≈ f*/5 at the mid-case edge) and already scales with
deposits automatically. Deposits act as bond-like wealth (12 committed months
≈ 3x current equity of effective bankroll), which *removes* growth-optimality
as an argument for changing sizing in either direction. Options P&L doesn't
match the $2,400/yr deposit stream until ~$75K–180K equity.

---

## DO NOW (free, in order)

1. **ThetaData free account — verify 2 facts (an afternoon).** Their docs
   contradict themselves on free-tier EOD history (1 year vs since 2023-06-01)
   and say nothing about delisted underlyings. These two facts decide the
   entire spend question. Also pull optionsDX free SPY/QQQ samples as a $0
   pipeline dry-run.
2. **Shadow book of the 11–19 discarded filter-passers per run (2–3 days).**
   Raw counts ×5.3 but effective sample only ×1.15–1.5 — its real value is
   **ranker validation**: top-5-taken vs discarded is a same-day controlled
   contrast where the market factor cancels out. Two binding requirements:
   - **Separate table.** Verified in code: `golive.gate_status()` and
     `market_marked_share()` pool ALL journal closes — shadow rows in the
     production journal would corrupt both the 200-counter and the 50% share.
   - Mark-rotation scheme: 130–300 concurrent shadows vs the ≤24
     chain-fetches/hour budget, or shadows silently degrade to model marks.
3. **Earnings-blackout autopsy (~1 day, piggybacks on shadow infra).** The
   only gate autopsy with forward statistical power (~20 quasi-independent
   name-quarter events per season). Start now → 4 seasons by mid-2027.
   Expect a directional verdict only (MDE ~$56/trade at 6mo, ~$40 at 12mo).
4. **Keep the 2-arm delta split (0.20 vs 0.30), nothing wider** (map #7).
   ~93–186 closes/arm by January (low end likely at MAX_VIRTUAL_OPEN=24).
   Frame as catastrophe detection, not tuning.
5. **Backtest harness against FREE data before spending anything
   (realistic 6–12 days, not 3–6).** Separability verified:
   `setup_generator.py` needs a ~4-line injectable-date fix;
   `virtual_tracker.evaluate_open_position` already takes injectable
   today/market_price — so the backtest runs the REAL gates, not a
   reimplementation. Honest-harness requirements the first estimate missed:
   as-of-date universe reconstruction (price band + volume per day),
   **delisted names** (excluding them censors the left tail exactly where
   short puts die), corporate actions, historical earnings calendar.

**Overfitting pre-registration (non-negotiable, commits BEFORE data arrives):**
frozen-config manifest naming the ≤4 knobs allowed to move (DTE window, delta,
width, credit floor); every run logged as the multiplicity denominator; paired
same-day contrasts across declared cells; one sealed test year opened once.
config.py has ~15 tunable knobs — without the manifest, the harness is a
structural bypass of the 14-day anti-tinker cooldown.

**Honest cost accounting (red-team requirement):** the dominant cost is build
time — 6–12 days ≈ €2,900–11,500 at the playbook's own €60–120/hr — chargeable
only to the $10–20K-scale skill asset, never to this account's ~$150/yr
options ceiling.

---

## SPEND OR NOT

**Recommendation: $80 — one month of ThetaData Standard, download everything,
cancel.** (Prices re-verified live Aug 2026: Free $0 / Value $40 / Standard
$80 / Pro $160 per month.) Standard, not Value: Value's history is
"2020-01-01" per docs but "4 years" per the pricing page — if rolling, it
starts ~Aug 2022 and misses BOTH stress regimes (Mar-2020, most of 2022 bear)
that the purchase exists to capture. Standard serves 2016+. No multi-month
subscription — EOD flat pulls in one month, rerun locally forever.
**Total budget $0–80, worst case $160.**

What it buys: 8 years ≈ **336–672 effective trades** spanning two real vol
regimes forward paper cannot see before 2027; paired same-day arm comparisons
with **$3.6–8.8/trade minimum detectable effect** on DTE/delta/credit-floor
(vs $23–32/trade detectable from forward paper by January; forward paper would
need ~10 years to match). EOD granularity is correct for 9–24-day holds —
the two EOD biases run in opposite directions and are bounded; live GTC
take-profit orders make the win side conservative.

What it cannot buy: proof of positive live expectancy, slippage information
(backtest fills are as slippage-blind as paper — report under the same
pessimistic band), or any of the 200 forward trades the gate demands.

**Flips to $0 if:** the free tier really serves 2023-06→present AND the
harness on free data already falsifies the config (negative expectancy at
mid-fill under the pessimistic band) — strategy dead, $80 saved, the entire
2026 paper year saved. Best possible outcome of the whole project.

**Flips to $945 (historicaloptiondata.com 5-yr Level 2 flat files) only if:**
ThetaData cannot serve delisted names AND a partial comparison shows
survivorship materially changes results. Delisted coverage — not "owning the
data" — is the only honest reason that purchase ever happens.

**Ruled out:** Polygon/Massive (only the $199/mo Advanced tier carries the
historical quotes our quote-dependent gates need — dominated), ORATS $99/mo
(hosted backtester can't run OUR gate code — which is the whole point),
CBOE DataShop / OptionMetrics (dominated or inaccessible), any tick/minute
data (marks are daily; useless granularity).

---

## LATER — adaptive sizing triggers (ALL must hold)

- Go-live gate passed + **~20 live fills** (fill-vs-mid quality converges only
  then; earliest realistically Feb–Mar 2027).
- An edge estimate whose **pessimistic-band CI excludes zero** — a true
  +$5/trade edge shows t≈1.2 at 200-effective, t≈1.9 at 500-effective; in
  practice this comes only from backtest + live fills combined.
- **Equity ≥ ~$5K** (≈ Mar 2028 at $200/mo) before multi-position sizing has
  degrees of freedom; **≥ $10K** before CSP-style concentration Kelly is legal.
- Value check: sizing optimization adds 10–30% *relative* to the options P&L
  line — $10–45/yr at today's ceiling. Build it when the line is hundreds/yr.

Until then, account.py's rule IS the correct adaptive policy.

---

## DO NOT DO

- **Universe expansion 107 → ~200.** The cleanest trap on the list: +89% raw
  trades = **+1.7–5.6% effective sample**, while thin names closing on model
  marks drag the pooled market-marked share toward the 0.50 gate floor —
  noise could mechanically BLOCK go-live — and per-ticker scores (5-trade
  shrinkage prior) thin everywhere.
- **6-arm DTE×delta grid.** ~31–62 closes/arm by January detects a $26/trade
  effect vs plausible true effects of $3–10; $5/trade needs 1,243/arm ≈ 10.4
  years. The backtest answers this question instead.
- **Any paper-derived OI-floor / spread-gate verdict.** Their cost IS
  slippage; paper fills contain zero slippage information. Only the first ~20
  live fills speak. (Credit-floor "autopsy" is deterministic arithmetic the
  near-miss ledger already computes — not an experiment.)
- **Shadow or backtest rows in the production journal.** Verified: the gate
  pools all closes. Separate tables, always.
- **Counting backtest evidence toward the go-live gate or touching
  EARLIEST_LIVE_DATE.** The gate's terms are forward-operational by design.
- **Buying data before the harness + manifest exist.** Inverts the value
  order and hands 15 knobs to an unpoliced loop over the exact regimes
  (post-spike!) we'd be tempted to condition on.
- **Standing subscriptions, $945 flat files as a first move, Polygon $199,
  ORATS, tick/minute data.** All dominated.
- **Treating raw trade counts as learning.** 5x volume = 1.3x information.
  The calendar is the binding constraint; only history breaks it.

---

*Full agent transcripts: session workflow wf_e39fad94-62b (5 agents, 28
findings, red-team re-executed all math and re-fetched all vendor pricing).*
