-- ===========================================================================
-- AUTH LOCKDOWN — run this AFTER schema.sql, when enabling the login wall.
-- ===========================================================================
-- Flips the dashboard data from public-read to authenticated-only.
-- Without this, the /login page is decoration: anyone with the anon key
-- (which ships in the dashboard's JS bundle) could query the tables directly.
--
-- Idempotent — safe to re-run.
-- ===========================================================================

-- 1. Replace public-read policies with authenticated-only
DROP POLICY IF EXISTS "Public read screens" ON public.screens;
DROP POLICY IF EXISTS "Authenticated read screens" ON public.screens;
CREATE POLICY "Authenticated read screens" ON public.screens
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "Public read virtual_trades" ON public.virtual_trades;
DROP POLICY IF EXISTS "Authenticated read virtual_trades" ON public.virtual_trades;
CREATE POLICY "Authenticated read virtual_trades" ON public.virtual_trades
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "Public read system_events" ON public.system_events;
DROP POLICY IF EXISTS "Authenticated read system_events" ON public.system_events;
CREATE POLICY "Authenticated read system_events" ON public.system_events
    FOR SELECT TO authenticated USING (true);

-- 2. CRITICAL: make the views respect RLS.
-- Postgres views execute with the OWNER's privileges by default, which
-- BYPASSES row-level security — meaning the views would stay publicly
-- readable even after step 1. security_invoker makes them run with the
-- caller's privileges instead.
ALTER VIEW public.open_virtual_trades SET (security_invoker = true);
ALTER VIEW public.closed_virtual_trades SET (security_invoker = true);

-- 3. Verify (both should return 0 rows when run with the anon key):
--    SELECT count(*) FROM public.screens;
--    SELECT count(*) FROM public.closed_virtual_trades;
-- In the SQL editor you're postgres (superuser) so you'll still see rows —
-- the real test is the dashboard redirecting to /login when signed out.
-- ===========================================================================
