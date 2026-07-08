// Mirrors the Supabase schema.

export interface Recommendation {
  severity: 'info' | 'warn' | 'alert';
  category: string;
  title: string;
  detail: string;
  supporting_data: Record<string, unknown>;
}

export interface Screen {
  id: number;
  screen_id: string;
  run_type: 'weekly' | 'daily' | null; // null = weekly (legacy rows)
  ran_at: string;
  universe_size: number | null;
  passed_filters: number | null;
  candidates_in_email: number | null;
  virtual_positions_opened: number | null;
  virtual_closed_this_run: number | null;
  virtual_closed_pnl: number | null;
  vix: number | null;
  email_sent: boolean;
  tickers_in_email: string[] | null;
  candidates_payload: CandidatePayload[] | null;
  summaries_payload: Record<string, PerformanceSummary> | null;
  recommendations_payload: Recommendation[] | null;
  live_viable: number | null;
  no_trade_week: boolean | null;
  post_spike_window: boolean | null;
  fomc_days: number | null;
  eur_usd_rate: number | null;
  recorded_at: string;
}

export interface CandidatePayload {
  ticker: string;
  last_price: number;
  rv_percentile: number;
  rv_20d_annual: number;
  avg_volume_20d: number;
  next_earnings_days: number | null;
  rank: number;
  setup: VirtualSetup | null;
  tier?: 'live' | 'sandbox';
  skip_reason?: string;
}

export interface VirtualSetup {
  ticker: string;
  spot_at_screen: number;
  expiration: string;
  dte: number;
  strike: number;
  pct_otm: number;
  delta: number | null;
  iv: number | null;
  bid: number;
  ask: number;
  mid: number;
  bid_ask_pct: number;
  open_interest: number;
  volume: number;
  estimated_credit_per_contract: number;
  max_loss_per_contract: number;
  breakeven: number;
  data_quality: string;
  reasoning: string[];
  structure?: 'csp' | 'put_credit_spread';
  long_strike?: number | null;
  tier?: 'live' | 'sandbox';
  net_credit_after_friction?: number | null;
  friction_estimate?: number | null;
  ticket?: string | null;
}

export interface OpenVirtualTrade {
  id: number;
  trade_id: string;
  screen_id: string;
  ticker: string;
  strike: number;
  expiration: string;
  opened_at: string;
  spot_at_open: number;
  dte_at_open: number;
  credit_received: number;
  max_loss: number;
  breakeven: number;
  delta_at_open: number | null;
  iv_at_open: number | null;
  data_quality: string;
}

export interface ClosedVirtualTrade {
  trade_id: string;
  ticker: string;
  strike: number;
  expiration: string;
  opened_at: string;
  closed_at: string;
  days_held: number;
  spot_at_open: number;
  exit_spot: number;
  dte_at_open: number;
  credit_received: number;
  max_loss: number;
  breakeven: number;
  delta_at_open: number | null;
  iv_at_open: number | null;
  data_quality: string;
  exit_reason: string;
  final_put_price: number;
  pnl: number;
  pnl_pct_of_credit: number;
}

export interface SystemEvent {
  id: number;
  event: string;
  at: string;
  payload: Record<string, unknown>;
}

export interface PerformanceSummary {
  period: string;
  closed_count: number;
  open_count: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  avg_pnl: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number;
  expectancy: number;
  best_trade: number;
  worst_trade: number;
  by_exit_reason: Record<string, { count: number; pnl: number }>;
  by_ticker: Record<string, { trades: number; wins: number; win_rate: number; pnl: number }>;
}
