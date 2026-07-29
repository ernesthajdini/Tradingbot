import type { CandidatePayload, VirtualSetup } from '@/lib/types';

/**
 * Shared two-tier candidate renderer used by /daily and /candidates.
 *
 * context='daily' is the ACTING run (mid-session, live two-sided quotes,
 * sends the 🎯 ticket alert). context='weekly' is the Sunday planning
 * snapshot at the last close — it structurally cannot stage a ticket,
 * because after-hours bid/ask are zeroed and the live tier voids before
 * any gate is evaluated.
 */

// Display-only mirrors of config.py playbook caps (not tunable here)
const MAX_RISK_PER_SPREAD = 130;

function fmtExpiry(iso: string): { pretty: string; days: number } {
  const d = new Date(iso + 'T16:00:00'); // options expire at US market close
  const days = Math.max(0, Math.round((d.getTime() - Date.now()) / 86_400_000));
  const pretty = d.toLocaleDateString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
  });
  return { pretty, days };
}

/** "roughly 9 times in 10" phrasing from a win probability. */
function oddsPhrase(p: number): { keep: string; lose: string } {
  const denoms = [4, 5, 10, 20];
  let best = { d: 10, k: Math.round(p * 10) };
  let bestErr = Infinity;
  for (const d of denoms) {
    const k = Math.round(p * d);
    const err = Math.abs(p - k / d);
    if (k > 0 && k < d && err < bestErr) { bestErr = err; best = { d, k }; }
  }
  return {
    keep: `${best.k} time${best.k === 1 ? '' : 's'} in ${best.d}`,
    lose: `${best.d - best.k} time${best.d - best.k === 1 ? '' : 's'} in ${best.d}`,
  };
}

/**
 * Plain-English "what exactly to trade" block. Every number comes straight
 * from the setup — this is a translation layer, not new math.
 */
