'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const ITEMS = [
  { href: '/', label: 'Dashboard', icon: '📊' },
  { href: '/candidates', label: 'Candidates', icon: '📥' },
  { href: '/portfolio', label: 'Portfolio', icon: '📈' },
  { href: '/track-record', label: 'Track Record', icon: '🎯' },
  { href: '/learning', label: 'Learning', icon: '🧠' },
  { href: '/system', label: 'System', icon: '🛠️' },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="sticky top-0 z-40 bg-panel border-b border-border">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex h-14 items-center justify-between">
          <Link href="/" className="text-accent font-semibold text-sm">
            CSP Screener
          </Link>
          <div className="flex gap-1 sm:gap-2 overflow-x-auto">
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
          </div>
        </div>
      </div>
    </nav>
  );
}
