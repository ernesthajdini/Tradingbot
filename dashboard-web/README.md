# CSP Screener Dashboard (Web)

Next.js 15 + Tailwind + Supabase. Deploys to Vercel free tier.

Pairs with the Python `csp_screener` that runs on GitHub Actions and writes
to Supabase. This app is read-only — no real trades executed from here.

## Stack

| Layer | Tech | Cost |
|---|---|---|
| Hosting | Vercel | Free (Hobby) |
| Framework | Next.js 15 (App Router, RSC) | OSS |
| Styling | Tailwind CSS | OSS |
| Charts | Recharts | OSS |
| Data | Supabase Postgres | Free (500 MB) |
| Cron | GitHub Actions | Free (public repo) |

**Total monthly cost: $0** within free tiers.

## Pages

| Route | Purpose |
|---|---|
| `/` | Dashboard: top metrics, cumulative P&L chart, latest screen, open positions |
| `/candidates` | Latest screen's candidates with virtual setup details |
| `/portfolio` | All currently-open virtual positions, sorted by DTE |
| `/track-record` | 30d/90d/all-time stats, by-reason and by-ticker breakdowns |
| `/system` | Liveness, recent system events |

## Local development

```bash
cd dashboard-web
cp .env.local.example .env.local
# Fill in NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY
npm install
npm run dev
```

Open http://localhost:3000.

## Deploy to Vercel (one-time, ~5 minutes)

### 1. Push the repo to GitHub

```bash
# From the TradingAgent root
git add csp_screener/ dashboard-web/ .github/
git commit -m "Add CSP screener + cloud dashboard"
git push
```

(If `TradingAgent` isn't a git repo yet: `git init && git remote add origin <url>`.)

### 2. Import to Vercel

1. Go to https://vercel.com/new
2. Import your GitHub repo
3. Set **Root Directory** to `dashboard-web`
4. Framework Preset auto-detects Next.js
5. Add environment variables (Settings → Environment Variables):
   - `NEXT_PUBLIC_SUPABASE_URL` (your Supabase URL)
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY` (Supabase anon/public key)
6. Click **Deploy**

You'll get a URL like `https://csp-screener-dashboard.vercel.app` in ~60s.

### 3. (Optional) Custom domain

Vercel free tier supports unlimited custom domains. Settings → Domains, add
yours, set the DNS CNAME, done.

## Authentication options (optional, free)

The dashboard is **public by default**. Anyone with the URL can see your
virtual trade journal. If that bothers you:

- **Vercel Password Protection** (Pro plan, $20/mo) — single shared password
- **Cloudflare Access** — front the Vercel domain with Cloudflare, free tier
  supports Google/GitHub login restrictions
- **Supabase Auth + Next.js middleware** — proper user login (extra ~50 lines)

For most users with a small CSP journal, public is fine.

## Updating the dashboard

Vercel auto-deploys on `git push` to the connected branch. No manual deploy
needed. PRs get preview URLs automatically.

## How data flows

```
[GitHub Actions — Sunday 22:00 UTC]
   runs: python -m csp_screener.main --no-ibkr
   ├ scans 102-ticker universe via yfinance
   ├ writes JSONL + dual-writes to Supabase
   └ sends weekly email via Gmail SMTP

[GitHub Actions — Mon-Fri 21:30 UTC]
   runs: python -m csp_screener.daily_check
   ├ marks open virtual positions to market
   ├ closes any that hit exit rules
   └ writes events to Supabase

[Supabase Postgres]
   ├ screens
   ├ virtual_trades  (open / close events)
   ├ system_events
   └ views: open_virtual_trades, closed_virtual_trades

[Vercel — this app]
   ├ Server Components fetch from Supabase
   ├ Revalidate every 30-60 seconds
   └ Renders dashboard to user's browser
```

## Mobile

Designed for iPhone-sized screens. Add to home screen for one-tap access.

## Troubleshooting

**Pages show empty:** Supabase URL/key wrong, or no data yet. Check
`/system` for recent events, then re-run the GitHub Action manually.

**`supabase` build error:** the `NEXT_PUBLIC_*` env vars must be set at
build time, not just runtime. Re-deploy after adding them in Vercel
settings.

**Stale data:** Server Components cache for 30-60 seconds. Hard refresh
clears the browser cache; the server cache expires on its own.
