# Cloud Deployment — Full Stack Setup

Run the entire CSP screener stack in the cloud for $0/month.

## What you'll have when done

- **GitHub Actions** runs the screener every Sunday + daily Mon-Fri (free, no PC needed)
- **Supabase** stores all journal data in Postgres (free, 500 MB)
- **Vercel** hosts the dashboard at `https://your-app.vercel.app` (free)
- **Gmail SMTP** sends weekly emails (free)

Your PC is no longer required.

## Setup checklist (~30 minutes one-time)

### 1. Supabase project (5 min)

- [ ] Sign up at https://supabase.com (free)
- [ ] Create new project (any region — pick close to you and Vercel region)
- [ ] Wait ~1 minute for Postgres to provision
- [ ] In SQL Editor, paste contents of `csp_screener/supabase/schema.sql` and run
- [ ] Settings → API → copy:
  - **Project URL** → save as `SUPABASE_URL`
  - **anon public key** → save as `SUPABASE_ANON_KEY`
  - **service_role secret** → save as `SUPABASE_SERVICE_KEY`

### 2. GitHub repo + secrets (10 min)

- [ ] Push this repo to GitHub (private is fine)
- [ ] Repo Settings → Secrets and variables → Actions → New repository secret:
  | Name | Value |
  |---|---|
  | `SUPABASE_URL` | from step 1 |
  | `SUPABASE_SERVICE_KEY` | from step 1 (the secret one) |
  | `FINNHUB_API_KEY` | free at https://finnhub.io/register |
  | `SMTP_USER` | your gmail address |
  | `SMTP_PASSWORD` | 16-char [Gmail App Password](https://myaccount.google.com/apppasswords) |
  | `SMTP_TO` | where alerts go (usually same as SMTP_USER) |
  | `HEALTHCHECKS_PING_URL` | optional — free at https://healthchecks.io |
  | `HEALTHCHECKS_PING_URL_DAILY` | optional |

- [ ] Settings → Actions → General → Workflow permissions = "Read and write" (so artifacts work)

### 3. First manual test (5 min)

- [ ] Actions tab → "CSP Screener — Sunday weekly run" → Run workflow
- [ ] Wait ~5 min, verify:
  - Green checkmark
  - Email arrived in your Gmail
  - Supabase tables have rows (check in Supabase Table Editor)

If it failed: check the run logs. Most common issue is a missing/typo'd secret.

### 4. Vercel deploy (10 min)

- [ ] https://vercel.com/new → import your GitHub repo
- [ ] **Root Directory: `dashboard-web`** (important!)
- [ ] Environment Variables:
  | Name | Value |
  |---|---|
  | `NEXT_PUBLIC_SUPABASE_URL` | from step 1 (the URL) |
  | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | from step 1 (the anon key, NOT service_role) |
- [ ] Deploy
- [ ] Open the URL — you should see your dashboard with data from step 3

### 5. (Optional) Custom domain

- [ ] Vercel → Settings → Domains → Add domain
- [ ] Update DNS at your registrar per Vercel's instructions

## After setup

The screener now runs on its own schedule. You can:

- **Check dashboard anytime** at your Vercel URL (works on phone)
- **Read weekly emails** in Gmail
- **Trigger an extra run** via GitHub Actions → workflow_dispatch
- **Backfill from local JSONL** (if you ran the screener locally first):
  - Actions tab → "manual backfill" → Run with confirm = `YES`

## Cost monitoring

All providers have generous free tiers. Honest math for this use case:

- **Vercel**: ~10 page views/day × 30 days = 300 visits. Free limit: 100K. Fine.
- **Supabase**: ~50 KB/week of new data. Free limit: 500 MB = years.
- **GitHub Actions**: ~3 min × 6 runs/week = 18 min/week. Free private limit: 2,000 min/month.
- **Gmail SMTP**: 500 emails/day free. We send ~2/week.

You will never hit any limit at single-user scale.

## What happens if a service dies

- **GitHub Actions outage**: missed weekly run. Healthchecks.io emails you. Next week's run picks up.
- **Supabase outage**: dashboard shows empty / errors. Read GitHub Action artifacts for the JSONL.
- **Vercel outage**: dashboard down but data is safe in Supabase.
- **Gmail outage**: email skipped. Run is still logged in Supabase / artifacts.

Worst case for any single outage: you miss one weekly screen. No data loss.

## Files added in this deployment

```
TradingAgent/
├── csp_screener/
│   ├── supabase/schema.sql              ← Postgres schema
│   ├── supabase_sync.py                 ← Python dual-write
│   └── journal.py                       ← updated to call supabase_sync
├── .github/workflows/
│   ├── sunday-screener.yml              ← weekly cron
│   ├── daily-check.yml                  ← daily cron
│   └── backfill.yml                     ← manual backfill
└── dashboard-web/                       ← Next.js 15 app
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx                    ← Dashboard
    │   ├── candidates/page.tsx
    │   ├── portfolio/page.tsx
    │   ├── track-record/page.tsx
    │   ├── system/page.tsx
    │   ├── components/                 ← Nav, MetricCard, PnlChart
    │   └── globals.css
    ├── lib/                            ← Supabase client + queries + types
    ├── package.json
    ├── tsconfig.json
    ├── tailwind.config.ts
    ├── vercel.json
    └── README.md
```

## Trouble?

Each subsystem has its own README with specific troubleshooting:
- `csp_screener/README.md` — Python screener
- `dashboard-web/README.md` — Next.js app

If a workflow keeps failing, download the artifact from that run to see what
happened locally.
