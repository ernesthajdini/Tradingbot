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

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Weekly candidates</h1>
        <p className="text-sm text-muted mt-1">
          The Sunday screen — the run that also lands in your inbox. Last ran{' '}
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
            {noTrade
              ? '🚫 Nothing to do this week — no real-money trade qualified.'
              : '✅ Staged ticket(s) below are waiting for your approve/reject in IBKR.'}
          </div>
          <div className="text-xs text-muted mt-1">
            {noTrade
              ? 'No spread passed the safety gates. Sitting out is the designed outcome, not a failure.'
              : 'Approve or reject only — never modify a ticket by hand.'}
          </div>
        </div>
      )}

      <ScreenCandidates candidates={candidates} />

      <section>
        <h2 className="text-lg font-medium mb-3">Recent weekly screens</h2>
        <ScreensTable screens={recent} />
      </section>
    </div>
  );
}
