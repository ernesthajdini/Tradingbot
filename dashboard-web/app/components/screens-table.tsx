import type { Screen } from '@/lib/types';

/** Compact history table of past screener runs, shared by /candidates and /daily. */
export function ScreensTable({ screens }: { screens: Screen[] }) {
  if (screens.length === 0) {
    return <div className="text-sm text-muted">No runs recorded yet.</div>;
  }
  return (
    <div className="bg-panel border border-border rounded-lg overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-muted">
          <tr className="border-b border-border">
            <th className="text-left py-3 px-4">Date</th>
            <th className="text-right py-3 px-4">Universe</th>
            <th className="text-right py-3 px-4">Passed</th>
            <th className="text-right py-3 px-4">Candidates</th>
            <th className="text-right py-3 px-4">Paper opened</th>
            <th className="text-right py-3 px-4">Paper closed</th>
            <th className="text-right py-3 px-4">Closed P&L</th>
            <th className="text-right py-3 px-4">VIX</th>
          </tr>
        </thead>
        <tbody>
          {screens.map((s) => (
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
  );
}
