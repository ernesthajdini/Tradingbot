export const revalidate = 60;

import { fetchLatestScreen, fetchRecentScreens } from '@/lib/queries';
import type { CandidatePayload } from '@/lib/types';
import { ScreenCandidates } from '../components/screen-candidates';
import { ScreensTable } from '../components/screens-table';

export default async function DailyPage() {
  const [latest, recent] = await Promise.all([
    fetchLatestScreen('daily'),
    fetchRecentScreens(10, 'daily'),
  ]);

  const candidates = (latest?.candidates_payload || []) as CandidatePayload[];

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Daily indications</h1>
        <p className="text-sm text-muted mt-1">
          Same screen as Sunday, run every weekday after the close. No email — this tab is the
          only surface. Qualifying names open paper positions automatically (one per ticker), so
          the track record grows daily instead of weekly.
        </p>
        <p className="text-sm text-muted mt-1">
          Last daily run:{' '}
          <span className="text-text font-mono">
            {latest ? new Date(latest.ran_at).toLocaleString() : 'none yet'}
          </span>
          {latest?.vix != null && <> · VIX {latest.vix}</>}
        </p>
      </div>

      {latest ? (
        <ScreenCandidates candidates={candidates} />
      ) : (
        <div className="bg-panel border border-border rounded-lg p-8 text-center text-muted">
          No daily runs yet. The first one lands after the next market close
          (GitHub Actions, Mon–Fri) — or trigger the “daily-screen” job manually
          from the Actions tab.
        </div>
      )}

      <section>
        <h2 className="text-lg font-medium mb-3">Recent daily runs</h2>
        <ScreensTable screens={recent} />
      </section>
    </div>
  );
}
