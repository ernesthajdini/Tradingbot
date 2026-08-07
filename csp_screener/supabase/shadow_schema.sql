-- ===========================================================================
-- CSP Screener — SHADOW BOOK schema (LEARNING_ACCELERATION_STUDY.md)
-- ===========================================================================
-- Run once in the Supabase SQL Editor (idempotent — safe to re-run).
--
-- WHY A SEPARATE TABLE: golive.gate_status() and the evaluator pool every
-- close in virtual_trades. Shadow (counterfactual) trades in that table
-- would corrupt BOTH the 200-trade go-live counter and the market-marked
-- share — in either direction. Separate table = structurally impossible.
-- The Python side fails soft until this table exists (records still land
-- in the local JSONL journal, but stateless CI runners need this table
-- for shadow state to survive between runs).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.shadow_trades (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL CHECK (event IN ('open', 'close')),
    trade_id TEXT NOT NULL,
    screen_id TEXT,
    ticker TEXT NOT NULL,
    strike NUMERIC(10, 4),
    expiration DATE,
    -- Open event fields (mirrors virtual_trades)
    opened_at TIMESTAMPTZ,
    spot_at_open NUMERIC(10, 4),
    dte_at_open INTEGER,
    credit_received NUMERIC(12, 2),
    max_loss NUMERIC(12, 2),
    breakeven NUMERIC(10, 4),
    delta_at_open NUMERIC(8, 4),
    iv_at_open NUMERIC(8, 4),
    rv_percentile_at_open NUMERIC(8, 2),
    vix_at_open NUMERIC(8, 2),
    data_quality TEXT,
    -- Shadow-specific fields
    shadow_reason TEXT,                  -- 'rank_cutoff' | 'earnings_blackout'
    rank_at_open INTEGER,                -- position in the FULL ranking
    next_earnings_days_at_open INTEGER,  -- earnings cohort: distance at open
    -- Close event fields (mirrors virtual_trades)
    closed_at TIMESTAMPTZ,
    exit_reason TEXT,
    exit_spot NUMERIC(10, 4),
    final_put_price NUMERIC(12, 4),
    pnl NUMERIC(12, 2),
    pnl_gross NUMERIC(12, 2),
    friction NUMERIC(12, 2),
    pnl_pessimistic NUMERIC(12, 2),
    eur_usd_rate NUMERIC(10, 4),
    pnl_eur NUMERIC(12, 2),
    structure TEXT DEFAULT 'csp',
    tier TEXT DEFAULT 'sandbox',
    long_strike NUMERIC(10, 4),
    pnl_pct_of_credit NUMERIC(8, 4),
    notes TEXT,
    -- Bookkeeping
    record_hash TEXT,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (event, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_st_trade_id ON public.shadow_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_st_event ON public.shadow_trades(event);
CREATE INDEX IF NOT EXISTS idx_st_ticker ON public.shadow_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_st_reason ON public.shadow_trades(shadow_reason);

-- Same RLS posture as the production tables: public read, service-key write.
ALTER TABLE public.shadow_trades ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read shadow_trades" ON public.shadow_trades;
CREATE POLICY "Public read shadow_trades" ON public.shadow_trades
    FOR SELECT USING (true);

-- ===========================================================================
-- Verify with:
--   SELECT count(*) FROM public.shadow_trades;
-- ===========================================================================
