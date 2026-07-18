export const revalidate = 60;

import { fetchOpenVirtualTrades } from '@/lib/queries';
import { MetricCard } from '../components/metric-card';

// Display-only mirrors of config.py playbook caps (not tunable here)
const BUDGET_CAP = 200;   // MAX_TOTAL_PREMIUM_AT_RISK
const SLOTS_MAX = 2;      // MAX_OPEN_POSITIONS
const MAX_RISK_PER_SPREAD = 130;

export default async function PortfolioPage() {
  const open = await fetchOpenVirtualTrades();

  const live = open.filter(t => t.tier === 'live');
  const sandbox = open.filter(t => t.tier !== 'live');
  const liveRisk = live.reduce((s, t) => s + (Number(t.max_loss) || 0), 0);
  const liveCredit = live.reduce((s, t) => s + (Number(t.credit_received) || 0), 0);
  const riskPct = Math.min(100, Math.round((liveRisk / BUDGET_CAP) * 100));

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Virtual portfolio</h1>
        <p className="text-sm text-muted mt-1">
          Live-tier positions count against your playbook caps; the sandbox is paper research
          with $0 real money.
        </p>
      </div>

      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="bg-panel border border-border rounded-lg p-5">
          <div className="text-xs uppercase tracking-wide text-muted font-medium mb-2">
            Live risk in use
          </div>
          <div className={`text-2xl font-semibold ${riskPct >= 80 ? 'text-warning' : ''}`}>
            ${liveRisk.toFixed(0)} <span className="text-sm text-muted font-normal">of ${BUDGET_CAP} cap</span>
          </div>
          <div className="mt-2 h-2 rounded bg-border/40 overflow-hidden">
            <div
              className={`h-full rounded ${riskPct >= 80 ? 'bg-warning' : 'bg-accent'}`}
              style={{ width: `${riskPct}%` }}
            />
          </div>
          <div className="mt-1 text-xs text-muted">
            {riskPct}% used · a new spread costs up to ${MAX_RISK_PER_SPREAD}
          </div>
        </div>
        <MetricCard
          label="Live position slots"
          value={`${live.length} of ${SLOTS_MAX}`}
          sub={live.length >= SLOTS_MAX ? 'Full — no new live positions' : undefined}
        />
        <MetricCard
          label="Premium collected (open live)"
          value={`$${liveCredit.toFixed(2)}`}
          trend={liveCredit > 0 ? 'up' : 'neutral'}
        />
        <MetricCard
          label="Sandbox paper research"
          value={sandbox.length}
          sub="$0 real money"
        />
      </section>

      {open.length === 0 ? (
        <div className="bg-panel border border-border rounded-lg p-8 text-center text-muted">
          No open virtual positions.
        </div>
      ) : (
        <div className="bg-panel border border-border rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4">Tier</th>
                <th className="text-left py-3 px-4">Ticker</th>
                <th className="text-right py-3 px-4">Strike</th>
                <th className="text-right py-3 px-4">Expiration</th>
                <th className="text-right py-3 px-4">DTE</th>
                <th className="text-right py-3 px-4">Days held</th>
                <th className="text-right py-3 px-4">Credit</th>
                <th className="text-right py-3 px-4">Max loss</th>
                <th className="text-right py-3 px-4">Breakeven</th>
                <th className="text-right py-3 px-4">Spot at open</th>
              </tr>
            </thead>
            <tbody>
              {open
                .slice()
                .sort((a, b) => {
                  // live first, then soonest expiry
                  const tierDiff = (a.tier === 'live' ? 0 : 1) - (b.tier === 'live' ? 0 : 1);
                  if (tierDiff !== 0) return tierDiff;
                  return new Date(a.expiration).getTime() - new Date(b.expiration).getTime();
                })
                .map((t) => {
                  const dte = Math.max(0, Math.floor(
                    (new Date(t.expiration).getTime() - Date.now()) / 86_400_000));
                  const held = Math.max(0, Math.floor(
                    (Date.now() - new Date(t.opened_at).getTime()) / 86_400_000));
                  return (
                    <tr key={t.id} className="border-b border-border/50 last:border-0 hover:bg-border/20">
                      <td className="py-3 px-4">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                          t.tier === 'live'
                            ? 'bg-success/20 text-success'
                            : 'bg-border/50 text-muted'
                        }`}>
                          {t.tier === 'live' ? 'Live' : 'Paper'}
                        </span>
                      </td>
                      <td className="py-3 px-4 font-mono font-semibold">{t.ticker}</td>
                      <td className="py-3 px-4 text-right font-mono">${Number(t.strike).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted">{t.expiration}</td>
                      <td className={`py-3 px-4 text-right ${dte <= 21 ? 'text-warning' : ''}`}>{dte}</td>
                      <td className="py-3 px-4 text-right text-muted">{held}</td>
                      <td className="py-3 px-4 text-right text-success font-mono">${Number(t.credit_received).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted font-mono">${Number(t.max_loss).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right font-mono">${Number(t.breakeven).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted font-mono">${Number(t.spot_at_open).toFixed(2)}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
