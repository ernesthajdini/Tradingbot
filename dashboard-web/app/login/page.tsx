'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { createClient } from '@/lib/supabase/client';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const supabase = createClient();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setLoading(false);
    if (error) {
      setError(error.message === 'Invalid login credentials'
        ? 'Wrong email or password.'
        : error.message);
      return;
    }
    router.push('/');
    router.refresh();
  }

  return (
    <div className="min-h-[70vh] flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="text-2xl font-semibold text-accent">CSP Screener</div>
          <div className="text-sm text-muted mt-1">Sign in to view the dashboard</div>
        </div>

        <form onSubmit={handleSubmit} className="bg-panel border border-border rounded-lg p-6 space-y-4">
          <div>
            <label className="block text-xs text-muted uppercase tracking-wide mb-1.5">
              Email
            </label>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-bg border border-border rounded px-3 py-2 text-sm
                         focus:outline-none focus:border-accent"
            />
          </div>
          <div>
            <label className="block text-xs text-muted uppercase tracking-wide mb-1.5">
              Password
            </label>
            <input
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-bg border border-border rounded px-3 py-2 text-sm
                         focus:outline-none focus:border-accent"
            />
          </div>

          {error && (
            <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded px-3 py-2">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-accent/20 hover:bg-accent/30 text-accent border border-accent/40
                       rounded px-3 py-2 text-sm font-medium transition-colors
                       disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="text-xs text-muted text-center mt-4">
          Access is invite-only. Contact the administrator for an account.
        </p>
      </div>
    </div>
  );
}
