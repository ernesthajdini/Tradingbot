export const revalidate = 60;

import { fetchLatestScreen, fetchRecentScreens } from '@/lib/queries';
import type { CandidatePayload } from '@/lib/types';

function QualityBadge({ q }: { q: string | undefined }) {
  if (!q) return null;
  const unverified = q.endsWith('_unverified_liquidity');
  const base = q.replace('_unverified_liquidity', '');
  const map: Record<string, { bg: string; label: string }> = {
    ibkr_greeks: { bg: 'bg-success/20 text-success', label: 'IBKR LIVE' },
    yfinance_iv_estimated_delta: { bg: 'bg-warning/20 text-warning', label: 'yfinance+est' },
    premium_only_no_greeks: { bg: 'bg-danger/20 text-danger', label: 'PREMIUM ONLY' },
  };
  const v = map[base] || { bg: 'bg-border/50 text-muted', label: base };
  return (
    <span className="inline-flex gap-1">
      <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${v.bg}`}>
        {v.label}
      </span>
      {unverified && (
        <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase bg-danger/20 text-danger">
          LIQ?
        </span>
      )}
    </span>
  );
}

export default async function CandidatesPage() {
  const [latest, recent] = await Promise.all([
    fetchLatestScreen(),
    fetchRecentScreens(10),
  ]);

  const candidates = (latest?.candidates_payload || []) as CandidatePayload[];

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Candidates</h1>
        <p className="text-sm text-muted mt-1">
          Last screen ran {latest ? new Date(latest.ran_at).toLocaleString() : 'never'}.
          Underlying candidates only — choose your own contract.
        </p>
      </div>

      {candidates.length === 0 ? (
        <div className="bg-panel border border-border rounded-lg p-8 text-center text-muted">
          No candidates in the latest screen.
        </div>
      ) : (
        <section className="space-y-3">
          {candidates.map((c) => {
            const s = c.setup;
            return (
              <div key={c.ticker} className="bg-panel border border-border rounded-lg p-5">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div className="flex items-baseline gap-3">
                    <span className="text-xl font-mono font-semibold">{c.ticker}</span>
                    <span className="text-sm text-muted">
                      ${c.last_price.toFixed(2)} · RV pct{' '}
                      <span className="text-text">{Math.round(c.rv_percentile)}</span>
                    </span>
                  </div>
                  {s && <QualityBadge q={s.data_quality} />}
                </div>

                {s ? (
                  <div className="mt-4 grid grid-cols-2 sm:grid-cols-6 gap-3 text-sm">
                    <div>
                      <div className="text-xs text-muted">Contract</div>
                      <div className="font-mono">${s.strike.toFixed(2)}P {s.expiration}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted">DTE</div>
                      <div>{s.dte}d / {(s.pct_otm * 100).toFixed(1)}% OTM</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted">Credit</div>
                      <div className="text-success">${s.estimated_credit_per_contract.toFixed(2)}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted">Max loss</div>
                      <div className="text-danger">${s.max_loss_per_contract.toFixed(0)}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted">Breakeven</div>
                      <div>${s.breakeven.toFixed(2)}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted">Δ / IV</div>
                      <div>
                        {s.delta != null ? s.delta.toFixed(2) : '?'} /{' '}
                        {s.iv != null ? `${(s.iv * 100).toFixed(0)}%` : '?'}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 text-sm text-muted italic">
                    No liquid put found in DTE window. Underlying still ranked — check chain manually in IBKR.
                  </div>
                )}

                {s?.reasoning?.length ? (
                  <ul className="mt-3 text-xs text-muted space-y-0.5">
                    {s.reasoning.map((r, i) => (
                      <li key={i}>• {r}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            );
          })}
        </section>
      )}

      <section>
        <h2 className="text-lg font-medium mb-3">Recent screens</h2>
        <div className="bg-panel border border-border rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4">Date</th>
                <th className="text-right py-3 px-4">Universe</th>
                <th className="text-right py-3 px-4">Passed</th>
                <th className="text-right py-3 px-4">Sent</th>
                <th className="text-right py-3 px-4">Opened</th>
                <th className="text-right py-3 px-4">Closed</th>
                <th className="text-right py-3 px-4">Closed PnL</th>
                <th className="text-right py-3 px-4">VIX</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((s) => (
                <tr key={s.id} className="border-b border-border/50 last:border-0">
                  <td className="py-3 px-4 font-mono text-xs">{s.ran_at.slice(0, 10)}</td>
                  <td className="py-3 px-4 text-right">{s.universe_size ?? 0}</td>
                  <td className="py-3 px-4 text-right">{s.passed_filters ?? 0}</td>
                  <td className="py-3 px-4 text-right">{s.candidates_in_email ?? 0}</td>
                  <td className="py-3 px-4 text-right">{s.virtual_positions_opened ?? 0}</td>
                  <td className="py-3 px-4 text-right">{s.virtual_closed_this_run ?? 0}</td>
                  <td className={`py-3 px-4 text-right font-mono ${
                    (s.virtual_closed_pnl ?? 0) > 0 ? 'text-success'
                    : (s.virtual_closed_pnl ?? 0) < 0 ? 'text-danger' : ''
                  }`}>
                    {s.virtual_closed_pnl != null ? `$${Number(s.virtual_closed_pnl).toFixed(2)}` : '—'}
                  </td>
                  <td className="py-3 px-4 text-right text-muted">{s.vix ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
