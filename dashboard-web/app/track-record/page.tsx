export const revalidate = 60;

import {
  fetchClosedVirtualTrades, computePerformance, filterTradesByDays, markSource,
} from '@/lib/queries';
import { MetricCard } from '../components/metric-card';

function PeriodCard({ label, perf }: { label: string; perf: ReturnType<typeof computePerformance> }) {
  return (
    <div className="bg-panel border border-border rounded-lg p-5">
      <div className="text-xs uppercase tracking-wide text-muted font-medium mb-3">
        {label}
      </div>
      {perf.count === 0 ? (
        <div className="text-sm text-muted italic">No closed trades in this period yet.</div>
      ) : (
        <>
          <div className={`text-2xl font-semibold ${
            perf.totalPnl > 0 ? 'text-success' : perf.totalPnl < 0 ? 'text-danger' : ''
          }`}>
            ${perf.totalPnlPessimistic.toFixed(2)}
            <span className="text-muted font-normal text-lg"> to </span>
            ${perf.totalPnl.toFixed(2)}
          </div>
          <div className="text-[11px] text-muted mt-0.5">
            pessimistic ↔ base fills — the truth is somewhere inside; paper fills contain no
            slippage information
          </div>
          {perf.totalPnlEur != null && (
            <div className="text-xs text-muted mt-0.5">
              ≈ €{perf.totalPnlEur.toFixed(2)} at trade-date rates (the scoreboard that matters)
            </div>
          )}
          <div className="mt-2 text-sm space-y-0.5">
            <div className="text-muted">
              <span className="text-text">{perf.count}</span> trades ·{' '}
              <span className="text-text">{(perf.winRate * 100).toFixed(0)}%</span> win
            </div>
            <div className="text-muted">
              Avg win ${perf.avgWin.toFixed(2)} vs avg loss ${perf.avgLoss.toFixed(2)} ·
              expectancy ${perf.expectancy.toFixed(2)}/trade
            </div>
            <div className="text-muted">
              Best ${perf.best.toFixed(2)} · Worst ${perf.worst.toFixed(2)}
            </div>
          </div>
          {perf.count < 30 && (
            <div className="mt-2 text-[11px] text-warning">
              Fewer than 30 closed trades — treat every number here as noise, not signal.
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default async function TrackRecordPage() {
  const closed = await fetchClosedVirtualTrades(500);
  const all = computePerformance(closed);
  const last90 = computePerformance(filterTradesByDays(closed, 90));
  const last30 = computePerformance(filterTradesByDays(closed, 30));
  const marketMarked = closed.filter(t => markSource(t) === 'market').length;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Virtual track record</h1>
        <p className="text-sm text-muted mt-1">
          What would the screener have made if you'd taken every suggestion?
          No real money involved.
        </p>
      </div>

      <section className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <PeriodCard label="Last 30 days" perf={last30} />
        <PeriodCard label="Last 90 days" perf={last90} />
        <PeriodCard label="All-time" perf={all} />
      </section>

      {all.count > 0 && (
        <div className="text-xs text-muted">
          <span className={marketMarked === 0 ? 'text-warning' : 'text-text'}>
            {marketMarked} of {all.count} closes ({Math.round((marketMarked / all.count) * 100)}%)
          </span>{' '}
          were marked against a real option quote — the rest fell back to the pricing model,
          which prices profits too early. Only market-marked closes are evidence for going live.
        </div>
      )}

      {all.count > 0 && (
        <>
          <section>
            <h2 className="text-lg font-medium mb-3">By exit reason</h2>
            <div className="bg-panel border border-border rounded-lg overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase text-muted">
                  <tr className="border-b border-border">
                    <th className="text-left py-3 px-4">Reason</th>
                    <th className="text-right py-3 px-4">Trades</th>
                    <th className="text-right py-3 px-4">Total PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(all.byExitReason)
                    .sort((a, b) => b[1].pnl - a[1].pnl)
                    .map(([reason, stats]) => (
                      <tr key={reason} className="border-b border-border/50 last:border-0">
                        <td className="py-3 px-4 font-mono">{reason}</td>
                        <td className="py-3 px-4 text-right">{stats.count}</td>
                        <td className={`py-3 px-4 text-right font-mono ${
                          stats.pnl > 0 ? 'text-success' : stats.pnl < 0 ? 'text-danger' : ''
                        }`}>${stats.pnl.toFixed(2)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </section>

          <section>
            <h2 className="text-lg font-medium mb-3">By ticker</h2>
            <div className="bg-panel border border-border rounded-lg overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase text-muted">
                  <tr className="border-b border-border">
                    <th className="text-left py-3 px-4">Ticker</th>
                    <th className="text-right py-3 px-4">Trades</th>
                    <th className="text-right py-3 px-4">Wins</th>
                    <th className="text-right py-3 px-4">Win rate</th>
                    <th className="text-right py-3 px-4">Total PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(all.byTicker)
                    .sort((a, b) => b[1].pnl - a[1].pnl)
                    .slice(0, 20)
                    .map(([ticker, s]) => (
                      <tr key={ticker} className="border-b border-border/50 last:border-0">
                        <td className="py-3 px-4 font-mono font-semibold">{ticker}</td>
                        <td className="py-3 px-4 text-right">{s.trades}</td>
                        <td className="py-3 px-4 text-right">{s.wins}</td>
                        <td className="py-3 px-4 text-right">
                          {((s.wins / s.trades) * 100).toFixed(0)}%
                        </td>
                        <td className={`py-3 px-4 text-right font-mono ${
                          s.pnl > 0 ? 'text-success' : s.pnl < 0 ? 'text-danger' : ''
                        }`}>${s.pnl.toFixed(2)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </section>

          <section>
            <h2 className="text-lg font-medium mb-3">All closed trades</h2>
            <div className="bg-panel border border-border rounded-lg overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase text-muted">
                  <tr className="border-b border-border">
                    <th className="text-left py-3 px-4">Closed</th>
                    <th className="text-left py-3 px-4">Ticker</th>
                    <th className="text-right py-3 px-4">Strike</th>
                    <th className="text-right py-3 px-4">Days held</th>
                    <th className="text-left py-3 px-4">Reason</th>
                    <th className="text-center py-3 px-4">Mark</th>
                    <th className="text-right py-3 px-4">PnL</th>
                    <th className="text-right py-3 px-4">% of credit</th>
                  </tr>
                </thead>
                <tbody>
                  {closed.slice(0, 100).map((t) => (
                    <tr key={t.trade_id} className="border-b border-border/50 last:border-0">
                      <td className="py-3 px-4 font-mono text-xs">{t.closed_at.slice(0, 10)}</td>
                      <td className="py-3 px-4 font-mono font-semibold">{t.ticker}</td>
                      <td className="py-3 px-4 text-right font-mono">${Number(t.strike).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right">{Math.round(Number(t.days_held) || 0)}</td>
                      <td className="py-3 px-4 text-xs text-muted font-mono">{t.exit_reason}</td>
                      <td className="py-3 px-4 text-center">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                          markSource(t) === 'market' ? 'bg-success/20 text-success'
                            : markSource(t) === 'model' ? 'bg-warning/20 text-warning'
                              : 'bg-border/50 text-muted'
                        }`}>
                          {markSource(t) ?? 'n/a'}
                        </span>
                      </td>
                      <td className={`py-3 px-4 text-right font-mono ${
                        Number(t.pnl) > 0 ? 'text-success' : 'text-danger'
                      }`}>${Number(t.pnl).toFixed(2)}</td>
                      <td className="py-3 px-4 text-right text-muted">
                        {(Number(t.pnl_pct_of_credit) * 100).toFixed(0)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
