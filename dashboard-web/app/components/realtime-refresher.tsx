'use client';

/**
 * Live-update engine for the dashboard.
 *
 * Primary: Supabase Realtime websocket — any INSERT/UPDATE on screens,
 * virtual_trades, or system_events triggers a debounced router.refresh(),
 * re-rendering the server components with fresh data.
 *
 * Fallback: 60-second polling refresh, so the page stays current even if
 * the websocket can't connect (corporate proxies, flaky mobile networks).
 *
 * Renders a small status pill: green "Live" when the socket is subscribed,
 * grey "Polling" otherwise, plus the last-update time.
 */

import { useEffect, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { createClient } from '@/lib/supabase/client';

const WATCHED_TABLES = ['screens', 'virtual_trades', 'system_events'];

export function RealtimeRefresher() {
  const router = useRouter();
  const pathname = usePathname();
  const [connected, setConnected] = useState(false);
  const [misconfigured, setMisconfigured] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<string>('');
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (pathname.startsWith('/login')) return;

    // createClient() throws on missing NEXT_PUBLIC_SUPABASE_* env vars. This
    // component is mounted from the root layout, so an uncaught throw here
    // white-screens EVERY page with "Application error" — including pages
    // that need no data at all, like /research. Degrade instead: keep the
    // polling refresh, and say so in the pill rather than dying silently.
    let supabase: ReturnType<typeof createClient> | null = null;
    try {
      supabase = createClient();
    } catch {
      setMisconfigured(true);
    }

    const refresh = () => {
      // Debounce: a screen write inserts many rows in quick succession —
      // collapse them into one refresh.
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        setLastUpdate(new Date().toLocaleTimeString());
        router.refresh();
      }, 800);
    };

    let channel: ReturnType<NonNullable<typeof supabase>['channel']> | null = null;
    if (supabase) {
      channel = supabase.channel('dashboard-live');
      for (const table of WATCHED_TABLES) {
        channel = channel.on(
          'postgres_changes',
          { event: '*', schema: 'public', table },
          refresh
        );
      }
      channel.subscribe((status) => {
        setConnected(status === 'SUBSCRIBED');
      });
    }

    // Fallback polling — keeps data at most 60s stale even without websocket
    const interval = setInterval(() => {
      setLastUpdate(new Date().toLocaleTimeString());
      router.refresh();
    }, 60_000);

    return () => {
      if (supabase && channel) supabase.removeChannel(channel);
      clearInterval(interval);
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  if (pathname.startsWith('/login')) return null;

  return (
    <div className="fixed bottom-3 right-3 z-50 flex items-center gap-1.5
                    bg-panel/90 backdrop-blur border border-border rounded-full
                    px-3 py-1 text-[11px] text-muted select-none">
      <span
        className={`inline-block w-2 h-2 rounded-full ${
          misconfigured ? 'bg-danger'
            : connected ? 'bg-success animate-pulse' : 'bg-muted'
        }`}
      />
      {misconfigured
        ? <span className="text-danger" title="NEXT_PUBLIC_SUPABASE_URL / _ANON_KEY not set">
            Supabase not configured
          </span>
        : connected ? 'Live' : 'Polling'}
      {!misconfigured && lastUpdate && (
        <span className="opacity-60">· {lastUpdate}</span>
      )}
    </div>
  );
}
