'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { createClient } from '@/lib/supabase/client';

const ITEMS = [
  { href: '/', label: 'Dashboard', icon: '📊' },
  { href: '/candidates', label: 'Weekly', icon: '📥' },
  { href: '/daily', label: 'Daily', icon: '📅' },
  { href: '/portfolio', label: 'Portfolio', icon: '📈' },
  { href: '/track-record', label: 'Track Record', icon: '🎯' },
  { href: '/learning', label: 'Learning', icon: '🧠' },
  { href: '/system', label: 'System', icon: '🛠️' },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();

  // Hide nav chrome on the login page
  if (pathname.startsWith('/login')) return null;

  async function signOut() {
    const supabase = createClient();
    await supabase.auth.signOut();
    router.push('/login');
    router.refresh();
  }

  return (
    <nav className="sticky top-0 z-40 bg-panel border-b border-border">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex h-14 items-center justify-between">
          <Link href="/" className="text-accent font-semibold text-sm">
            CSP Screener
          </Link>
          <div className="flex items-center gap-1 sm:gap-2 overflow-x-auto">
            {ITEMS.map((item) => {
              const active = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`px-3 py-1.5 rounded text-sm whitespace-nowrap transition-colors ${
                    active
                      ? 'bg-accent/15 text-accent'
                      : 'text-muted hover:text-text hover:bg-border/40'
                  }`}
                >
                  <span className="mr-1.5">{item.icon}</span>
                  <span className="hidden sm:inline">{item.label}</span>
                </Link>
              );
            })}
            <button
              onClick={signOut}
              className="ml-1 px-3 py-1.5 rounded text-sm whitespace-nowrap text-muted
                         hover:text-danger hover:bg-danger/10 transition-colors"
              title="Sign out"
            >
              ⎋<span className="hidden sm:inline ml-1.5">Sign out</span>
            </button>
          </div>
        </div>
      </div>
    </nav>
  );
}
