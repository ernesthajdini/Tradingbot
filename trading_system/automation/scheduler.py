"""
Trading system scheduler.
Runs the full trading pipeline on a schedule:
  - Pre-market scan (9:00 AM ET)
  - Signal execution (9:35 AM ET, after market opens)
  - Midday check (12:30 PM ET)
  - End-of-day summary (4:05 PM ET, after close)
  - Weekly model retrain (Saturday 10:00 AM)

Can run as:
  1. Standalone daemon: python run.py auto start
  2. Single job: python run.py auto run-once
  3. Windows Task Scheduler: python run.py auto install
"""

import json
import logging
import time
import sched
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.config.settings import SystemConfig, get_config
from trading_system.automation.alerts import AlertManager

logger = logging.getLogger(__name__)

# US Eastern timezone (market hours)
ET = ZoneInfo("America/New_York")
# User's local timezone (GMT+2)
LOCAL_TZ = ZoneInfo("Europe/Athens")

# Market schedule
MARKET_OPEN = (9, 30)    # 9:30 AM ET
MARKET_CLOSE = (16, 0)   # 4:00 PM ET


def fmt_dual(dt_et: datetime) -> str:
    """Format a datetime showing both ET and local time."""
    local = dt_et.astimezone(LOCAL_TZ)
    return f"{dt_et.strftime('%H:%M ET')} ({local.strftime('%H:%M')} local)"


def et_to_local_str(hour: int, minute: int) -> str:
    """Convert an ET hour:minute to a 'HH:MM ET (HH:MM local)' string."""
    now = datetime.now(ET)
    et_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    local_dt = et_dt.astimezone(LOCAL_TZ)
    et_str = et_dt.strftime("%I:%M %p").lstrip("0")
    local_str = local_dt.strftime("%I:%M %p").lstrip("0")
    return f"{et_str} ET  ({local_str} local)"


def is_market_day(dt: datetime | None = None) -> bool:
    """Check if given date is a trading day (weekday, not a holiday)."""
    dt = dt or datetime.now(ET)
    # Simple check: weekday only. Could add holiday calendar later.
    return dt.weekday() < 5  # Mon=0 ... Fri=4


def next_market_time(hour: int, minute: int, dt: datetime | None = None) -> datetime:
    """Get next occurrence of a specific time on a market day."""
    dt = dt or datetime.now(ET)
    target = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target <= dt:
        target += timedelta(days=1)

    # Skip to next weekday
    while target.weekday() >= 5:
        target += timedelta(days=1)

    return target


