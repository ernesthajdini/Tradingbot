export const revalidate = 60;

import { fetchLatestScreen, fetchRecentScreens } from '@/lib/queries';
import type { CandidatePayload } from '@/lib/types';
import { ScreenCandidates } from '../components/screen-candidates';
import { ScreensTable } from '../components/screens-table';

// Mirrors VIX_KILL_SWITCH in csp_screener/config.py (display-only heuristic).
const VIX_KILL_SWITCH = 35;

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

      {latest && latest.vix != null && Number(latest.vix) > VIX_KILL_SWITCH && (
        <div className="rounded-lg border-2 border-danger/60 bg-danger/5 p-4">
          <div className="font-semibold text-danger">
            ⛔ VIX {Number(latest.vix).toFixed(1)} — kill switch active
          </div>
          <div className="text-xs text-muted mt-1">
            Selling premium into a vol spike is the classic retail blow-up. The screener stood
            down and generated no candidates. Open positions ride to their normal exits.
          </div>
        </div>
      )}

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
