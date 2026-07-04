# Cloud Migration Path (Option B)

You don't need this yet. Local Streamlit + Cloudflare Tunnel covers 99% of use
cases for free. Only migrate when you genuinely outgrow it (multi-user,
24/7 access without your PC on, etc).

This doc shows the path so you (or a future agent) don't have to figure it
out from scratch.

## Target architecture

```
[Your PC]                              [Cloud]
  Screener (local) ──── nightly sync ──► Supabase Postgres
  IBKR TWS                                    ▲
  Email alerts                                │
                                              │ read
                                              │
                              Streamlit Community Cloud
                              (deployed from GitHub)
                              https://your-app.streamlit.app
```

**All free tier:**
- Supabase: 500 MB Postgres, 2 GB bandwidth/mo, plenty for years of journal
- Streamlit Cloud: unlimited public apps
- GitHub: free for public repos

## Migration steps

### 1. Supabase setup (15 min)

1. Sign up at https://supabase.com (free)
2. Create new project (any region, but match where you mostly browse from)
3. In the SQL editor, create these tables:

```sql
CREATE TABLE screens (
    id BIGSERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    payload JSONB NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE INDEX idx_screens_recorded_at ON screens(recorded_at);

CREATE TABLE virtual_trades (
    id BIGSERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    trade_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload JSONB NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE INDEX idx_virtual_trade_id ON virtual_trades(trade_id);
CREATE INDEX idx_virtual_event ON virtual_trades(event);

CREATE TABLE system_events (
    id BIGSERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    payload JSONB NOT NULL
);

CREATE TABLE evaluations (
    id BIGSERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    period TEXT,
    payload JSONB NOT NULL
);
```

4. Copy your Supabase URL and `anon` key from Settings → API.

### 2. Sync script (one new file: `csp_screener/sync_to_cloud.py`)

```python
import os, json
from datetime import datetime
from supabase import create_client
from csp_screener import config, journal

client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

def sync_topic(topic: str):
    # Idempotent: query max recorded_at in DB, upload only newer records.
    last = client.table(topic).select("recorded_at").order(
        "recorded_at", desc=True).limit(1).execute()
    cutoff = last.data[0]["recorded_at"] if last.data else "1970-01-01"
    records = journal.read_all(topic)
    to_upload = [r for r in records if r["recorded_at"] > cutoff]
    if to_upload:
        client.table(topic).insert([
            {"payload": r, "record_hash": r.get("record_hash", "")}
            for r in to_upload
        ]).execute()
    return len(to_upload)

if __name__ == "__main__":
    for topic in ("screens", "virtual_trades", "system_events"):
        print(f"{topic}: synced {sync_topic(topic)} new records")
```

Run as a nightly Windows scheduled task: `schtasks /Create ... /SC DAILY /ST 23:00`.

### 3. Cloud dashboard (fork of `dashboard/app.py`)

Create `dashboard/cloud_app.py` that reads from Supabase instead of journal:

```python
import os
from supabase import create_client
import streamlit as st

@st.cache_data(ttl=60)
def load_screens():
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    res = client.table("screens").select("*").order(
        "recorded_at", desc=False).limit(500).execute()
    return [r["payload"] for r in res.data]

# ... rest of app.py logic, just swap journal.read_all() for Supabase queries
```

### 4. Deploy to Streamlit Community Cloud

1. Push your repo to GitHub (only the `dashboard/` folder + supabase deps)
2. Go to https://share.streamlit.io
3. Connect GitHub, point at your repo, file = `dashboard/cloud_app.py`
4. In app secrets, add `SUPABASE_URL` and `SUPABASE_KEY`
5. Deploy — you get `https://your-username-your-app.streamlit.app`

### 5. Add auth (optional, free)

Streamlit Cloud has built-in OAuth via `st.user`. Restrict to your email
in app settings → Sharing → "Only specific people."

## Estimated effort

- Total work: 3–4 hours
- Total cost: $0/month indefinitely (within free tiers)

## When to actually do this

- ✅ You're checking the dashboard from your phone multiple times a day
- ✅ You want family / partner to see it without VPN
- ✅ You're traveling and your PC at home is unreliable
- ✅ You have 30+ closed virtual trades and want to share the track record
- ❌ It's been less than 30 days since the screener started running
- ❌ You're tempted by it because cloud sounds cool (the worst reason)
