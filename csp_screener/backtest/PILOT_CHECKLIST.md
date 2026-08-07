# Free-Tier Data Pilot — Checklist (gates the $80 spend)

The study's spend decision hangs on two facts about ThetaData that their own
docs contradict themselves on. This pilot settles both for $0 before any
money moves. **Approved budget: $80 one-time (one month of Standard,
download, cancel) — spent ONLY if steps 1–3 below pass.**

## Step 1 — Owner creates the free account (~5 min, Art does this part)

1. Go to https://www.thetadata.net → Sign Up → **Free** tier.
2. No card needed for Free. Use the usual qa-style email, not a personal one
   if preferred.
3. Note the credentials — the Theta Terminal (their local Java app) logs in
   with them; the REST API runs against the local terminal.

## Step 2 — Verify the two load-bearing facts (I do this, with the account)

- [ ] **History depth on Free:** request EOD option chains for a liquid name
  (e.g. AAPL) at dates 2023-07-03, 2024-07-01, 2025-07-01. Docs claim both
  "1 year" and "since 2023-06-01" — whichever actually returns data decides
  whether Free covers any stress regime.
- [ ] **Delisted underlyings:** request an EOD chain for a name that died
  (candidates: BBBY (2023), SIVB (2023), FSR (2024)). If the API serves
  them, survivorship-honest reconstruction is possible on ThetaData alone.
  If not, the $945 historicaloptiondata.com flat files become the only
  honest route — and that purchase needs its own justification pass.

## Step 3 — $0 pipeline dry-run

- [ ] Download optionsDX free SPY/QQQ EOD samples (free account,
  https://www.optionsdx.com) → `backtest/data/` (gitignored).
- [ ] `data_loader.load_optionsdx_csv` parses them → normalized frame.
- [ ] **Adjustment alignment check** (data_loader docstring): price frames
  must be AS-TRADED, matching the chains' strikes — verify frame Close vs
  chain `underlying_price` agree within ~1% on overlapping dates for every
  ticker, especially names with splits in the window. Retroactively
  split-adjusted closes (yfinance default) against as-traded chains corrupt
  entries and model marks silently.
- [ ] Engine smoke run on one quarter of real data, production params only.
  Confirm: trades open and close, market-marked share is high, run lands in
  `runs_log.jsonl`, nothing appears in any journal, `data_ended_closes`
  is sane.

## Step 4 — The spend (only if 1–3 pass)

- [ ] One month ThetaData **Standard** ($80) — NOT Value: Value's history
  ("2020-01-01" per docs, "4 years" per pricing page) may miss Mar-2020 and
  most of the 2022 bear if "4 years" is rolling. Standard serves 2016+.
- [ ] Bulk-download EOD chains for the as-of-reconstructed universe,
  2016 → present, to local flat files. **Cancel the subscription.** All
  reruns are local forever after.
- [ ] Write the ThetaData adapter in `data_loader.py` against the actual
  files on disk (deliberately not written in advance — see module docstring).

## Step 5 — Before the first real run

- [ ] Re-read `MANIFEST.md`. The declared grid is the whole game.
- [ ] Confirm `runs_log.jsonl` is committed and starts at zero real-data runs.

## Kill condition (a SUCCESS outcome)

If the production configuration shows negative expectancy at mid-fill under
the pessimistic band across 2016–2023: the strategy is falsified, the $80 is
saved (or already spent and well spent), the 2026 paper year is released,
and the owner gets the single most valuable answer this project can produce.
