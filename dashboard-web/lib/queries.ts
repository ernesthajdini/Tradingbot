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

/**
 * Credit-sanity quarantine — mirrors csp_screener/sanity.py exactly.
 * Trades opened on garbage quotes (the LCID incident: deep-OTM delta with a
 * deep-ITM premium from a misaligned yfinance row) are excluded from every
 * dashboard read. Journal/DB rows are never mutated.
 */
const MAX_CSP_CREDIT_FRAC_OF_STRIKE = 0.20;
const DEEP_OTM_DELTA = 0.15;
const MAX_DEEP_OTM_CREDIT_FRAC = 0.08;
const MAX_SPREAD_CREDIT_FRAC_OF_WIDTH = 0.9;

function tradeIsSane(t: {
  credit_received: number; strike: number;
  delta_at_open?: number | null; long_strike?: number | null; structure?: string | null;
}): boolean {
  const credit = Number(t.credit_received);
  const strike = Number(t.strike);
  if (!(strike > 0) || !(credit > 0)) return false;
  if ((t.structure === 'put_credit_spread') && t.long_strike != null) {
    const width = (strike - Number(t.long_strike)) * 100;
    return width > 0 && credit <= width * MAX_SPREAD_CREDIT_FRAC_OF_WIDTH;
  }
  const frac = credit / (strike * 100);
  if (frac > MAX_CSP_CREDIT_FRAC_OF_STRIKE) return false;
  const delta = t.delta_at_open != null ? Math.abs(Number(t.delta_at_open)) : null;
  if (delta != null && delta < DEEP_OTM_DELTA && frac > MAX_DEEP_OTM_CREDIT_FRAC) return false;
  return true;
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
  return ((data || []) as OpenVirtualTrade[]).filter(tradeIsSane);
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
  return ((data || []) as ClosedVirtualTrade[]).filter(tradeIsSane);
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
      totalPnl: 0, totalPnlPessimistic: 0, totalPnlEur: null as number | null,
      avgPnl: 0, avgWin: 0, avgLoss: 0,
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
  // Pessimistic-band total: paper fills contain no slippage information, so
  // the honest number is a band, not a point. Legacy rows without the band
  // fall back to base pnl (band collapses for them — no invented friction).
  const totalPnlPessimistic = trades.reduce(
    (a, t) => a + Number(t.pnl_pessimistic ?? t.pnl ?? 0), 0);
  // EUR scoreboard (playbook: the currency you eat with). Null when no
  // trade carries a EUR conversion yet.
  const eurVals = trades.map(t => t.pnl_eur).filter((v): v is number => v != null);
  const totalPnlEur = eurVals.length ? eurVals.reduce((a, b) => a + Number(b), 0) : null;

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
    totalPnlPessimistic,
    totalPnlEur,
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

/**
 * Was a close marked against a REAL option quote, or the Black-Scholes
 * fallback? Parsed from the close note (virtual_tracker writes
 * "…, mark_source=market|model, model_price=…"). Only market-marked
 * closes are evidence for the go-live decision — the model admits it
 * "prices profits too early".
 */
export function markSource(t: { notes?: string | null }): 'market' | 'model' | null {
  const m = /mark_source=(market|model)/.exec(t.notes ?? '');
  return (m?.[1] as 'market' | 'model') ?? null;
}

export function filterTradesByDays(trades: ClosedVirtualTrade[], days: number | null): ClosedVirtualTrade[] {
  if (!days) return trades;
  const cutoff = Date.now() - days * 86_400_000;
  return trades.filter(t => new Date(t.closed_at).getTime() >= cutoff);
}

/**
 * Compute per-ticker stats with Bayesian shrinkage — matches Python ticker_scorer.
 * Keeping it in TS lets the dashboard surface live insights without an extra
 * round trip through Python. If you change the formula here, change it in
 * csp_screener/learning/ticker_scorer.py too (and vice versa).
 */
const PRIOR_TRADES = 5;
const PRIOR_WIN_RATE = 0.5;
const TICKER_MIN_TRADES = 3;
const LOOKBACK_DAYS = 180; // mirrors ticker_scorer.LOOKBACK_DAYS
// Expectancy-primary scoring (mirrors ticker_scorer.py): PnL per unit of
// credit drives the score; win rate is a small secondary added ONLY on top
// of positive expectancy. Premium selling wins often and loses big — win
// rate alone is the wrong scoreboard.
const EXPECTANCY_SCALE = 0.5;
const WINRATE_SECONDARY_SCALE = 0.2;

export interface TickerScore {
  ticker: string;
  trades: number;
  wins: number;
  losses: number;
  rawWinRate: number;
  shrunkWinRate: number;
  avgPnl: number;
  totalPnl: number;
  avgPnlPct: number;
  shrunkExpectancy: number;
  score: number;
  sampleQuality: 'thin' | 'moderate' | 'robust';
}

export function scoreTicker(ticker: string, trades: ClosedVirtualTrade[]): TickerScore {
  const cutoff = Date.now() - LOOKBACK_DAYS * 86_400_000;
  const relevant = trades.filter(t => {
    if (t.ticker !== ticker) return false;
    const closedAt = new Date(t.closed_at).getTime();
    return isFinite(closedAt) ? closedAt >= cutoff : false;
  });
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

  // PnL as fraction of credit received (falls back to pnl/credit)
  const pnlPcts: number[] = [];
  for (const t of relevant) {
    let v: number | null = t.pnl_pct_of_credit != null ? Number(t.pnl_pct_of_credit) : null;
    if (v == null && t.credit_received && Number(t.credit_received) > 0 && t.pnl != null) {
      v = Number(t.pnl) / Number(t.credit_received);
    }
    if (v != null && isFinite(v)) pnlPcts.push(v);
  }
  const pctSum = pnlPcts.reduce((a, b) => a + b, 0);
  const avgPnlPct = pnlPcts.length ? pctSum / pnlPcts.length : 0;
  const shrunkExpectancy = pnlPcts.length ? pctSum / (pnlPcts.length + PRIOR_TRADES) : 0;

  let score = 1.0;
  if (n >= TICKER_MIN_TRADES) {
    if (pnlPcts.length >= TICKER_MIN_TRADES) {
      // Win rate only ever ADDS on top of positive expectancy
      const wrComponent = shrunkExpectancy > 0
        ? (shrunkWr - 0.5) * WINRATE_SECONDARY_SCALE : 0;
      score = 1.0 + shrunkExpectancy * EXPECTANCY_SCALE + wrComponent;
    } else {
      // Legacy fallback: no credit data — original win-rate-only mapping
      score = 1.0 + ((shrunkWr - 0.5) / 0.5) * 0.2;
      if (avg < 0) score *= 0.9;
    }
    score = Math.max(0.5, Math.min(1.2, score));
  }

  return {
    ticker, trades: n, wins, losses,
    rawWinRate: rawWr, shrunkWinRate: shrunkWr,
    avgPnl: avg, totalPnl: total,
    avgPnlPct, shrunkExpectancy,
    score, sampleQuality: quality,
  };
}

export function scoreAllTickers(trades: ClosedVirtualTrade[]): TickerScore[] {
  const tickers = new Set(trades.map(t => t.ticker));
  return Array.from(tickers).map(t => scoreTicker(t, trades));
}