function TradeInPlainEnglish({
  s, spot, tier,
}: { s: VirtualSetup; spot: number; tier: 'live' | 'sandbox' }) {
  const { pretty, days } = fmtExpiry(s.expiration);
  const isSpread = s.structure === 'put_credit_spread' && s.long_strike != null;
  const credit = s.net_credit_after_friction ?? s.estimated_credit_per_contract;
  const maxLoss = Number(s.max_loss_per_contract);
  const isPaper = tier !== 'live';

  // Odds: P(expires worthless) ≈ 1 - |delta| — the market's own estimate
  const winP = s.delta != null ? 1 - Math.min(1, Math.abs(Number(s.delta))) : null;
  const estimated = !s.data_quality?.startsWith('ibkr');
  const odds = winP != null ? oddsPhrase(winP) : null;

  const riskPctOfCap = Math.round((maxLoss / MAX_RISK_PER_SPREAD) * 100);

  return (
    <div className={`mt-3 rounded-lg border p-4 ${
      isPaper ? 'border-border bg-border/10' : 'border-accent/40 bg-accent/5'
    }`}>
      <div className={`text-[11px] font-semibold uppercase tracking-wide mb-1 ${
        isPaper ? 'text-muted' : 'text-accent'
      }`}>
        {isPaper ? 'The paper trade — research only, not for your account' : 'The trade'}
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
          <span className={`font-medium ${isPaper ? 'text-muted' : 'text-success'}`}>
            {isPaper ? `The model collects ≈ $${Number(credit).toFixed(0)} (virtual)` :
              `You collect ≈ $${Number(credit).toFixed(0)}`}
          </span>
          <span className="text-muted"> per contract, up front{isSpread ? ' (net, after costs)' : ''}.</span>
        </li>
        {odds && winP != null && (
          <li>
            <span className="font-medium">≈ {Math.round(winP * 100)}% odds</span>
            <span className="text-muted">
              {' '}this expires worthless (read off the option&apos;s delta — the market&apos;s own
              estimate, not a promise{estimated ? '; delta is estimated here, treat as rough' : ''}).
              At those odds: keep ≈ ${Number(credit).toFixed(0)} about {odds.keep}; lose up to{' '}
              ${maxLoss.toFixed(0)} about {odds.lose}.
            </span>
          </li>
        )}
        {!odds && (
          <li className="text-warning text-xs">
            Odds unknown — the data source gave no Greeks. Don&apos;t act on this one without
            checking the chain in IBKR.
          </li>
        )}
        <li>
          <span className="font-medium">You win</span>
          <span className="text-muted">
            {' '}if {s.ticker} stays above ${s.strike.toFixed(2)} through {pretty} — the strike is{' '}
            {(s.pct_otm * 100).toFixed(0)}% below today&apos;s ${spot.toFixed(2)}.
          </span>
        </li>
        <li>
          <span className="text-danger font-medium">
            Worst case −${maxLoss.toFixed(0)}
          </span>
          <span className="text-muted">
            {isSpread
              ? ` — capped by the bought put (${riskPctOfCap}% of your $${MAX_RISK_PER_SPREAD} per-trade cap).`
              : ` if ${s.ticker} went to zero. You start losing below $${s.breakeven.toFixed(2)}.`}
          </span>
        </li>
        <li className="text-muted">
          Exit plan: buy it back at 50% of the credit, or close when 21 days remain — whichever first.
        </li>
        {isPaper && (
          <li className="text-xs text-warning">
            Real CSPs at this account size are a playbook hard-no until ~$10K+. This exists to
            build the track record.
          </li>
        )}
      </ul>
    </div>
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

export function ScreenCandidates({
  candidates, context = 'weekly',
}: { candidates: CandidatePayload[]; context?: 'weekly' | 'daily' }) {
  const liveCands = candidates.filter(c => c.tier === 'live');
  const sandboxCands = candidates.filter(c => c.tier !== 'live');
  const liveViable = liveCands.filter(c => c.setup);
  const isDaily = context === 'daily';

  return (
    <div className="space-y-8">
      <section>
        <h2 className={`text-lg font-medium ${isDaily ? 'text-success' : ''}`}>
          {isDaily
            ? 'Real-money candidates — staged from live quotes'
            : 'Planning snapshot — last close, cannot stage'}
          {liveViable.length === 0 && (
            <span className="ml-2 text-muted font-normal">· nothing qualified</span>
          )}
        </h2>
        <p className="text-xs text-muted mt-1 mb-3">
          {isDaily
            ? 'Liquid $20–60 names as defined-risk put credit spreads, screened mid-session against real two-sided quotes. Only shown when every gate passed (net credit ≥ $25 after friction, friction ≤ 20%, live quotes). Your only move in IBKR: approve or reject — never retype a ticket.'
            : 'Same gates, but this run screens after the close, where bid/ask are zeroed — the live tier voids before any gate is evaluated. Planning only; tickets come from the weekday 15:05 UTC run.'}
        </p>
        {liveCands.length === 0 ? (
          <div className="text-sm text-muted">No live-tier candidates recorded.</div>
        ) : (
          <div className="space-y-3">
            {liveCands.map((c) => {
              const viableIndex = liveViable.indexOf(c); // -1 if not viable
              return (
                <div key={`${c.ticker}-${c.rank}`} className={`bg-panel border rounded-lg p-5 ${
                  c.setup ? 'border-success/60' : 'border-border'
                }`}>
                  <div className="flex items-baseline justify-between">
                    <span className="text-lg font-mono font-semibold">{c.ticker}
                      <span className="ml-2 text-xs text-muted font-normal">
                        ${c.last_price.toFixed(2)} · vol rank {Math.round(c.rv_percentile)}/100
                      </span>
                    </span>
                    <span className="inline-flex gap-1">
                      {c.setup && viableIndex === 0 && (
                        <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-warning/25 text-warning">
                          ★ Best pick
                        </span>
                      )}
                      {c.setup && viableIndex > 0 && (
                        <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase bg-border/50 text-muted">
                          Backup #{viableIndex + 1}
                        </span>
                      )}
                      <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                        c.setup ? 'bg-success/20 text-success' : 'bg-border/50 text-muted'
                      }`}>
                        {c.setup ? 'TICKET STAGED' : 'NO SPREAD'}
                      </span>
                    </span>
                  </div>
                  {c.setup && viableIndex > 0 && (
                    <div className="mt-1 text-xs text-muted">
                      Take only if the best pick won&apos;t fill at its limit — never both; the
                      budget allows one.
                    </div>
                  )}
                  {c.setup ? (
                    <>
                      <TradeInPlainEnglish s={c.setup} spot={c.last_price} tier="live" />
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
                    <div className="mt-2 text-xs text-muted">
                      <span className="italic">
                        {c.skip_reason || 'No spread passed the gates.'}
                      </span>
                      {c.void_reasons?.length ? (
                        <ul className="mt-1 space-y-0.5">
                          {c.void_reasons.slice(0, 3).map((r, i) => (
                            <li key={i} className={r.includes('NEAR MISS') ? 'text-warning' : ''}>
                              • {r}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  )}
                </div>
              );
            })}
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

                  {s && <TradeInPlainEnglish s={s} spot={c.last_price} tier="sandbox" />}

                  {s ? (
                    <div className="mt-3 text-xs text-muted">
                      For the detail-inclined: Δ {s.delta != null ? s.delta.toFixed(2) : '?'} · IV{' '}
                      {s.iv != null ? `${(s.iv * 100).toFixed(0)}%` : '?'} · open interest{' '}
                      {s.open_interest} · breakeven ${s.breakeven.toFixed(2)}
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
