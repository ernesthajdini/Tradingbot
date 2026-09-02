-- ===========================================================================
-- CSP Screener — Supabase schema
-- ===========================================================================
-- Run this once in the Supabase SQL Editor on a fresh project.
-- Idempotent: safe to re-run; uses CREATE TABLE IF NOT EXISTS everywhere.
--
-- After running this, set these env vars in your GitHub repo and Vercel:
--   SUPABASE_URL          — https://<project>.supabase.co
--   SUPABASE_SERVICE_KEY  — for the Python screener (writes)
--   SUPABASE_ANON_KEY     — for the Next.js dashboard (reads only)
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- screens — one row per weekly screener run
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.screens (
    id BIGSERIAL PRIMARY KEY,
    screen_id TEXT UNIQUE NOT NULL,
    run_type TEXT DEFAULT 'weekly',    -- 'weekly' | 'daily'; NULL = weekly (legacy)
    ran_at TIMESTAMPTZ NOT NULL,
    universe_size INTEGER,
    passed_filters INTEGER,
    candidates_in_email INTEGER,
    virtual_positions_opened INTEGER,
    virtual_closed_this_run INTEGER,
    virtual_closed_pnl NUMERIC(12, 2),
    vix NUMERIC(8, 2),
    email_sent BOOLEAN DEFAULT FALSE,
    tickers_in_email TEXT[],            -- array of ticker symbols
    candidates_payload JSONB,           -- full candidate details with setups
    summaries_payload JSONB,            -- evaluator summaries snapshot
    recommendations_payload JSONB,      -- learning layer's recommendations
    record_hash TEXT,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_screens_ran_at ON public.screens(ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_screens_screen_id ON public.screens(screen_id);


-- ---------------------------------------------------------------------------
-- virtual_trades — event log (open / close events)
-- ---------------------------------------------------------------------------
-- This is the append-only event store. Trade state is reconstructed by
-- replaying open + close events keyed by trade_id.
CREATE TABLE IF NOT EXISTS public.virtual_trades (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL CHECK (event IN ('open', 'close')),
    trade_id TEXT NOT NULL,
    screen_id TEXT,
    ticker TEXT NOT NULL,
    strike NUMERIC(10, 4),
    expiration DATE,
    -- Open event fields
    opened_at TIMESTAMPTZ,
    spot_at_open NUMERIC(10, 4),
    dte_at_open INTEGER,
    credit_received NUMERIC(12, 2),
    max_loss NUMERIC(12, 2),
    breakeven NUMERIC(10, 4),
    delta_at_open NUMERIC(8, 4),
    iv_at_open NUMERIC(8, 4),
    rv_percentile_at_open NUMERIC(8, 2),  -- entry context for the learning layer
    vix_at_open NUMERIC(8, 2),            -- entry context for the learning layer
    data_quality TEXT,
    -- Close event fields
    closed_at TIMESTAMPTZ,
    exit_reason TEXT,
    exit_spot NUMERIC(10, 4),
    final_put_price NUMERIC(12, 4),
    pnl NUMERIC(12, 2),               -- NET of commission + slippage (base)
    pnl_gross NUMERIC(12, 2),         -- frictionless mark
    friction NUMERIC(12, 2),          -- commissions + slippage charged (base)
    pnl_pessimistic NUMERIC(12, 2),   -- NET at pessimistic slippage band
    eur_usd_rate NUMERIC(10, 4),      -- EURUSD at close
    pnl_eur NUMERIC(12, 2),           -- net PnL in EUR (the real scoreboard)
    structure TEXT DEFAULT 'csp',     -- 'csp' | 'put_credit_spread'
    tier TEXT DEFAULT 'sandbox',      -- 'live' | 'sandbox'
    long_strike NUMERIC(10, 4),       -- spread long leg
    pnl_pct_of_credit NUMERIC(8, 4),
    notes TEXT,
    -- Bookkeeping
    record_hash TEXT,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    -- Soft uniqueness for idempotency: (event, trade_id) is the natural key
    UNIQUE (event, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_vt_trade_id ON public.virtual_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_vt_event ON public.virtual_trades(event);
CREATE INDEX IF NOT EXISTS idx_vt_ticker ON public.virtual_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_vt_closed_at ON public.virtual_trades(closed_at DESC NULLS LAST);


-- ---------------------------------------------------------------------------
-- system_events — daily checks, failures, anything noteworthy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.system_events (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    payload JSONB,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_se_at ON public.system_events(at DESC);
CREATE INDEX IF NOT EXISTS idx_se_event ON public.system_events(event);


-- ---------------------------------------------------------------------------
-- Migrations for pre-existing installs (MUST run before the views below,
-- which reference the newer columns). All idempotent.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    col RECORD;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='screens'
                     AND column_name='recommendations_payload') THEN
        ALTER TABLE public.screens ADD COLUMN recommendations_payload JSONB;
    END IF;
    -- Two-tier / playbook columns on screens
    FOR col IN SELECT * FROM (VALUES
        ('live_viable', 'INTEGER'),
        ('no_trade_week', 'BOOLEAN'),
        ('post_spike_window', 'BOOLEAN'),
        ('fomc_days', 'INTEGER'),
        ('eur_usd_rate', 'NUMERIC(10,4)'),
        -- 'weekly' (Sunday digest run) or 'daily' (weekday indications run).
        -- NULL on legacy rows means 'weekly'.
        ('run_type', 'TEXT')
    ) AS t(name, type)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema='public' AND table_name='screens'
                         AND column_name=col.name) THEN
            EXECUTE format('ALTER TABLE public.screens ADD COLUMN %I %s',
                           col.name, col.type);
        END IF;
    END LOOP;
    -- Friction + two-tier columns on virtual_trades
    FOR col IN SELECT * FROM (VALUES
        ('pnl_gross', 'NUMERIC(12,2)'),
        ('friction', 'NUMERIC(12,2)'),
        ('pnl_pessimistic', 'NUMERIC(12,2)'),
        ('eur_usd_rate', 'NUMERIC(10,4)'),
        ('pnl_eur', 'NUMERIC(12,2)'),
        ('structure', 'TEXT'),
        ('tier', 'TEXT'),
        ('long_strike', 'NUMERIC(10,4)'),
        ('rv_percentile_at_open', 'NUMERIC(8,2)'),
        ('vix_at_open', 'NUMERIC(8,2)')
    ) AS t(name, type)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema='public' AND table_name='virtual_trades'
                         AND column_name=col.name) THEN
            EXECUTE format('ALTER TABLE public.virtual_trades ADD COLUMN %I %s',
                           col.name, col.type);
        END IF;
    END LOOP;
END$$;


-- ---------------------------------------------------------------------------
-- VIEW: open_virtual_trades — replay event log to get currently-open trades
-- ---------------------------------------------------------------------------
-- The dashboard queries this view instead of doing the replay client-side.
-- NOTE: views are DROPPED and recreated (CREATE OR REPLACE cannot change a
-- view's column list), and security_invoker is re-applied immediately after
-- so re-running this file never silently reopens public data access.
DROP VIEW IF EXISTS public.open_virtual_trades;
CREATE VIEW public.open_virtual_trades AS
SELECT o.*
FROM public.virtual_trades o
WHERE o.event = 'open'
  AND NOT EXISTS (
      SELECT 1 FROM public.virtual_trades c
      WHERE c.event = 'close' AND c.trade_id = o.trade_id
  );


ALTER VIEW public.open_virtual_trades SET (security_invoker = true);


-- ---------------------------------------------------------------------------
-- VIEW: closed_virtual_trades — joined open+close for analytics
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS public.closed_virtual_trades;
CREATE VIEW public.closed_virtual_trades AS
SELECT
    c.trade_id,
    c.ticker,
    c.strike,
    c.expiration,
    o.opened_at,
    c.closed_at,
    EXTRACT(EPOCH FROM (c.closed_at - o.opened_at))/86400 AS days_held,
    o.spot_at_open,
    c.exit_spot,
    o.dte_at_open,
    o.credit_received,
    o.max_loss,
    o.breakeven,
    o.delta_at_open,
    o.iv_at_open,
    o.rv_percentile_at_open,
    o.vix_at_open,
    o.data_quality,
    c.exit_reason,
    c.final_put_price,
    c.pnl,
    c.pnl_gross,
    c.friction,
    c.pnl_pessimistic,
    c.eur_usd_rate,
    c.pnl_eur,
    COALESCE(c.structure, o.structure, 'csp') AS structure,
    COALESCE(c.tier, o.tier, 'sandbox') AS tier,
    COALESCE(c.long_strike, o.long_strike) AS long_strike,
    c.pnl_pct_of_credit,
    c.notes
FROM public.virtual_trades o
JOIN public.virtual_trades c USING (trade_id)
WHERE o.event = 'open' AND c.event = 'close';

ALTER VIEW public.closed_virtual_trades SET (security_invoker = true);


-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
-- Enable RLS on all tables. Dashboard uses ANON_KEY (read-only via policies).
-- Screener uses SERVICE_KEY which bypasses RLS.
ALTER TABLE public.screens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.virtual_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.system_events ENABLE ROW LEVEL SECURITY;

-- Public READ policies (dashboard works without auth)
DROP POLICY IF EXISTS "Public read screens" ON public.screens;
CREATE POLICY "Public read screens" ON public.screens
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read virtual_trades" ON public.virtual_trades;
CREATE POLICY "Public read virtual_trades" ON public.virtual_trades
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read system_events" ON public.system_events;
CREATE POLICY "Public read system_events" ON public.system_events
    FOR SELECT USING (true);

-- Writes blocked for anon (only service role can write)


-- ---------------------------------------------------------------------------
-- Helper RPC: latest_screen — returns most recent screen as JSON
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.latest_screen()
RETURNS SETOF public.screens AS $$
    SELECT * FROM public.screens ORDER BY ran_at DESC LIMIT 1;
$$ LANGUAGE sql STABLE;


-- ---------------------------------------------------------------------------
-- Done.
-- ---------------------------------------------------------------------------
-- Verify with:
--   SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
--   SELECT count(*) FROM public.screens;
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- paper_orders: what the IBKR PAPER account did with each paper trade.
-- Written only by csp_screener/paper_broker.py (runs locally, next to TWS).
-- Separate from virtual_trades on purpose: the go-live gate never reads it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_orders (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL CHECK (event IN ('submitted','repriced','filled','cancelled','rejected')),
    trade_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('open','close')),
    ticker TEXT,
    structure TEXT,
    expiration DATE,
    strike NUMERIC(10, 4),
    long_strike NUMERIC(10, 4),
    qty INTEGER,
    limit_per_share NUMERIC(10, 4),        -- IBKR sign convention (bags negative = credit)
    journal_price_dollars NUMERIC(12, 2),  -- what the paper journal recorded
    ibkr_order_id BIGINT DEFAULT 0,
    ibkr_perm_id BIGINT,
    account TEXT,                          -- always DU… (paper) by construction
    fill_per_share NUMERIC(10, 4),
    fill_dollars NUMERIC(12, 2),
    commission NUMERIC(10, 4),
    slippage_dollars NUMERIC(12, 2),       -- positive = broker worse than paper
    status_text TEXT,
    at TIMESTAMPTZ,
    record_hash TEXT,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (trade_id, side, event, ibkr_order_id)
);
CREATE INDEX IF NOT EXISTS idx_po_trade ON public.paper_orders(trade_id);
