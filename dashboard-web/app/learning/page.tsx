export const revalidate = 60;

import {
  fetchLatestScreen, fetchClosedVirtualTrades, scoreAllTickers,
} from '@/lib/queries';
import type { Recommendation } from '@/lib/types';

function SeverityBadge({ severity }: { severity: Recommendation['severity'] }) {
  const map = {
    warn: 'bg-warning/20 text-warning border-warning/40',
    alert: 'bg-danger/20 text-danger border-danger/40',
    info: 'bg-accent/20 text-accent border-accent/40',
  } as const;
  return (
    <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase border ${map[severity]}`}>
      {severity}
    </span>
  );
}

export default async function LearningPage() {
  const [latest, closed] = await Promise.all([
    fetchLatestScreen(),
    fetchClosedVirtualTrades(500),
  ]);

  const recommendations = (latest?.recommendations_payload || []) as Recommendation[];
  const tickerScores = scoreAllTickers(closed)
    .filter(t => t.trades > 0)
    .sort((a, b) => b.totalPnl - a.totalPnl);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">🧠 Learning</h1>
        <p className="text-sm text-muted mt-1">
          What the system has learned from {closed.length} closed virtual trades.
          Recommendations surface patterns — they never auto-apply.
        </p>
      </div>

      {/* Recommendations from latest screen */}
      <section>
        <h2 className="text-lg font-medium mb-3">System recommendations</h2>
        {recommendations.length === 0 ? (
          <div className="bg-panel border border-border rounded-lg p-8 text-center text-muted text-sm">
            No recommendations yet. The learning layer needs at least 5 closed
            virtual trades before it has anything meaningful to say.
          </div>
        ) : (
          <div className="space-y-2">
            {recommendations.map((r, i) => (
              <div
                key={i}
                className={`bg-panel border-l-4 ${
                  r.severity === 'warn' ? 'border-l-warning'
                  : r.severity === 'alert' ? 'border-l-danger'
                  : 'border-l-accent'
                } border border-border rounded-r-lg p-4`}
              >
                <div className="flex items-center gap-2 mb-1.5">
                  <SeverityBadge severity={r.severity} />
                  <span className="text-xs text-muted uppercase tracking-wide">
                    {r.category}
                  </span>
                </div>
                <div className="font-medium text-sm">{r.title}</div>
                <div className="text-xs text-muted mt-1">{r.detail}</div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Per-ticker reliability table */}
      <section>
        <h2 className="text-lg font-medium mb-3">Per-ticker reliability</h2>
        {tickerScores.length === 0 ? (
          <div className="text-sm text-muted">No closed trades to score yet.</div>
        ) : (
          <div className="bg-panel border border-border rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr className="border-b border-border">
                  <th className="text-left py-3 px-4">Ticker</th>
                  <th className="text-right py-3 px-4">Trades</th>
                  <th className="text-right py-3 px-4">Wins</th>
                  <th className="text-right py-3 px-4">Raw WR</th>
                  <th className="text-right py-3 px-4">Shrunk WR</th>
                  <th className="text-right py-3 px-4">Avg PnL</th>
                  <th className="text-right py-3 px-4">Total PnL</th>
                  <th className="text-right py-3 px-4">Score</th>
                  <th className="text-left py-3 px-4">Sample</th>
                </tr>
              </thead>
              <tbody>
                {tickerScores.map((t) => (
                  <tr key={t.ticker} className="border-b border-border/50 last:border-0">
                    <td className="py-3 px-4 font-mono font-semibold">{t.ticker}</td>
                    <td className="py-3 px-4 text-right">{t.trades}</td>
                    <td className="py-3 px-4 text-right">{t.wins}</td>
                    <td className="py-3 px-4 text-right">{(t.rawWinRate * 100).toFixed(0)}%</td>
                    <td className={`py-3 px-4 text-right ${
                      t.shrunkWinRate > 0.55 ? 'text-success'
                      : t.shrunkWinRate < 0.45 ? 'text-danger'
                      : ''
                    }`}>{(t.shrunkWinRate * 100).toFixed(0)}%</td>
                    <td className={`py-3 px-4 text-right font-mono ${
                      t.avgPnl > 0 ? 'text-success' : t.avgPnl < 0 ? 'text-danger' : ''
                    }`}>${t.avgPnl.toFixed(2)}</td>
                    <td className={`py-3 px-4 text-right font-mono ${
                      t.totalPnl > 0 ? 'text-success' : t.totalPnl < 0 ? 'text-danger' : ''
                    }`}>${t.totalPnl.toFixed(2)}</td>
                    <td className={`py-3 px-4 text-right font-mono ${
                      t.score > 1.05 ? 'text-success'
                      : t.score < 0.95 ? 'text-danger'
                      : 'text-muted'
                    }`}>{t.score.toFixed(2)}×</td>
                    <td className={`py-3 px-4 text-xs ${
                      t.sampleQuality === 'thin' ? 'text-muted'
                      : t.sampleQuality === 'robust' ? 'text-success'
                      : 'text-warning'
                    }`}>{t.sampleQuality}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-muted mt-2">
          <b>Score</b> is the ranking multiplier the screener applies to each
          ticker's RV percentile. <b>Shrunk WR</b> uses Bayesian shrinkage so
          small samples don't overpower the prior of 50%. Sample quality:
          <span className="text-muted"> thin</span> (&lt;3) ·
          <span className="text-warning"> moderate</span> (3–9) ·
          <span className="text-success"> robust</span> (10+).
        </p>
      </section>

      {/* How learning works */}
      <section className="bg-panel border border-border rounded-lg p-5">
        <h2 className="text-base font-medium mb-2">How the self-learning works</h2>
        <ul className="text-sm text-muted space-y-1 list-disc list-inside">
          <li>Every Sunday screen opens virtual positions for the top 5 candidates</li>
          <li>The daily check marks them to market and closes when exit rules fire</li>
          <li>Every closed trade gets logged with its features (delta, DTE, RV, VIX)</li>
          <li>Next Sunday, the ranker boosts tickers with strong history and de-prioritizes chronic losers</li>
          <li>The recommender surfaces feature buckets that are over-/under-performing</li>
          <li>Threshold changes are <b>never</b> auto-applied — 14-day cooldown enforced via pre-commit hook</li>
        </ul>
      </section>
    </div>
  );
}