class TradingScheduler:
    """
    Orchestrates automated trading operations.
    """

    def __init__(self, config: SystemConfig | None = None, dry_run: bool = True):
        self.config = config or get_config()
        self.dry_run = dry_run
        self.alerts = AlertManager()
        self._system = None  # lazy load to avoid circular imports
        self._journal = None
        self.state_file = Path(self.config.output_dir) / "scheduler_state.json"
        self.state = self._load_state()

    @property
    def journal(self):
        """Lazy-load trade journal."""
        if self._journal is None:
            from trading_system.automation.trade_journal import TradeJournal
            self._journal = TradeJournal()
        return self._journal

    @property
    def learner(self):
        """Lazy-load adaptive learner."""
        if not hasattr(self, '_learner') or self._learner is None:
            from trading_system.automation.adaptive_learner import AdaptiveLearner
            self._learner = AdaptiveLearner(self.journal)
        return self._learner

    @property
    def sentiment(self):
        """Lazy-load sentiment aggregator."""
        if not hasattr(self, '_sentiment') or self._sentiment is None:
            from trading_system.data.scrapers import SentimentAggregator
            self._sentiment = SentimentAggregator()
        return self._sentiment

    @property
    def system(self):
        """Lazy-load the trading system to avoid import issues."""
        if self._system is None:
            from trading_system.main import TradingSystem
            self._system = TradingSystem(self.config)
        return self._system

    def run_once(self):
        """
        Run the full pipeline once: scan, signal, risk-check, execute, report.
        This is the main job that runs on schedule.
        """
        now = datetime.now(ET)
        logger.info(f"Starting automated run at {fmt_dual(now)}")

        try:
            # 0. Apply learned weights + regime to signal generation
            learned = self.learner.get_learned_weights()
            if learned.get("strategy_weights"):
                self.system.rule_signals.set_weights(learned["strategy_weights"])
            if learned.get("min_confidence"):
                self.system.combiner.min_confidence = learned["min_confidence"]
            regime = learned.get("regime", "unknown")
            self.system.rule_signals.set_regime(regime)
            logger.info(f"Regime={regime} | min_confidence={self.system.combiner.min_confidence:.2f}")

            # 1. Scan for signals
            logger.info("Step 1: Scanning for signals...")
            signals = self.system.scan(use_ml=True)

            if signals:
                self.alerts.signal_alert(signals)
                logger.info(f"Found {len(signals)} signals")

                # 2. Execute signals (dry run by default)
                if self._is_market_hours():
                    logger.info(f"Step 2: Executing signals (dry_run={self.dry_run})...")
                    capital = self.config.backtest.initial_capital
                    results = self.system.broker.execute_signals(
                        signals, dry_run=self.dry_run, capital=capital
                    )
                    self.alerts.execution_alert(results, dry_run=self.dry_run)

                    # Log trades to journal — match by ticker, not position
                    result_by_ticker = {r["ticker"]: r for r in results}
                    for sig in signals:
                        result = result_by_ticker.get(sig.ticker)
                        if not result:
                            # Signal was skipped by broker (duplicate, no capital)
                            self.journal.log_signal(sig, executed=False, execution_status="skipped")
                            continue
                        status = result.get("status", "unknown")
                        signal_id = self.journal.log_signal(
                            sig, executed=True, execution_status=status
                        )
                        if result.get("filled") and status not in ("DRY_RUN", "error", "rejected"):
                            self.journal.log_trade_open(
                                signal_id=signal_id,
                                ticker=sig.ticker,
                                direction=sig.direction,
                                shares=result.get("shares", 1),
                                entry_price=result.get("avg_fill_price", sig.entry_price),
                                # Use the recalculated bracket prices (anchored to actual fill)
                                stop_loss=result.get("stop_loss", sig.stop_loss),
                                target_price=result.get("target", sig.target_price),
                                order_id=str(result.get("order_id", "")),
                            )
                else:
                    # Market closed — log signals as not executed
                    for sig in signals:
                        self.journal.log_signal(sig, executed=False, execution_status="market_closed")
                    logger.info("Market closed - skipping execution")
            else:
                logger.info("No actionable signals")

            # 3. Check time-based exits first
            self.check_time_exits()

            # 4. Sync trade outcomes (detect closed positions)
            self.sync_trade_outcomes()

            # 4. Portfolio check and daily snapshot
            self._check_portfolio()
            self._log_daily_snapshot(len(signals))

            # 4. Update state
            self.state["last_run"] = now.isoformat()
            self.state["last_signal_count"] = len(signals)
            self.state["runs_today"] = self.state.get("runs_today", 0) + 1
            self._save_state()

            logger.info("Automated run complete")

        except Exception as e:
            logger.error(f"Automated run failed: {e}")
            self.alerts.send("Automation Error", str(e), level="error")

    def run_premarket(self):
        """Pre-market analysis (run before 9:30 AM ET)."""
        logger.info("Running pre-market analysis...")

        try:
            # Run adaptive learning before scanning
            learn_result = self.learner.learn()
            if learn_result.get("status") == "learned":
                logger.info(f"Adaptive learning: iteration #{learn_result['iteration']}")

            signals = self.system.scan(use_ml=True)
            sentiment_tickers = [s.ticker for s in signals[:5]] if signals else ["AAPL", "NVDA", "TSLA"]

            # Get sentiment from all sources (Reddit, News, StockTwits)
            logger.info("Gathering sentiment from Reddit, News, StockTwits...")
            sentiment = self.sentiment.get_batch_sentiment(sentiment_tickers)

            # Get market mood
            market_mood = self.sentiment.get_market_mood()
            logger.info(f"Market mood: {market_mood.get('news_label', 'unknown')}")

            lines = [f"Pre-Market Report - {datetime.now(ET).strftime('%Y-%m-%d')}\n"]
            lines.append(f"Signals found: {len(signals)}\n")

            for s in signals:
                sent = sentiment.get(s.ticker, {})
                sent_label = sent.get("label", "N/A")
                lines.append(
                    f"  {s.direction:4s} {s.ticker:6s} conf={s.confidence:.2f} "
                    f"sentiment={sent_label}"
                )
                lines.append(f"    Entry=${s.entry_price:.2f} SL=${s.stop_loss:.2f} TP=${s.target_price:.2f}")
                if s.reasoning:
                    lines.append(f"    {' | '.join(s.reasoning[:2])}")
                lines.append("")

            body = "\n".join(lines)
            self.alerts.send("Pre-Market Report", body, level="signal")
            logger.info("Pre-market report sent")

        except Exception as e:
            logger.error(f"Pre-market analysis failed: {e}")
            self.alerts.send("Pre-Market Error", str(e), level="error")

    def run_eod_summary(self):
        """End-of-day portfolio summary."""
        logger.info("Running EOD summary...")

        # Sync trades first — detect any positions closed today
        self.sync_trade_outcomes()

        try:
            summary = {
                "equity": 0, "daily_pnl": 0, "daily_pnl_pct": 0,
                "num_positions": 0, "num_orders": 0, "cash": 0,
                "positions": [],
            }

            try:
                conn = self.system.broker.connect()
                if conn.get("status") == "connected":
                    summary["equity"] = conn.get("equity", 0)
                    summary["cash"] = conn.get("cash", 0)
                    summary["positions"] = self.system.broker.get_positions()
                    summary["num_positions"] = len(summary["positions"])
                    summary["num_orders"] = len(self.system.broker.get_open_orders())
            except Exception:
                logger.info("Broker not connected for EOD summary")

            self.alerts.daily_summary(summary)
        except Exception as e:
            logger.error(f"EOD summary failed: {e}")

    def run_retrain(self):
        """Retrain the ML model with latest data AND learn from trade outcomes."""
        logger.info("Starting weekly retrain + learning cycle...")
        try:
            # Step 1: Learn from trade outcomes
            learn_result = self.learner.learn()
            learn_status = learn_result.get("status", "unknown")
            logger.info(f"Adaptive learning: {learn_status}")

            # Step 2: Retrain ML model
            results = self.system.train_ml()
            n_folds = results.get("n_folds", 0)
            accuracies = [r["accuracy"] for r in results.get("results", []) if "accuracy" in r]
            avg_acc = sum(accuracies) / len(accuracies) if accuracies else 0

            # Step 3: Report
            body = (
                f"Weekly Retrain Complete\n\n"
                f"ML Model:\n"
                f"  Folds: {n_folds}\n"
                f"  Avg Accuracy: {avg_acc:.3f}\n"
                f"  Top features: {list(results.get('feature_importance', {}).keys())[:5]}\n\n"
                f"Adaptive Learning:\n"
                f"  Status: {learn_status}\n"
                f"  Trades analyzed: {learn_result.get('trades_analyzed', 0)}\n"
                f"  Regime: {self.learner.weights.get('regime', 'unknown')}\n"
            )

            # Include strategy weight changes if any
            lessons = learn_result.get("lessons", {})
            if lessons.get("strategy_weights"):
                body += "\n  Strategy adjustments:\n"
                for strat, adj in lessons["strategy_weights"].items():
                    body += f"    {strat}: {adj['old_weight']:.3f} -> {adj['new_weight']:.3f} (win rate: {adj['win_rate']:.1%})\n"

            self.alerts.send("Weekly Retrain + Learning", body, level="info")
        except Exception as e:
            logger.error(f"Retrain failed: {e}")
            self.alerts.send("Retrain Error", str(e), level="error")

    def run_daemon(self):
        """
        Run as a long-lived daemon with scheduled jobs.
        Schedule:
          - 9:00 AM ET: Pre-market scan
          - 9:35 AM ET: Execute signals
          - 12:30 PM ET: Midday re-scan
          - 4:05 PM ET: EOD summary
          - Saturday 10:00 AM: Model retrain
        """
        logger.info("Starting trading daemon...")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        self.alerts.send("Daemon Started",
                         f"Trading daemon started in {'DRY RUN' if self.dry_run else 'LIVE'} mode",
                         level="info")

        schedule = [
            (9, 0, "pre-market", self.run_premarket),
            (9, 35, "execute", self.run_once),
            (12, 30, "midday", self.run_once),
            (16, 5, "eod", self.run_eod_summary),
        ]

        try:
            while True:
                now = datetime.now(ET)

                if not is_market_day(now):
                    # Weekend — check for weekly retrain on Saturday
                    retrain_key = f"retrain_{now.strftime('%Y-%W')}"
                    if now.weekday() == 5 and now.hour >= 10 and not self.state.get(retrain_key):
                        self.run_retrain()
                        self.state[retrain_key] = now.isoformat()
                        self._save_state()

                    # Weekly digest on Sunday morning
                    digest_key = f"digest_{now.strftime('%Y-%W')}"
                    if now.weekday() == 6 and now.hour >= 9 and not self.state.get(digest_key):
                        self.run_weekly_digest()
                        self.state[digest_key] = now.isoformat()
                        self._save_state()

                    # Sleep until next day
                    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                    sleep_seconds = (tomorrow - now).total_seconds()
                    tomorrow_local = tomorrow.astimezone(LOCAL_TZ)
                    logger.info(f"Non-trading day. Sleeping until {tomorrow.strftime('%A %H:%M ET')} ({tomorrow_local.strftime('%H:%M')} local)")
                    time.sleep(min(sleep_seconds, 3600))  # max 1 hour sleep chunks
                    continue

                # Check each scheduled job
                for hour, minute, name, func in schedule:
                    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    diff = abs((now - target).total_seconds())

                    if diff < 30:  # within 30 seconds of scheduled time
                        last_key = f"last_{name}_{now.strftime('%Y-%m-%d')}"
                        if self.state.get(last_key) != now.strftime("%H:%M"):
                            logger.info(f"Running scheduled job: {name}")
                            func()
                            self.state[last_key] = now.strftime("%H:%M")
                            self._save_state()

                # Position management: every 20 minutes during market hours
                # (move stops to breakeven, upgrade to trailing as positions move favorably)
                if self._is_market_hours():
                    pm_key = f"pm_{now.strftime('%Y-%m-%d_%H')}_{now.minute // 20}"
                    if not self.state.get(pm_key):
                        try:
                            self.manage_positions()
                            self.state[pm_key] = now.isoformat()
                            self._save_state()
                        except Exception as e:
                            logger.debug(f"Position management failed: {e}")

                # Sleep 30 seconds between checks
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("Daemon stopped by user")
            self.alerts.send("Daemon Stopped", "Trading daemon stopped by user", level="info")

    def run_weekly_digest(self):
        """
        Generate a weekly performance summary on Sunday.
        Aggregates: trades, PnL, win rate, regime, top/bottom performers.
        """
        try:
            closed_week = [
                t for t in self.journal.get_closed_trades(days=7)
                if t.get("pnl") is not None
            ]
            open_trades = self.journal.get_open_trades()

            wins = [t for t in closed_week if (t.get("pnl") or 0) > 0]
            losses = [t for t in closed_week if (t.get("pnl") or 0) <= 0]
            total_pnl = sum((t.get("pnl") or 0) for t in closed_week)
            win_rate = len(wins) / len(closed_week) if closed_week else 0

            # All-time stats
            all_closed = self.journal.get_closed_trades()
            all_pnl = sum((t.get("pnl") or 0) for t in all_closed if t.get("pnl") is not None)
            total_trades = len(all_closed)

            # Current learner state
            learned = self.learner.get_learned_weights()
            regime = learned.get("regime", "unknown")
            min_conf = learned.get("min_confidence", 0.30)
            iterations = learned.get("learning_iterations", 0)

            # Best / worst this week
            ranked = sorted(closed_week, key=lambda t: (t.get("pnl_pct") or 0))
            worst = ranked[0] if ranked else None
            best = ranked[-1] if ranked else None

            body = f"""
WEEKLY DIGEST — {datetime.now(ET).strftime('%Y-%m-%d')}

This week (last 7 days):
  Closed trades:    {len(closed_week)}
  Wins:             {len(wins)} ({win_rate:.0%})
  Losses:           {len(losses)}
  Net PnL:          ${total_pnl:+.2f}
  Best trade:       {best['ticker'] if best else '-'} {(best.get('pnl_pct', 0) or 0)*100:+.2f}% (${best.get('pnl', 0):+.2f})
  Worst trade:      {worst['ticker'] if worst else '-'} {(worst.get('pnl_pct', 0) or 0)*100:+.2f}% (${worst.get('pnl', 0):+.2f})

Open positions:   {len(open_trades)}
""".rstrip()
            for t in open_trades[:10]:
                body += f"\n  {t['ticker']:6s} {t['direction']:4s} {t['shares']} @ ${t['entry_price']:.2f}"

            body += f"""

System state:
  Regime:           {regime}
  Min confidence:   {min_conf:.2f}
  Learning iters:   {iterations}

All-time:
  Total trades:     {total_trades}
  Total PnL:        ${all_pnl:+.2f}
"""
            self.alerts.send("Weekly Digest", body, level="info")
            logger.info("Weekly digest sent")
        except Exception as e:
            logger.error(f"Weekly digest failed: {e}")

    def manage_positions(self):
        """
        Active position management: move stops to breakeven and upgrade to
        trailing stops as positions move favorably. Runs every 20 minutes
        during market hours.
        """
        try:
            open_trades = self.journal.get_open_trades()
            if not open_trades:
                return

            actions = self.system.broker.manage_open_positions(open_trades)
            if actions:
                logger.info(f"Position management: {len(actions)} actions taken")
                for a in actions:
                    self.alerts.send(
                        f"Stop adjusted: {a['ticker']}",
                        f"{a['action']} — current ${a['current_price']:.2f}, R={a.get('r_multiple', 0):.2f}",
                        level="info",
                    )
                    # Update journal with new stop
                    if a['action'] == 'breakeven_stop':
                        # Find trade by ticker and update its stop_loss
                        import sqlite3
                        conn = sqlite3.connect(self.journal.db_path)
                        conn.execute(
                            "UPDATE trades SET stop_loss = ? WHERE ticker = ? AND status = 'open'",
                            (a['new_stop'], a['ticker'])
                        )
                        conn.commit()
                        conn.close()
        except Exception as e:
            logger.debug(f"Position management failed: {e}")

    def check_time_exits(self):
        """
        Close positions that have been open > 10 trading days.
        Prevents capital from being locked in stagnant trades.
        """
        try:
            open_trades = self.journal.get_open_trades()
            if not open_trades:
                return

            conn = self.system.broker.connect()
            if conn.get("status") != "connected":
                return

            for trade in open_trades:
                entry_date = trade.get("entry_date", "")
                if not entry_date:
                    continue

                days_held = (datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d")).days
                if days_held >= 14:  # ~10 trading days
                    ticker = trade["ticker"]
                    logger.info(f"Time exit: {ticker} held for {days_held} days, closing...")
                    result = self.system.broker.close_position(ticker)
                    if result.get("status") == "close_submitted":
                        self.alerts.send(
                            f"Time Exit: {ticker}",
                            f"Closed {ticker} after {days_held} days (max hold exceeded)",
                            level="signal",
                        )

        except Exception as e:
            logger.debug(f"Time exit check failed: {e}")

    def sync_trade_outcomes(self):
        """
        Check IBKR positions against journal open trades.
        If a journal trade is open but the IBKR position is gone,
        the stop-loss or take-profit was hit — log the close.
        """
        try:
            conn = self.system.broker.connect()
            if conn.get("status") != "connected":
                return

            # Get current IBKR positions
            ibkr_positions = self.system.broker.get_positions()
            ibkr_tickers = {p["ticker"] for p in ibkr_positions}

            # Get journal open trades
            open_trades = self.journal.get_open_trades()

            for trade in open_trades:
                ticker = trade["ticker"]

                # SAFETY: don't sync-close trades that were just opened today.
                # IBKR positions API can lag a few seconds after a fill, and
                # closing prematurely creates ghost trades with fake PnL.
                # A real stop/target close shows up on the next scheduled job.
                trade_ts = trade.get("timestamp", "")
                if trade_ts:
                    try:
                        opened = datetime.fromisoformat(trade_ts)
                        age_minutes = (datetime.now() - opened).total_seconds() / 60
                        if age_minutes < 30:  # too fresh — let IBKR settle
                            logger.debug(f"Skipping sync for {ticker}: opened {age_minutes:.1f} min ago")
                            continue
                    except Exception:
                        pass

                if ticker not in ibkr_tickers:
                    # Position is gone from IBKR — it was closed (stop or target hit)
                    # Try to determine exit price from the trade's stop/target
                    entry = trade.get("entry_price", 0)
                    stop = trade.get("stop_loss", 0)
                    target = trade.get("target_price", 0)

                    # Check executed orders for actual fill price
                    exit_price = entry  # fallback
                    exit_reason = "unknown"

                    # If we can't determine exact exit, estimate based on
                    # which was closer to last known price
                    try:
                        price_data = self.system.pipeline.get_market_data(ticker)
                        if price_data is not None and not price_data.empty:
                            last_price = float(price_data["Close"].iloc[-1])
                            # Check which exit was hit
                            if trade["direction"] == "BUY":
                                if last_price <= stop:
                                    exit_price = stop
                                    exit_reason = "stop_loss"
                                elif last_price >= target:
                                    exit_price = target
                                    exit_reason = "take_profit"
                                else:
                                    exit_price = last_price
                                    exit_reason = "position_closed"
                            else:  # SELL (short)
                                if last_price >= stop:
                                    exit_price = stop
                                    exit_reason = "stop_loss"
                                elif last_price <= target:
                                    exit_price = target
                                    exit_reason = "take_profit"
                                else:
                                    exit_price = last_price
                                    exit_reason = "position_closed"
                    except Exception:
                        exit_reason = "position_closed"

                    # Log the close
                    self.journal.log_trade_close(
                        trade_id=trade["id"],
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                    )

                    self.alerts.send(
                        f"Trade Closed: {ticker}",
                        f"{trade['direction']} {trade.get('shares', '?')} {ticker} "
                        f"closed via {exit_reason} @ ${exit_price:.2f}",
                        level="signal",
                    )

        except Exception as e:
            logger.debug(f"Trade sync failed: {e}")

    def _log_daily_snapshot(self, signals_count: int = 0):
        """Log daily portfolio snapshot to journal."""
        try:
            conn = self.system.broker.connect()
            if conn.get("status") == "connected":
                positions = self.system.broker.get_positions()
                self.journal.log_daily_snapshot(
                    equity=conn.get("equity", 0),
                    cash=conn.get("cash", 0),
                    positions=positions,
                    signals_generated=signals_count,
                )
        except Exception:
            pass

    def _is_market_hours(self) -> bool:
        """Check if market is currently open."""
        now = datetime.now(ET)
        if not is_market_day(now):
            return False
        market_open = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1])
        market_close = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1])
        return market_open <= now <= market_close

    def _check_portfolio(self):
        """Check portfolio for risk breaches."""
        try:
            conn = self.system.broker.connect()
            if conn.get("status") != "connected":
                return

            positions = self.system.broker.get_positions()
            if not positions:
                return

            # Check for big losers
            for p in positions:
                if p["unrealized_pnl_pct"] < -0.10:  # down >10%
                    self.alerts.risk_alert(
                        f"{p['ticker']} is down {p['unrealized_pnl_pct']:.1%} "
                        f"(${p['unrealized_pnl']:.2f}). Consider closing."
                    )

        except Exception:
            pass  # broker not connected is fine

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {}

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2, default=str))


