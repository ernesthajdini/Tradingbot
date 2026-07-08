/**
 * All Supabase queries used by server components.
 * Each call builds a per-request server client that carries the user's
 * session cookie — required since reads are now authenticated-only.
 */
import { createClient } from './supabase/server';
import type {
  ClosedVirtualTrade, OpenVirtualTrade, Screen, SystemEvent,
} from './types';

type RunType = 'weekly' | 'daily';

/** Legacy rows predate run_type and are all weekly, hence the is.null arm. */
function withRunType(query: any, runType?: RunType) {
  if (runType === 'daily') return query.eq('run_type', 'daily');
  if (runType === 'weekly') return query.or('run_type.eq.weekly,run_type.is.null');
  return query;
}

export async function fetchLatestScreen(runType?: RunType): Promise<Screen | null> {
  const supabase = await createClient();
  const { data, error } = await withRunType(
    supabase.from('screens').select('*'), runType)
    .order('ran_at', { ascending: false })
    .limit(1)
    .maybeSingle();
  if (error) {
    console.error('fetchLatestScreen', error);
    return null;
  }
  return data as Screen | null;
}

export async function fetchRecentScreens(limit = 30, runType?: RunType): Promise<Screen[]> {
  const supabase = await createClient();
  const { data, error } = await withRunType(
    supabase.from('screens').select('*'), runType)
    .order('ran_at', { ascending: false })
    .limit(limit);
  if (error) {
    console.error('fetchRecentScreens', error);
    return [];
  }
  return (data || []) as Screen[];
}

export async function fetchOpenVirtualTrades(): Promise<OpenVirtualTrade[]> {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from('open_virtual_trades')
    .select('*')
    .order('opened_at', { ascending: false });
  if (error) {
    console.error('fetchOpenVirtualTrades', error);
    return [];
  }
  return (data || []) as OpenVirtualTrade[];
}

export async function fetchClosedVirtualTrades(limit = 500): Promise<ClosedVirtualTrade[]> {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from('closed_virtual_trades')
    .select('*')
    .order('closed_at', { ascending: false })
    .limit(limit);
  if (error) {
    console.error('fetchClosedVirtualTrades', error);
    return [];
  }
  return (data || []) as ClosedVirtualTrade[];
}

export async function fetchRecentSystemEvents(limit = 30): Promise<SystemEvent[]> {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from('system_events')
    .select('*')
    .order('at', { ascending: false })
    .limit(limit);
  if (error) {
    console.error('fetchRecentSystemEvents', error);
    return [];
  }
  return (data || []) as SystemEvent[];
}

/**
 * Compute performance metrics from closed trades, in-memory.
 * For a small account journal this is more than fast enough.
 */
export function computePerformance(trades: ClosedVirtualTrade[]) {
  if (trades.length === 0) {
    return {
      count: 0, wins: 0, losses: 0, winRate: 0,
      totalPnl: 0, avgPnl: 0, avgWin: 0, avgLoss: 0,
      profitFactor: 0, expectancy: 0,
      best: 0, worst: 0,
      byExitReason: {} as Record<string, { count: number; pnl: number }>,
      byTicker: {} as Record<string, { trades: number; wins: number; pnl: number }>,
    };
  }
  const pnls = trades.map(t => Number(t.pnl) || 0);
  const wins = pnls.filter(p => p > 0);
  const losses = pnls.filter(p => p <= 0);
  const grossProfit = wins.reduce((a, b) => a + b, 0);
  const grossLoss = Math.abs(losses.reduce((a, b) => a + b, 0));
  const totalPnl = pnls.reduce((a, b) => a + b, 0);

  const byExit: Record<string, { count: number; pnl: number }> = {};
  const byTicker: Record<string, { trades: number; wins: number; pnl: number }> = {};
  for (const t of trades) {
    const r = t.exit_reason || 'unknown';
    byExit[r] = byExit[r] || { count: 0, pnl: 0 };
    byExit[r].count++; byExit[r].pnl += Number(t.pnl) || 0;
    const tk = t.ticker;
    byTicker[tk] = byTicker[tk] || { trades: 0, wins: 0, pnl: 0 };
    byTicker[tk].trades++;
    if ((Number(t.pnl) || 0) > 0) byTicker[tk].wins++;
    byTicker[tk].pnl += Number(t.pnl) || 0;
  }

  const winRate = wins.length / trades.length;
  const avgWin = wins.length ? wins.reduce((a, b) => a + b, 0) / wins.length : 0;
  const avgLoss = losses.length ? losses.reduce((a, b) => a + b, 0) / losses.length : 0;

  return {
    count: trades.length,
    wins: wins.length,
    losses: losses.length,
    winRate,
    totalPnl,
    avgPnl: totalPnl / trades.length,
    avgWin,
    avgLoss,
    profitFactor: grossLoss > 0 ? grossProfit / grossLoss : (grossProfit > 0 ? Infinity : 0),
    expectancy: winRate * avgWin + (1 - winRate) * avgLoss,
    best: Math.max(...pnls),
    worst: Math.min(...pnls),
    byExitReason: byExit,
    byTicker,
  };
}

export function filterTradesByDays(trades: ClosedVirtualTrade[], days: number | null): ClosedVirtualTrade[] {
  if (!days) return trades;
  const cutoff = Date.now() - days * 86_400_000;
  return trades.filter(t => new Date(t.closed_at).getTime() >= cutoff);
}

/**
 * Compute per-ticker stats with Bayesian shrinkage — matches Python ticker_scorer.
 * Keeping it in TS lets the dashboard surface live insights without an extra
 * round trip through Python.
 */
const PRIOR_TRADES = 5;
const PRIOR_WIN_RATE = 0.5;
const TICKER_MIN_TRADES = 3;

export interface TickerScore {
  ticker: string;
  trades: number;
  wins: number;
  losses: number;
  rawWinRate: number;
  shrunkWinRate: number;
  avgPnl: number;
  totalPnl: number;
  score: number;
  sampleQuality: 'thin' | 'moderate' | 'robust';
}

export function scoreTicker(ticker: string, trades: ClosedVirtualTrade[]): TickerScore {
  const relevant = trades.filter(t => t.ticker === ticker);
  const pnls = relevant.map(t => Number(t.pnl) || 0);
  const wins = pnls.filter(p => p > 0).length;
  const losses = pnls.filter(p => p <= 0).length;
  const n = pnls.length;
  const total = pnls.reduce((a, b) => a + b, 0);
  const rawWr = n > 0 ? wins / n : 0;
  const shrunkWr = (wins + PRIOR_TRADES * PRIOR_WIN_RATE) / (n + PRIOR_TRADES);
  const avg = n > 0 ? total / n : 0;
  let quality: 'thin' | 'moderate' | 'robust' = 'thin';
  if (n >= 10) quality = 'robust';
  else if (n >= TICKER_MIN_TRADES) quality = 'moderate';

  let score = 1.0;
  if (n >= TICKER_MIN_TRADES) {
    score = 1.0 + ((shrunkWr - 0.5) / 0.5) * 0.2;
    if (avg < 0) score *= 0.9;
    score = Math.max(0.5, Math.min(1.2, score));
  }

  return {
    ticker, trades: n, wins, losses,
    rawWinRate: rawWr, shrunkWinRate: shrunkWr,
    avgPnl: avg, totalPnl: total,
    score, sampleQuality: quality,
  };
}

export function scoreAllTickers(trades: ClosedVirtualTrade[]): TickerScore[] {
  const tickers = new Set(trades.map(t => t.ticker));
  return Array.from(tickers).map(t => scoreTicker(t, trades));
}
