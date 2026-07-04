"""
Trade Journal — records every signal, trade, and outcome for learning.

This is the memory of the system. Every signal generated, every order placed,
every outcome observed gets logged here. The performance analyzer reads this
to determine what's working, what's failing, and how to adapt.

Storage: SQLite database (lightweight, no server needed, portable).
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class TradeJournal:
    """Persistent trade journal backed by SQLite."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path("trading_system/output/trade_journal.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    entry_price REAL,
                    stop_loss REAL,
                    target_price REAL,
                    risk_reward REAL,
                    reasoning TEXT,
                    model_contributions TEXT,
                    sub_signals TEXT,
                    market_regime TEXT,
                    was_executed INTEGER DEFAULT 0,
                    execution_status TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    shares INTEGER,
                    entry_price REAL,
                    entry_date TEXT,
                    exit_price REAL,
                    exit_date TEXT,
                    stop_loss REAL,
                    target_price REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    holding_days INTEGER,
                    exit_reason TEXT,
                    order_id TEXT,
                    status TEXT DEFAULT 'open',
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                );

                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    equity REAL,
                    cash REAL,
                    num_positions INTEGER,
                    total_invested REAL,
                    daily_pnl REAL,
                    daily_pnl_pct REAL,
                    positions_json TEXT,
                    signals_generated INTEGER,
                    signals_executed INTEGER,
                    market_regime TEXT
                );

                CREATE TABLE IF NOT EXISTS strategy_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    win_rate REAL,
                    avg_pnl REAL,
                    total_signals INTEGER,
                    total_trades INTEGER,
                    sharpe_30d REAL,
                    notes TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
                CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            """)
        logger.info(f"Trade journal initialized at {self.db_path}")

    @staticmethod
    def _safe_json(obj):
        """Convert numpy/non-serializable types before JSON encoding."""
        def convert(o):
            if hasattr(o, 'item'):  # numpy scalar
                return o.item()
            if hasattr(o, 'tolist'):  # numpy array
                return o.tolist()
            return o

        if isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [convert(v) for v in obj]
        return convert(obj)

    def log_signal(self, signal, sub_signals: dict | None = None, executed: bool = False,
                   execution_status: str = "") -> int:
        """Log a generated signal. Returns signal_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO signals (
                    timestamp, ticker, direction, confidence,
                    entry_price, stop_loss, target_price, risk_reward,
                    reasoning, model_contributions, sub_signals,
                    was_executed, execution_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                signal.ticker,
                signal.direction,
                float(signal.confidence),
                float(signal.entry_price),
                float(signal.stop_loss),
                float(signal.target_price),
                float(signal.risk_reward_ratio),
                json.dumps(self._safe_json(signal.reasoning)),
                json.dumps(self._safe_json(signal.model_contributions)),
                json.dumps(self._safe_json(sub_signals)) if sub_signals else None,
                1 if executed else 0,
                execution_status,
            ))
            signal_id = cursor.lastrowid
            logger.debug(f"Logged signal #{signal_id}: {signal.direction} {signal.ticker}")
            return signal_id

    def log_trade_open(self, signal_id: int, ticker: str, direction: str,
                       shares: int, entry_price: float, stop_loss: float,
                       target_price: float, order_id: str = "") -> int:
        """Log a new trade opening. Returns trade_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO trades (
                    signal_id, timestamp, ticker, direction, shares,
                    entry_price, entry_date, stop_loss, target_price,
                    order_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """, (
                signal_id,
                datetime.now().isoformat(),
                ticker,
                direction,
                shares,
                entry_price,
                datetime.now().strftime("%Y-%m-%d"),
                stop_loss,
                target_price,
                order_id,
            ))
            trade_id = cursor.lastrowid
            logger.info(f"Trade opened #{trade_id}: {direction} {shares} {ticker} @ ${entry_price:.2f}")
            return trade_id

    def log_trade_close(self, trade_id: int, exit_price: float, exit_reason: str):
        """Log a trade closing with outcome."""
        with sqlite3.connect(self.db_path) as conn:
            # Get the trade details
            row = conn.execute(
                "SELECT entry_price, shares, direction, entry_date FROM trades WHERE id = ?",
                (trade_id,)
            ).fetchone()

            if not row:
                logger.warning(f"Trade #{trade_id} not found")
                return

            entry_price, shares, direction, entry_date = row

            if direction == "BUY":
                pnl = (exit_price - entry_price) * shares
            else:
                pnl = (entry_price - exit_price) * shares

            pnl_pct = (exit_price - entry_price) / entry_price if direction == "BUY" else (entry_price - exit_price) / entry_price

            entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
            holding_days = (datetime.now() - entry_dt).days

            conn.execute("""
                UPDATE trades SET
                    exit_price = ?, exit_date = ?, pnl = ?, pnl_pct = ?,
                    holding_days = ?, exit_reason = ?, status = 'closed'
                WHERE id = ?
            """, (
                exit_price,
                datetime.now().strftime("%Y-%m-%d"),
                round(pnl, 2),
                round(pnl_pct, 4),
                holding_days,
                exit_reason,
                trade_id,
            ))
            logger.info(
                f"Trade closed #{trade_id}: PnL=${pnl:.2f} ({pnl_pct:+.2%}) "
                f"after {holding_days}d, reason={exit_reason}"
            )

    def log_daily_snapshot(self, equity: float, cash: float, positions: list,
                           signals_generated: int = 0, signals_executed: int = 0):
        """Log end-of-day portfolio state."""
        today = datetime.now().strftime("%Y-%m-%d")
        num_positions = len(positions)
        total_invested = sum(p.get("market_value", 0) for p in positions)
        daily_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
        daily_pnl_pct = daily_pnl / equity if equity > 0 else 0

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_snapshots (
                    date, equity, cash, num_positions, total_invested,
                    daily_pnl, daily_pnl_pct, positions_json,
                    signals_generated, signals_executed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today, equity, cash, num_positions, total_invested,
                round(daily_pnl, 2), round(daily_pnl_pct, 4),
                json.dumps(positions),
                signals_generated, signals_executed,
            ))

    def get_open_trades(self) -> list[dict]:
        """Get all currently open trades."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'open' ORDER BY entry_date"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_closed_trades(self, days: int | None = None) -> list[dict]:
        """Get closed trades, optionally limited to recent N days."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if days:
                rows = conn.execute("""
                    SELECT * FROM trades WHERE status = 'closed'
                    AND exit_date >= date('now', ?)
                    ORDER BY exit_date DESC
                """, (f"-{days} days",)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_date DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_signal_history(self, ticker: str | None = None, days: int = 30) -> list[dict]:
        """Get recent signal history."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if ticker:
                rows = conn.execute("""
                    SELECT * FROM signals WHERE ticker = ?
                    AND timestamp >= datetime('now', ?)
                    ORDER BY timestamp DESC
                """, (ticker, f"-{days} days")).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM signals
                    WHERE timestamp >= datetime('now', ?)
                    ORDER BY timestamp DESC
                """, (f"-{days} days",)).fetchall()
            return [dict(r) for r in rows]

    def get_performance_summary(self, days: int = 30) -> dict:
        """Get aggregate performance metrics over recent period."""
        closed = self.get_closed_trades(days=days)

        if not closed:
            return {
                "period_days": days,
                "total_trades": 0,
                "message": "No closed trades yet"
            }

        pnls = [t["pnl_pct"] for t in closed if t["pnl_pct"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        return {
            "period_days": days,
            "total_trades": len(closed),
            "win_rate": len(wins) / len(pnls) if pnls else 0,
            "avg_pnl_pct": sum(pnls) / len(pnls) if pnls else 0,
            "avg_win_pct": sum(wins) / len(wins) if wins else 0,
            "avg_loss_pct": sum(losses) / len(losses) if losses else 0,
            "total_pnl": sum(t["pnl"] for t in closed if t["pnl"] is not None),
            "best_trade": max(pnls) if pnls else 0,
            "worst_trade": min(pnls) if pnls else 0,
            "avg_holding_days": sum(t["holding_days"] for t in closed if t["holding_days"]) / len(closed),
            "by_direction": {
                "BUY": len([t for t in closed if t["direction"] == "BUY"]),
                "SELL": len([t for t in closed if t["direction"] == "SELL"]),
            },
        }

    def get_strategy_accuracy(self) -> dict:
        """
        Analyze which sub-signals were most predictive.
        Compares signal reasoning to trade outcomes.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT s.reasoning, s.model_contributions, s.sub_signals,
                       t.pnl_pct, t.exit_reason, t.direction
                FROM trades t
                JOIN signals s ON t.signal_id = s.id
                WHERE t.status = 'closed' AND t.pnl_pct IS NOT NULL
            """).fetchall()

        if not rows:
            return {"message": "No closed trades with linked signals yet"}

        strategy_results = {}
        for row in rows:
            try:
                contributions = json.loads(row["model_contributions"]) if row["model_contributions"] else {}
            except (json.JSONDecodeError, TypeError):
                contributions = {}

            for strategy, score in contributions.items():
                if strategy not in strategy_results:
                    strategy_results[strategy] = {"wins": 0, "losses": 0, "total_pnl": 0}

                if row["pnl_pct"] > 0:
                    strategy_results[strategy]["wins"] += 1
                else:
                    strategy_results[strategy]["losses"] += 1
                strategy_results[strategy]["total_pnl"] += row["pnl_pct"]

        # Calculate win rates
        for name, stats in strategy_results.items():
            total = stats["wins"] + stats["losses"]
            stats["win_rate"] = stats["wins"] / total if total > 0 else 0
            stats["total_trades"] = total
            stats["avg_pnl"] = stats["total_pnl"] / total if total > 0 else 0

        return strategy_results

    def get_equity_curve(self) -> list[dict]:
        """Get daily equity snapshots."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT date, equity, daily_pnl, daily_pnl_pct, num_positions "
                "FROM daily_snapshots ORDER BY date"
            ).fetchall()
            return [dict(r) for r in rows]

    def stats_summary(self) -> str:
        """Print a readable summary of the journal."""
        with sqlite3.connect(self.db_path) as conn:
            total_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            executed_signals = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE was_executed = 1"
            ).fetchone()[0]
            open_trades = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'open'"
            ).fetchone()[0]
            closed_trades = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'closed'"
            ).fetchone()[0]
            snapshots = conn.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0]

        perf = self.get_performance_summary()

        lines = [
            f"\n{'='*50}",
            f"  TRADE JOURNAL SUMMARY",
            f"{'='*50}",
            f"  Total signals logged:   {total_signals}",
            f"  Signals executed:       {executed_signals}",
            f"  Open trades:            {open_trades}",
            f"  Closed trades:          {closed_trades}",
            f"  Daily snapshots:        {snapshots}",
        ]

        if closed_trades > 0:
            lines.extend([
                f"\n  --- Performance (last 30 days) ---",
                f"  Win rate:         {perf.get('win_rate', 0):.1%}",
                f"  Avg PnL/trade:    {perf.get('avg_pnl_pct', 0):+.2%}",
                f"  Total PnL:        ${perf.get('total_pnl', 0):.2f}",
                f"  Best trade:       {perf.get('best_trade', 0):+.2%}",
                f"  Worst trade:      {perf.get('worst_trade', 0):+.2%}",
            ])

        lines.append(f"{'='*50}\n")
        return "\n".join(lines)
