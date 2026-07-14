import type { CandidatePayload, VirtualSetup } from '@/lib/types';

/**
 * Shared two-tier candidate renderer used by /candidates (weekly) and
 * /daily (daily indications). Pure display — data fetching stays in pages.
 */

function fmtExpiry(iso: string): { pretty: string; days: number } {
  const d = new Date(iso + 'T16:00:00'); // options expire at US market close
  const days = Math.max(0, Math.round((d.getTime() - Date.now()) / 86_400_000));
  const pretty = d.toLocaleDateString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
  });
  return { pretty, days };
}

/**
 * Plain-English "what exactly to trade" block. Every number comes straight
 * from the setup — this is a translation layer, not new math.
 */
function TradeInPlainEnglish({ s, spot }: { s: VirtualSetup; spot: number }) {
  const { pretty, days } = fmtExpiry(s.expiration);
  const isSpread = s.structure === 'put_credit_spread' && s.long_strike != null;
  const credit = s.net_credit_after_friction ?? s.estimated_credit_per_contract;

  return (
    <div className="mt-3 rounded-lg border border-accent/40 bg-accent/5 p-4">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-accent mb-1">
        The trade
      </div>
      <div className="font-mono font-semibold text-base">
        {isSpread ? (
          <>SELL the ${s.strike.toFixed(2)} put + BUY the ${Number(s.long_strike).toFixed(2)} put</>
        ) : (
          <>SELL 1 × {s.ticker} ${s.strike.toFixed(2)} PUT</>
        )}
        <span className="text-muted font-sans font-normal"> — expires </span>
        {pretty}
        <span className="text-muted font-sans font-normal"> ({days} days from now)</span>
      </div>
      <ul className="mt-2 text-sm space-y-1">
        <li>
          <span className="text-success font-medium">You collect ≈ ${Number(credit).toFixed(0)}</span>
          <span className="text-muted"> per contract, up front{isSpread ? ' (net, after costs)' : ''}.</span>
        </li>
        <li>
          <span className="font-medium">You win</span>
          <span className="text-muted">
            {' '}if {s.ticker} stays above ${s.strike.toFixed(2)} through {pretty} — the strike is{' '}
            {(s.pct_otm * 100).toFixed(0)}% below today&apos;s ${spot.toFixed(2)}.
          </span>
        </li>
        <li>
          <span className="text-danger font-medium">
            Worst case −${Number(s.max_loss_per_contract).toFixed(0)}
          </span>
          <span className="text-muted">
            {isSpread
              ? ' — capped by the bought put, no matter how far it falls.'
              : ` if ${s.ticker} went to zero. You start losing below $${s.breakeven.toFixed(2)}.`}
          </span>
        </li>
        <li className="text-muted">
          Exit plan: buy it back at 50% of the credit, or close when 21 days remain — whichever first.
        </li>
      </ul>
    </div>
  );
}

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
                {c.setup ? (
                  <>
                    <TradeInPlainEnglish s={c.setup} spot={c.last_price} />
                    {c.setup.ticket && (
                      <>
                        <div className="mt-3 text-xs text-muted">
                          The exact staged order (approve or reject in IBKR — never retype it):
                        </div>
                        <pre className="mt-1 bg-bg border border-border rounded p-3 text-xs overflow-x-auto whitespace-pre-wrap">
                          {c.setup.ticket}
                        </pre>
                      </>
                    )}
                  </>
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

                  {s && <TradeInPlainEnglish s={s} spot={c.last_price} />}

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
