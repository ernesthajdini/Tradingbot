-- ===========================================================================
-- REALTIME — enable live change events for the dashboard.
-- Run once in the Supabase SQL Editor. Idempotent.
-- ===========================================================================
-- Adds the three tables to Supabase's realtime publication so the dashboard
-- receives a websocket event whenever the screener writes, and refreshes
-- itself without a manual reload. RLS still applies: only authenticated
-- users receive events.
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                   WHERE pubname = 'supabase_realtime'
                     AND schemaname = 'public' AND tablename = 'screens') THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.screens;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                   WHERE pubname = 'supabase_realtime'
                     AND schemaname = 'public' AND tablename = 'virtual_trades') THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.virtual_trades;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                   WHERE pubname = 'supabase_realtime'
                     AND schemaname = 'public' AND tablename = 'system_events') THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.system_events;
    END IF;
END$$;

-- Verify:
--   SELECT tablename FROM pg_publication_tables WHERE pubname = 'supabase_realtime';
-- Expect: screens, virtual_trades, system_events
