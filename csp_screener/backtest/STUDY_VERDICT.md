# Study Verdict — 8 years, 240 configurations, one answer

**Question asked (2026-08-28):** "you have 8 years worth of data — find a way
to make money."

**Answer:** No configuration in the declared search space demonstrated an
edge. The single most favourable result — cherry-picked as the maximum of a
240-configuration search, measured on 27 trades, never validated — is
**0.54% per year**. Every other configuration is worse; most are negative.

---

## 1. What was searched

| leg | configurations | result |
|---|---|---|
| Single names, CSPs + spreads (Amendment 1) | 144 | 0 promoted |
| Index/sector ETFs, spreads (Amendment 1) | 72 | 0 promoted |
| Index ETFs across account scales (Amendment 2) | 24 | 0 promoted |
| **total** | **240** | **0 findings** |

Knobs swept: universe, structure, DTE window, target delta (0.15-0.30),
force-exit DTE (21 / 7 / hold-to-expiry), stop multiple (2x / 3x / none),
account scale ($1.2K / $8K / $16.5K).

Every space, the split (train 2017-02-08..2021-12-31, validate 2022-2023,
sealed 2024+) and the promotion rule were written into MANIFEST.md BEFORE
each leg ran. Not one configuration reached the promotion bar, so validation
was never entered and the sealed period was never opened.

## 2. Why it fails — three mechanisms, each measured

**a. The friction/risk scissors.** On 395 real index spread quotes
(2018-2021):

| width | median credit | median risk | net at take-profit | clears $25 floor | fits $130 cap |
|---|---|---|---|---|---|
| $1 | $26 | $74 | $7 | 0% | 100% |
| $2 | $47 | $152 | $16 | 1% | 11% |
| $5 | $102 | $398 | $39 | 100% | 0% |
| $10 | $173 | $827 | $70 | 100% | 0% |

Narrow spreads fit the account and cannot out-earn commissions; wide spreads
out-earn commissions and cannot fit the account. At $1,200 the overlap is
empty — the index leg produced 7 trades in five years not because the market
refused, but because the gates did.

**b. Not enough decisions.** Across all 240 configurations the maximum trade
count on a five-year training window was 72 (single names) and 66 (index at
$16.5K scale) — 13-14 per year. Nothing reached the 100-trade bar. At that
frequency an edge cannot compound and cannot be proven.

**c. The shape is wrong for the size.** Win rates are genuinely high
(61-85%) and median trades are positive, but P&L is decided by 4-9 tail
events per hundred trades. Verified real, market-confirmed: First Republic's
2023 collapse (-$714), AMC, TEVA, MNMD. Index products remove that tail —
and remove enough premium with it that the remainder cannot clear friction.

## 3. The best case, stated at its most flattering

`index_etf/spread w$5 risk$400 d0.20 exit7 stopNone` — 27 trades, 85% win,
+$7.86/trade at pessimistic fills = **$43/year on the $8,000 of equity its
own risk rule requires: 0.54% per year.**

That figure is the maximum of a 240-way search on 27 trades. It is what a
selection bias looks like, not an edge. Taken at face value it still loses to
a deposit account while carrying trades that can lose $400 at once.

## 4. What this does NOT say

- It does not say options selling never works. It says it does not work
  **in this declared space, at this account size, at these commissions,
  with this trade frequency**.
- It does not prove a loss with statistical significance. It shows an
  estimate consistently at or below zero, and — decisively — that even the
  favourable tail of the estimate is economically irrelevant.
- The sealed 2024+ window remains unopened. There is nothing to confirm.

## 5. Consequences

1. **The January 2027 go-live target is retired for this strategy.** Not
   because a gate blocked it, but because the strategy it was gating has
   been measured and does not pay.
2. **The 2026 paper-trading programme has served its purpose.** Its job was
   to produce this decision; five more months of paper trades would add
   ~85 trades to a question already answered by 240 configurations over
   eight years.
3. **The infrastructure keeps its value.** The purchased data, the replay
   engine, the ranking-parity proof, the pre-registration discipline and the
   shadow book are strategy-agnostic. Any future idea can be put through the
   same machine in an afternoon — that capability is what the $160 bought.
4. **Deposits remain the outcome.** The playbook assumed deposits were ~87%
   of results at this size. That is now measured rather than assumed.
