# CSP Screener

Sunday-night cash-secured put candidate screener. Sends you a weekly email with
underlying-stock candidates that would be reasonable to sell puts on. **You**
choose the contract.

Tracks every suggestion as a virtual trade so the screener accumulates a real
performance record over time — even when you don't take any live trades.

## What it does

| | |
|---|---|
| **Sunday 6 PM** | Sends weekly email with ranked candidates (both tiers) + virtual track record |
| **Mon–Fri hourly (market hours)** | Marks open virtual positions to market, closes any that hit exit rules |
| **Mon–Fri after close** | **Daily indications run**: full screen, opens paper positions (one per ticker), no email — see the dashboard's Daily tab |
| **Every run** | Pings healthchecks.io (silence = something broke) |

Everything runs in **GitHub Actions** (see `.github/workflows/`); data dual-writes
to local JSONL + **Supabase**, and the **Vercel dashboard** (`dashboard-web/`)
is the read surface. Your PC is not required. See `CLOUD_DEPLOY.md`.

## What it deliberately does NOT do

- Auto-execution (never — tickets are staged, you approve/reject in IBKR)
- Real-time alerts
- Contract picks you didn't gate: the LIVE tier stages complete tickets; the sandbox tier is paper-only research

---

## Setup (one-time, ~15 minutes)

### 1. Install Python dependencies

```
pip install yfinance pandas numpy requests ib_insync pytest
```

### 2. Set up Gmail App Password

1. Go to https://myaccount.google.com/apppasswords
2. Sign in if needed
3. Under "App passwords", create one named `csp_screener`
4. Copy the 16-character password (no spaces)

### 3. Sign up for Finnhub free tier (earnings data)

1. Go to https://finnhub.io/register
2. Confirm email, copy your API key from the dashboard

### 4. Sign up for healthchecks.io (deadman switch)

1. Go to https://healthchecks.io
2. Create a check named "csp_screener_weekly", schedule = `0 18 * * 0` (Sunday 6 PM)
3. Create another check named "csp_screener_daily", schedule = `0 17 * * 1-5` (weekday 5 PM)
4. Copy each ping URL (looks like `https://hc-ping.com/<uuid>`)

### 5. Set environment variables (Windows)

Open PowerShell as your user and set permanently:

```powershell
[Environment]::SetEnvironmentVariable("SMTP_USER", "your.email@gmail.com", "User")
[Environment]::SetEnvironmentVariable("SMTP_PASSWORD", "abcd efgh ijkl mnop", "User")
[Environment]::SetEnvironmentVariable("SMTP_TO", "your.email@gmail.com", "User")
[Environment]::SetEnvironmentVariable("FINNHUB_API_KEY", "your_finnhub_key", "User")
[Environment]::SetEnvironmentVariable("HEALTHCHECKS_PING_URL", "https://hc-ping.com/<uuid-for-weekly>", "User")
```

Close and reopen any open terminals so they pick up the new vars.

### 6. Install Windows Task Scheduler entries

```cmd
cd "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent\csp_screener"
install_tasks.bat
```

(See `install_tasks.bat` for what it sets up.)

### 7. Test it now (don't wait for Sunday)

```cmd
cd "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent"
python -m csp_screener.main --no-ibkr
```

This runs the full pipeline once, writes an email preview to
`csp_screener/output/email_preview_*.html`, and sends it to your gmail
(if SMTP is set). Open the HTML in a browser to confirm formatting.

---

## How to use it

Every Sunday night you get an email with:

1. **5 candidate underlyings** ranked by realized-vol percentile, with a
   *suggested* put strike, expiration, premium, max loss, and breakeven.
2. **Screener's virtual track record** — would this strategy have made money
   in the last 30/90/all days?
3. **Open virtual positions** with current P&L.

For each candidate that looks interesting:

1. Open barchart.com, verify actual IV rank (the screener uses realized vol
   as a proxy — it's directional but not literal IV rank).
2. Open IBKR TWS, look at the chain. Verify bid/ask spreads in real-time.
3. Decide if you want to take it. **Most weeks the answer is "no, skip."**
4. If yes, write your exit plan in the journal sheet BEFORE clicking.
5. Place the order yourself in TWS.

Mid-week emails only arrive when virtual positions close (the daily check
script). You don't need to do anything — they're just informational.

---

## File map

```
csp_screener/
├── config.py            # ALL thresholds + risk rules (locked by 14-day cooldown)
├── universe.py          # ~100 hardcoded liquid US tickers under $25
├── data_pipeline.py     # yfinance price/volume fetcher with cache
├── filters.py           # price, volume, earnings, exclusion
├── ranker.py            # realized vol percentile
├── earnings.py          # Finnhub + yfinance fallback
├── options_data.py      # IBKR primary + yfinance fallback
├── setup_generator.py   # picks specific put strike/expiry for each candidate
├── virtual_tracker.py   # opens/closes virtual trades, BS re-pricing
├── evaluator.py         # screener accuracy metrics
├── journal.py           # append-only JSONL writer
├── deadman.py           # healthchecks.io ping
├── notify.py            # Gmail SMTP HTML emails
├── main.py              # Sunday orchestrator (run weekly)
├── daily_check.py       # Mon-Fri virtual position updates
├── tests/               # 35 pytest unit tests
└── output/              # journal, logs, email previews, cache
```

---

## Hard rules (anti-loss insurance)

These live in `config.py` and are enforced everywhere:

| Rule | Value |
|---|---|
| Max open positions | 2 |
| Max total premium at risk | $200 |
| Min open interest | 500 |
| Max bid/ask spread | 5% of mid |
| Min daily volume | 1M shares |
| Price band | $5 – $25 |
| Never hold through earnings | Yes (14-day exclusion) |
| VIX kill switch | Above 35 → screener returns nothing |
| Account drawdown kill switch | 20% halts new positions |
| Threshold change cooldown | 14 days (anti-tinker) |

---

## What the screener is NOT trying to do

- **It's not trying to make you rich.** $1K account, year 1 honest range: −$300 to +$100.
- **It's not predicting "the right trade at the right time."** It's surfacing
  reasonable candidates so you can practice picking.
- **It's not a substitute for actually opening the chain in TWS and looking.**
  The data sources are free, which means they're noisy.

The point of the system: build a real performance record over months so you
know if the strategy works, without blowing up $1K while you find out.

---

## Why the virtual tracker matters

Even if you take zero real trades for 6 months, the virtual tracker quietly
builds the answer to: **"If I had taken every weekly suggestion, would I have
made money?"**

After 30+ closed virtual trades, the answer is statistically meaningful.
Before that, anything you see is noise.

---

## Updating thresholds

DON'T. At least not for 14 days after the last edit. The pre-commit hook
will reject your commit. This is intentional. The week-8 urge to "improve"
based on noise is the most reliable way to destroy a working system.

If after 60+ closed virtual trades you have a clear hypothesis backed by
data (not vibes), edit one threshold at a time, wait 14 more days, measure.
