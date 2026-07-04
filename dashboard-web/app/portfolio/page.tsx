export const revalidate = 60;

import { fetchOpenVirtualTrades } from '@/lib/queries';
import { MetricCard } from '../components/metric-card';

export default async function PortfolioPage() {
  const open = await fetchOpenVirtualTrades();

  const totalCredit = open.reduce((s, t) => s + (Number(t.credit_received) || 0), 0);
  const totalMaxLoss = open.reduce((s, t) => s + (Number(t.max_loss) || 0), 0);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Virtual portfolio</h1>
        <p className="text-sm text-muted mt-1">
          Currently open virtual cash-secured puts being tracked.
        </p>
      </div>

      <section className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <MetricCard label="Open positions" value={open.length} />
        <MetricCard label="Total credit at risk" value={`$${totalCredit.toFixed(2)}`} trend="up" />
        <MetricCard label="Total max-loss exposure" value={`$${totalMaxLoss.toFixed(2)}`} trend="down" />
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
                  const da = (new Date(a.expiration).getTime() - Date.now());
                  const db = (new Date(b.expiration).getTime() - Date.now());
                  return da - db;
                })
                .map((t) => {
                  const dte = Math.max(0, Math.floor(
                    (new Date(t.expiration).getTime() - Date.now()) / 86_400_000));
                  const held = Math.max(0, Math.floor(
                    (Date.now() - new Date(t.opened_at).getTime()) / 86_400_000));
                  return (
                    <tr key={t.id} className="border-b border-border/50 last:border-0 hover:bg-border/20">
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
