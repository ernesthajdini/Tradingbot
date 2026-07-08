// Server Component: Dashboard home
export const revalidate = 60; // 1 minute server-side cache

import {
  fetchLatestScreen, fetchOpenVirtualTrades, fetchClosedVirtualTrades,
  computePerformance, filterTradesByDays,
} from '@/lib/queries';
import { MetricCard } from './components/metric-card';
import { PnlChart } from './components/pnl-chart';

export default async function DashboardPage() {
  const [latest, openTrades, closed] = await Promise.all([
    fetchLatestScreen(),
    fetchOpenVirtualTrades(),
    fetchClosedVirtualTrades(500),
  ]);

  const allTime = computePerformance(closed);
  const last30 = computePerformance(filterTradesByDays(closed, 30));

  // Build chart data: sorted by closed_at, cumulative pnl
  const sorted = [...closed].sort(
    (a, b) => new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime()
  );
  let cum = 0;
  const chartData = sorted.map(t => {
    cum += Number(t.pnl) || 0;
    return {
      date: t.closed_at.slice(0, 10),
      cumulative: Number(cum.toFixed(2)),
      pnl: Number(t.pnl) || 0,
      ticker: t.ticker,
    };
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="text-sm text-muted mt-1">
          Quick overview of screener state and virtual track record.
        </p>
      </div>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          label="Open virtual positions"
          value={openTrades.length}
        />
        <MetricCard
          label="Closed virtual trades"
          value={allTime.count}
          sub={last30.count ? `${last30.count} last 30d` : undefined}
        />
        <MetricCard
          label="All-time P&L"
          value={`$${allTime.totalPnl.toFixed(2)}`}
          trend={allTime.totalPnl > 0 ? 'up' : allTime.totalPnl < 0 ? 'down' : 'neutral'}
          sub={last30.count ? `$${last30.totalPnl.toFixed(2)} last 30d` : undefined}
        />
        <MetricCard
          label="Win rate"
          value={allTime.count ? `${(allTime.winRate * 100).toFixed(0)}%` : '—'}
          sub={allTime.count ? `${allTime.wins}W / ${allTime.losses}L` : undefined}
        />
      </section>

      <section>
        <h2 className="text-lg font-medium mb-3">Latest screen</h2>
        {latest ? (
          <div className="bg-panel border border-border rounded-lg p-5">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div className="text-sm text-muted">
                <span className={`mr-2 px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                  latest.run_type === 'daily'
                    ? 'bg-accent/15 text-accent'
                    : 'bg-success/20 text-success'
                }`}>
                  {latest.run_type === 'daily' ? 'Daily' : 'Weekly'}
                </span>
                Ran <span className="text-text font-mono">{new Date(latest.ran_at).toLocaleString()}</span>
              </div>
              <div className="text-sm text-muted">
                VIX: <span className="text-text">{latest.vix ?? '—'}</span> ·
                Email sent: <span className={latest.email_sent ? 'text-success' : 'text-warning'}>
                  {latest.email_sent ? 'yes' : 'no'}
                </span>
              </div>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 text-sm">
              <div>
                <div className="text-muted">Universe</div>
                <div className="text-lg">{latest.universe_size ?? 0}</div>
              </div>
              <div>
                <div className="text-muted">Passed filters</div>
                <div className="text-lg">{latest.passed_filters ?? 0}</div>
              </div>
              <div>
                <div className="text-muted">Emailed</div>
                <div className="text-lg">{latest.candidates_in_email ?? 0}</div>
              </div>
              <div>
                <div className="text-muted">Opened virtuals</div>
                <div className="text-lg">{latest.virtual_positions_opened ?? 0}</div>
              </div>
            </div>
            {latest.tickers_in_email?.length ? (
              <div className="mt-4 flex flex-wrap gap-1.5">
                {latest.tickers_in_email.map(t => (
                  <span key={t} className="px-2 py-0.5 rounded text-xs font-mono bg-bg border border-border">
                    {t}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="bg-panel border border-border rounded-lg p-8 text-center text-muted">
            No screens yet. The first run will appear here.
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-medium mb-3">Cumulative virtual P&L</h2>
        <div className="bg-panel border border-border rounded-lg p-4">
          <PnlChart data={chartData} />
        </div>
      </section>

      <section>
        <h2 className="text-lg font-medium mb-3">Open positions ({openTrades.length})</h2>
        {openTrades.length === 0 ? (
          <div className="text-sm text-muted">No open virtual positions.</div>
        ) : (
          <div className="bg-panel border border-border rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr className="border-b border-border">
                  <th className="text-left py-3 px-4">Ticker</th>
                  <th className="text-right py-3 px-4">Strike</th>
                  <th className="text-right py-3 px-4">Expiration</th>
                  <th className="text-right py-3 px-4">DTE</th>
                  <th className="text-right py-3 px-4">Credit</th>
                  <th className="text-right py-3 px-4">Max loss</th>
                  <th className="text-right py-3 px-4">Opened</th>
                </tr>
              </thead>
              <tbody>
                {openTrades.map((t) => {
                  const dte = Math.max(0, Math.floor(
                    (new Date(t.expiration).getTime() - Date.now()) / 86_400_000
                  ));
                  return (
                    <tr key={t.id} className="border-b border-border/50 last:border-0 hover:bg-border/20">
                      <td className="py-3 px-4 font-mono font-semibold">{t.ticker}</td>
                      <td className="py-3 px-4 text-right font-mono">${Number(t.strike).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted">{t.expiration}</td>
                      <td className="py-3 px-4 text-right">{dte}</td>
                      <td className="py-3 px-4 text-right text-success">${Number(t.credit_received).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted">${Number(t.max_loss).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted">{t.opened_at.slice(0, 10)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
