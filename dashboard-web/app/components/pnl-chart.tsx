'use client';

import {
  Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis, CartesianGrid, ReferenceLine,
} from 'recharts';

interface Point { date: string; cumulative: number; pnl: number; ticker: string; }

export function PnlChart({ data }: { data: Point[] }) {
  if (data.length === 0) {
    return (
      <div className="text-center py-12 text-muted">
        No closed virtual trades yet.
      </div>
    );
  }
  return (
    <div className="w-full" style={{ height: 320 }}>
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 10, right: 16, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3fb950" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#3fb950" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
          <XAxis
            dataKey="date" stroke="#8b949e" fontSize={11}
            tickFormatter={(v) => v.slice(5)}
          />
          <YAxis stroke="#8b949e" fontSize={11} tickFormatter={(v) => `$${v}`} />
          <Tooltip
            contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 6 }}
            labelStyle={{ color: '#c9d1d9' }}
            formatter={(value: number) => [`$${value.toFixed(2)}`, 'Cumulative']}
          />
          <ReferenceLine y={0} stroke="#484f58" strokeDasharray="3 3" />
          <Area
            type="monotone" dataKey="cumulative"
            stroke="#3fb950" strokeWidth={2}
            fill="url(#pnlGrad)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
