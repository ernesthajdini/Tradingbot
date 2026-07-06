import './globals.css';
import type { Metadata } from 'next';
import { Nav } from './components/nav';
import { RealtimeRefresher } from './components/realtime-refresher';

export const metadata: Metadata = {
  title: 'CSP Screener',
  description: 'Cash-secured put screener — virtual track record dashboard',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-text font-sans">
        <Nav />
        <RealtimeRefresher />
        <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          {children}
        </main>
        <footer className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 text-xs text-muted border-t border-border mt-12">
          Read-only dashboard. Screener runs in GitHub Actions, data in Supabase.
          No real trades placed by this app.
        </footer>
      </body>
    </html>
  );
}
