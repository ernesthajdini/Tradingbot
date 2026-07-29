export const revalidate = 60;

import { fetchLatestScreen, fetchRecentScreens } from '@/lib/queries';
import type { CandidatePayload } from '@/lib/types';
import { ScreenCandidates } from '../components/screen-candidates';
import { ScreensTable } from '../components/screens-table';

export default async function CandidatesPage() {
  const [latest, recent] = await Promise.all([
    fetchLatestScreen('weekly'),
    fetchRecentScreens(10, 'weekly'),
  ]);

  const candidates = (latest?.candidates_payload || []) as CandidatePayload[];
  const noTrade = latest?.no_trade_week ?? !candidates.some(c => c.tier === 'live' && c.setup);
  const liveCands = candidates.filter(c => c.tier === 'live');
  // The Sunday run screens at 22:00 UTC: bid/ask are zeroed, so the live tier
  // voids before any gate is evaluated. Detect it from the near-miss ledger —
  // `some`, not `every`: void_reasons is truncated to 6 and only populated
  // when a chain existed, and one closed market closes them all.
  const quotesUnavailable = noTrade && liveCands.some(
    c => (c.void_reasons ?? []).some(r => r.includes('no live two-sided quotes')));

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Planning digest</h1>
        <p className="text-sm text-muted mt-1">
          The Sunday run that lands in your inbox. Planning only: it screens after the
          close and cannot stage a ticket — those come from the weekday 15:05 UTC run
          (see Live). Last ran{' '}
          <span className="text-text font-mono">
            {latest ? new Date(latest.ran_at).toLocaleString() : 'never'}
          </span>.
        </p>
      </div>

      {latest && (
        <div className={`rounded-lg border-2 p-4 ${
          noTrade ? 'border-accent/50 bg-accent/5' : 'border-success/60 bg-success/5'
        }`}>
          <div className="font-semibold">
            {quotesUnavailable
              ? '🗓 Planning snapshot — nothing can stage from a closed market.'
              : noTrade
                ? '🚫 Nothing to do this week — no real-money trade qualified.'
                : '✅ Staged ticket(s) below are waiting for your approve/reject in IBKR.'}
          </div>
          <div className="text-xs text-muted mt-1">
            {quotesUnavailable
              ? 'Prices are the last close. Bid/ask are zeroed after hours, so the live tier voids before any gate is evaluated — a data condition, not a market verdict. Tickets come from the weekday 15:05 UTC run — see Live.'
              : noTrade
                ? 'No spread passed the safety gates. Sitting out is the designed outcome, not a failure.'
                : 'Approve or reject only — never modify a ticket by hand.'}
          </div>
        </div>
      )}

      <ScreenCandidates candidates={candidates} />

      <section>
        <h2 className="text-lg font-medium mb-3">Recent planning screens</h2>
        <ScreensTable screens={recent} />
      </section>
    </div>
  );
}