def create_windows_task(task_name: str = "TradingSystem", script_path: str | None = None):
    """
    Generate a .bat file and instructions to set up Windows Task Scheduler.
    """
    if script_path is None:
        script_path = str(Path(__file__).resolve().parent.parent.parent / "run.py")

    python_path = "python"
    bat_content = f"""@echo off
cd /d "{Path(script_path).parent}"
{python_path} "{script_path}" auto run-once >> trading_system\\output\\logs\\scheduler.log 2>&1
"""

    bat_path = Path(script_path).parent / "run_scheduled.bat"
    bat_path.write_text(bat_content)

    daemon_bat = f"""@echo off
cd /d "{Path(script_path).parent}"
{python_path} "{script_path}" auto start
"""
    daemon_path = Path(script_path).parent / "run_daemon.bat"
    daemon_path.write_text(daemon_bat)

    instructions = f"""
Windows Task Scheduler Setup
{'='*50}

Two options for automation:

OPTION 1: Run as Daemon (Recommended)
  Double-click: {daemon_path}
  Or run: python run.py auto start
  This runs continuously and handles all scheduling internally.
  Add to Windows Startup folder for auto-start on boot.

OPTION 2: Windows Task Scheduler (for single jobs)
  1. Open Task Scheduler (taskschd.msc)
  2. Click "Create Basic Task"
  3. Name: "{task_name}"
  4. Trigger: Daily, 9:00 AM
  5. Action: Start a program
     Program: "{bat_path}"
  6. Check "Open the Properties dialog" -> Finish
  7. In Properties:
     - Check "Run whether user is logged on or not"
     - Check "Run with highest privileges"

Created files:
  {bat_path}     (single run)
  {daemon_path}  (daemon mode)
"""
    print(instructions)
    return str(bat_path), str(daemon_path)
