export const revalidate = 30;

import { fetchLatestScreen, fetchRecentSystemEvents } from '@/lib/queries';
import { MetricCard } from '../components/metric-card';

export default async function SystemPage() {
  const [latest, events] = await Promise.all([
    fetchLatestScreen(),
    fetchRecentSystemEvents(50),
  ]);

  const lastRun = latest?.ran_at ? new Date(latest.ran_at) : null;
  const hoursSinceLastRun = lastRun
    ? (Date.now() - lastRun.getTime()) / 3_600_000
    : null;

  const liveness =
    hoursSinceLastRun === null
      ? { label: 'No data yet', trend: 'neutral' as const, color: 'text-muted' }
      : hoursSinceLastRun < 24 * 8
      ? { label: `${hoursSinceLastRun.toFixed(1)}h ago`, trend: 'up' as const, color: 'text-success' }
      : { label: `${(hoursSinceLastRun / 24).toFixed(1)}d ago — STALE`, trend: 'down' as const, color: 'text-danger' };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">System health</h1>
        <p className="text-sm text-muted mt-1">
          Liveness, recent events, and pipeline state.
        </p>
      </div>

      <section className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <MetricCard
          label="Last screener run"
          value={lastRun ? lastRun.toLocaleDateString() : 'never'}
          sub={liveness.label}
          trend={liveness.trend}
        />
        <MetricCard label="Recent events" value={events.length} />
        <MetricCard
          label="Last email sent"
          value={latest?.email_sent ? 'Yes' : 'No'}
          trend={latest?.email_sent ? 'up' : 'down'}
        />
      </section>

      <section>
        <h2 className="text-lg font-medium mb-3">Recent system events</h2>
        {events.length === 0 ? (
          <div className="text-sm text-muted">No system events yet.</div>
        ) : (
          <div className="bg-panel border border-border rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr className="border-b border-border">
                  <th className="text-left py-3 px-4">Time</th>
                  <th className="text-left py-3 px-4">Event</th>
                  <th className="text-left py-3 px-4">Details</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id} className="border-b border-border/50 last:border-0 align-top">
                    <td className="py-3 px-4 font-mono text-xs whitespace-nowrap">
                      {new Date(e.at).toLocaleString()}
                    </td>
                    <td className="py-3 px-4 font-mono">{e.event}</td>
                    <td className="py-3 px-4 text-xs text-muted font-mono">
                      <pre className="whitespace-pre-wrap break-all max-w-2xl">
                        {JSON.stringify(e.payload, null, 2)}
                      </pre>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
