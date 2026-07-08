import type { CandidatePayload } from '@/lib/types';

/**
 * Shared two-tier candidate renderer used by /candidates (weekly) and
 * /daily (daily indications). Pure display — data fetching stays in pages.
 */

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

export function QualityLegend() {
  return (
    <div className="text-xs text-muted leading-relaxed">
      <span className="font-medium text-text">Data badges: </span>
      <span className="text-success">IBKR LIVE</span> = live broker quotes (trustworthy) ·{' '}
      <span className="text-warning">yfinance+est</span> = free delayed data, Greeks estimated
      (directional only) · <span className="text-danger">PREMIUM ONLY</span> = price known, no
      Greeks · <span className="text-danger">LIQ?</span> = liquidity not verified — always open
      the chain in IBKR before acting.
    </div>
  );
}

export function ScreenCandidates({ candidates }: { candidates: CandidatePayload[] }) {
  const liveCands = candidates.filter(c => c.tier === 'live');
  const sandboxCands = candidates.filter(c => c.tier !== 'live');
  const liveViable = liveCands.filter(c => c.setup);

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-lg font-medium text-success">
          Real-money candidates — staged tickets
          {liveViable.length === 0 && (
            <span className="ml-2 text-muted font-normal">· nothing qualified</span>
          )}
        </h2>
        <p className="text-xs text-muted mt-1 mb-3">
          Liquid $20–60 names as defined-risk put credit spreads. Only shown when every gate
          passed (net credit ≥ $25 after friction, friction ≤ 20%, live quotes). Your only move
          in IBKR: approve or reject.
        </p>
        {liveCands.length === 0 ? (
          <div className="text-sm text-muted">No live-tier candidates recorded.</div>
        ) : (
          <div className="space-y-3">
            {liveCands.map((c) => (
              <div key={`${c.ticker}-${c.rank}`} className={`bg-panel border rounded-lg p-5 ${
                c.setup ? 'border-success/60' : 'border-border'
              }`}>
                <div className="flex items-baseline justify-between">
                  <span className="text-lg font-mono font-semibold">{c.ticker}
                    <span className="ml-2 text-xs text-muted font-normal">
                      ${c.last_price.toFixed(2)} · vol rank {Math.round(c.rv_percentile)}/100
                    </span>
                  </span>
                  <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase bg-success/20 text-success">
                    {c.setup ? 'TICKET STAGED' : 'NO SPREAD'}
                  </span>
                </div>
                {c.setup?.ticket ? (
                  <pre className="mt-3 bg-bg border border-border rounded p-3 text-xs overflow-x-auto whitespace-pre-wrap">
                    {c.setup.ticket}
                  </pre>
                ) : (
                  <div className="mt-2 text-xs text-muted italic">
                    {c.skip_reason || 'No spread passed the gates (credit/friction/liquidity).'}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-medium">Paper-only research (no real money)</h2>
        <p className="text-xs text-muted mt-1 mb-3">
          The $5–25 research universe. These open as virtual positions to build the track
          record — they are not trade suggestions.
        </p>
        {sandboxCands.length === 0 ? (
          <div className="bg-panel border border-border rounded-lg p-8 text-center text-muted">
            No sandbox candidates in this screen.
          </div>
        ) : (
          <div className="space-y-3">
            {sandboxCands.map((c) => {
              const s = c.setup;
              return (
                <div key={`${c.ticker}-${c.rank}`} className="bg-panel border border-border rounded-lg p-5">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <div className="flex items-baseline gap-3">
                      <span className="text-xl font-mono font-semibold">{c.ticker}</span>
                      <span className="text-sm text-muted">
                        ${c.last_price.toFixed(2)} · vol rank{' '}
                        <span className="text-text">{Math.round(c.rv_percentile)}/100</span>
                        {c.next_earnings_days != null && c.next_earnings_days >= 0 && (
                          <> · earnings in {c.next_earnings_days}d</>
                        )}
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
                        <div className="text-xs text-muted">Days to expiry</div>
                        <div>{s.dte}d / {(s.pct_otm * 100).toFixed(1)}% OTM</div>
                      </div>
                      <div>
                        <div className="text-xs text-muted">Credit (you receive)</div>
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
                      No liquid put found in the expiry window. Underlying still ranked — check
                      the chain manually in IBKR.
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
          </div>
        )}
        <div className="mt-3">
          <QualityLegend />
        </div>
      </section>
    </div>
  );
}
