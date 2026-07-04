#!/usr/bin/env python3
"""
Trading Signal System -- CLI Entry Point.

Usage:
    python run.py scan                     # Scan all stocks, show signals
    python run.py scan --ml                # Scan with ML model
    python run.py analyze AAPL             # Deep analysis of one stock
    python run.py sentiment AAPL NVDA      # News sentiment analysis
    python run.py train                    # Train ML model
    python run.py backtest                 # Run historical backtest
    python run.py paper status             # IBKR connection status
    python run.py paper execute            # Dry-run signal execution via IBKR
    python run.py auto start               # Start automation daemon
    python run.py auto run-once            # Run one scan+execute cycle
    python run.py auto install             # Set up Windows Task Scheduler
"""

import argparse
import json
import sys

from trading_system.main import TradingSystem
from trading_system.utils.logger import setup_logging


def cmd_scan(system: TradingSystem, args):
    """Run market scan and display signals."""
    tickers = args.tickers if args.tickers else None
    signals = system.scan(tickers=tickers, use_ml=args.ml)

    if not signals:
        print("\nNo actionable signals found.")
        return

    print(f"\n{'='*80}")
    print(f"  TRADING SIGNALS — {len(signals)} actionable")
    print(f"{'='*80}")

    for i, sig in enumerate(signals, 1):
        print(f"\n  #{i}  {sig}")

    print(f"\n{'='*80}")
    print("  Note: These are algorithmic signals, NOT financial advice.")
    print("  Always do your own research before trading.")
    print(f"{'='*80}\n")


def cmd_analyze(system: TradingSystem, args):
    """Deep analysis of a single ticker."""
    result = system.analyze_ticker(args.ticker)

    if "error" in result:
        print(f"Error: {result['error']}")
        return

    print(f"\n{'='*60}")
    print(f"  ANALYSIS: {result['ticker']}  —  ${result['price']}")
    print(f"  Date: {result['date']}")
    print(f"{'='*60}")

    print(f"\n  Signal: {result['signal']['direction']}  "
          f"(score={result['signal']['score']:.3f}, "
          f"conf={result['signal']['confidence']:.3f})")
    print(f"  Reason: {result['signal']['reasoning']}")

    print(f"\n  --- Technical Indicators ---")
    for k, v in result["technical"].items():
        print(f"    {k:25s}: {v}")

    print(f"\n  --- Returns ---")
    for k, v in result["returns"].items():
        print(f"    {k:25s}: {v:+.2%}")

    print(f"\n  --- Sub-Signal Scores ---")
    for k, v in result["sub_signals"].items():
        bar = "+" * int(abs(v) * 20) if v > 0 else "-" * int(abs(v) * 20)
        print(f"    {k:25s}: {v:+.3f}  {bar}")

    # Sentiment
    if "sentiment" in result:
        sent = result["sentiment"]
        print(f"\n  --- News Sentiment ({sent.get('article_count', 0)} articles) ---")
        print(f"    Score: {sent.get('score', 0):+.3f}  ({sent.get('label', 'N/A')})")
        print(f"    Confidence: {sent.get('confidence', 0):.2f}")
        if sent.get("details"):
            for d in sent["details"][:3]:
                icon = "+" if d["score"] > 0 else "-" if d["score"] < 0 else " "
                print(f"    {icon} {d['title'][:65]}")

    print(f"\n{'='*60}")
    print("  This is algorithmic analysis, NOT financial advice.")
    print(f"{'='*60}\n")


def cmd_train(system: TradingSystem, args):
    """Train the ML model."""
    tickers = args.tickers if args.tickers else None
    results = system.train_ml(tickers=tickers)

    if "error" in results:
        print(f"Error: {results['error']}")
        return

    print(f"\n{'='*60}")
    print(f"  ML TRAINING RESULTS")
    print(f"{'='*60}")
    print(f"  Walk-forward folds: {results['n_folds']}")

    if results["results"]:
        accuracies = [r["accuracy"] for r in results["results"] if "accuracy" in r]
        if accuracies:
            print(f"  Average accuracy:   {sum(accuracies)/len(accuracies):.3f}")
            print(f"  Best fold:          {max(accuracies):.3f}")
            print(f"  Worst fold:         {min(accuracies):.3f}")

    print(f"\n  Top Features:")
    for feat, imp in list(results.get("feature_importance", {}).items())[:10]:
        bar = "#" * int(imp * 100)
        print(f"    {feat:30s}: {imp:.4f}  {bar}")

    print(f"{'='*60}\n")


def cmd_backtest(system: TradingSystem, args):
    """Run historical backtest."""
    tickers = args.tickers if args.tickers else None
    result = system.backtest(tickers=tickers)
    print(result.summary())


def cmd_sentiment(system: TradingSystem, args):
    """Get sentiment analysis for tickers."""
    tickers = args.tickers
    print(f"\n{'='*70}")
    print(f"  SENTIMENT ANALYSIS")
    print(f"{'='*70}")

    for ticker in tickers:
        result = system.sentiment.get_sentiment(ticker, days_back=args.days)
        score_bar = "+" * int(max(result["score"], 0) * 20) + "-" * int(max(-result["score"], 0) * 20)
        print(f"\n  {ticker:6s}  {result['label']:8s}  "
              f"score={result['score']:+.3f}  "
              f"conf={result['confidence']:.2f}  "
              f"articles={result['article_count']}  {score_bar}")
        if result.get("details"):
            for d in result["details"][:3]:
                sentiment_icon = "+" if d["score"] > 0 else "-" if d["score"] < 0 else " "
                print(f"    {sentiment_icon} {d['title'][:70]}")

    print(f"\n{'='*70}")
    print("  Sentiment is one signal among many. NOT financial advice.")
    print(f"{'='*70}\n")


def cmd_paper(system: TradingSystem, args):
    """IBKR paper trading operations."""
    broker = system.broker

    if args.action == "status":
        result = broker.connect()
        if result["status"] == "connected":
            print(f"\n  IBKR {result['mode']} Trading Account")
            print(f"  {'='*40}")
            print(f"  Equity:         ${result.get('equity', 0):>12,.2f}")
            print(f"  Cash:           ${result.get('cash', 0):>12,.2f}")
            print(f"  Buying Power:   ${result.get('buying_power', 0):>12,.2f}")
            print(f"  Unrealized PnL: ${result.get('unrealized_pnl', 0):>12,.2f}")
            print()
        else:
            print(f"\n  Connection failed: {result.get('message', 'unknown error')}")
            if result.get("help"):
                print(f"\n  {result['help']}")
            print()

    elif args.action == "positions":
        conn = broker.connect()
        if conn["status"] != "connected":
            print(f"  Not connected: {conn.get('message', '')}")
            return
        positions = broker.get_positions()
        if not positions:
            print("\n  No open positions.\n")
        else:
            print(f"\n  {'Ticker':8s} {'Shares':>8s} {'Entry':>10s} {'Current':>10s} {'PnL':>10s} {'PnL%':>8s}")
            print(f"  {'-'*56}")
            for p in positions:
                print(f"  {p['ticker']:8s} {p['shares']:>8d} "
                      f"${p['entry_price']:>9.2f} ${p['current_price']:>9.2f} "
                      f"${p['unrealized_pnl']:>9.2f} {p['unrealized_pnl_pct']:>+7.2%}")
            print()

    elif args.action == "orders":
        conn = broker.connect()
        if conn["status"] != "connected":
            print(f"  Not connected: {conn.get('message', '')}")
            return
        orders = broker.get_open_orders()
        if not orders:
            print("\n  No open orders.\n")
        else:
            print(f"\n  {'ID':>8s} {'Ticker':8s} {'Side':6s} {'Shares':>8s} {'Type':8s} {'Status':12s}")
            print(f"  {'-'*56}")
            for o in orders:
                print(f"  {o['order_id']:>8d} {o['ticker']:8s} {o['side']:6s} "
                      f"{o['shares']:>8d} {o['type']:8s} {o['status']:12s}")
            print()

    elif args.action == "execute":
        signals = system.scan(use_ml=True)
        if not signals:
            print("No actionable signals to execute.")
            return

        print(f"\n  Found {len(signals)} signals to execute:")
        for s in signals:
            print(f"    {s}")

        capital = system.config.backtest.initial_capital
        results = broker.execute_signals(signals, dry_run=args.dry_run, capital=capital)
        mode = "DRY RUN" if args.dry_run else "EXECUTED"
        print(f"\n  [{mode}] {len(results)} orders processed (capital: ${capital:,.0f})")
        for r in results:
            risk = r.get('risk_amount', '?')
            print(f"    {r['side']:4s} {r.get('shares', '?'):>3} {r['ticker']:6s} "
                  f"@ ${r.get('entry', 0):.2f}  risk=${risk}  -- {r['status']}")
        print()

    elif args.action == "cancel":
        conn = broker.connect()
        if conn["status"] != "connected":
            print(f"  Not connected: {conn.get('message', '')}")
            return
        result = broker.cancel_all_orders()
        print(f"  {result['status']}")

    elif args.action == "close":
        conn = broker.connect()
        if conn["status"] != "connected":
            print(f"  Not connected: {conn.get('message', '')}")
            return
        result = broker.close_all_positions()
        print(f"  {result['status']}")
        for r in result.get("results", []):
            print(f"    {r['ticker']}: {r['status']}")


def main():
    parser = argparse.ArgumentParser(
        description="AI Trading Signal System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Note: All output is for informational purposes only, NOT financial advice.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan stocks for trading signals")
    scan_parser.add_argument("--tickers", nargs="+", help="Specific tickers to scan")
    scan_parser.add_argument("--ml", action="store_true", help="Include ML model signals")

    # analyze
    analyze_parser = subparsers.add_parser("analyze", help="Deep analysis of a single ticker")
    analyze_parser.add_argument("ticker", help="Ticker symbol (e.g., AAPL)")

    # train
    train_parser = subparsers.add_parser("train", help="Train the ML model")
    train_parser.add_argument("--tickers", nargs="+", help="Tickers to use for training")

    # backtest
    bt_parser = subparsers.add_parser("backtest", help="Run historical backtest")
    bt_parser.add_argument("--tickers", nargs="+", help="Tickers to backtest")

    # sentiment
    sent_parser = subparsers.add_parser("sentiment", help="Analyze news sentiment")
    sent_parser.add_argument("tickers", nargs="+", help="Tickers to analyze")
    sent_parser.add_argument("--days", type=int, default=7, help="Days of news to analyze")

    # IBKR paper trading
    paper_parser = subparsers.add_parser("paper", help="IBKR paper trading")
    paper_parser.add_argument("action",
                              choices=["status", "positions", "orders", "execute", "cancel", "close"],
                              help="status=account info, positions=open positions, "
                                   "orders=pending orders, execute=run signals, "
                                   "cancel=cancel all orders, close=close all positions")
    paper_parser.add_argument("--dry-run", action="store_true", default=True,
                              help="Simulate orders without placing them (default: True)")
    paper_parser.add_argument("--live", action="store_true",
                              help="Actually place paper trading orders")
    paper_parser.add_argument("--port", type=int, default=None,
                              help="TWS port (7497=paper, 7496=live, 4002=gateway paper)")

    # journal
    journal_parser = subparsers.add_parser("journal", help="View trade journal and learning data")
    journal_parser.add_argument("action",
                                choices=["summary", "signals", "trades", "performance", "strategy"],
                                help="summary=overview, signals=recent signals, "
                                     "trades=trade history, performance=metrics, "
                                     "strategy=which strategies work best")
    journal_parser.add_argument("--days", type=int, default=30, help="Lookback period in days")

    # learn
    learn_parser = subparsers.add_parser("learn", help="Adaptive learning from trade outcomes")
    learn_parser.add_argument("action", choices=["status", "run"],
                              help="status=show learned weights, run=trigger learning now")

    # automation
    auto_parser = subparsers.add_parser("auto", help="Automation and scheduling")
    auto_parser.add_argument("action",
                             choices=["start", "run-once", "premarket", "eod", "retrain", "install"],
                             help="start=run daemon, run-once=single cycle, "
                                  "premarket=pre-market report, eod=end-of-day summary, "
                                  "retrain=retrain ML model, install=setup Windows Task Scheduler")
    auto_parser.add_argument("--dry-run", action="store_true", default=True,
                             help="Simulate orders without placing them (default: True)")
    auto_parser.add_argument("--live", action="store_true",
                             help="Actually place orders (CAREFUL!)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Handle live/dry-run flags
    if args.command in ("paper", "auto"):
        if hasattr(args, "live") and args.live:
            args.dry_run = False

    setup_logging("INFO")

    # Commands that don't need full TradingSystem init
    if args.command == "auto":
        cmd_auto(args)
        return
    if args.command == "journal":
        cmd_journal(args)
        return
    if args.command == "learn":
        cmd_learn(args)
        return

    system = TradingSystem()

    # Override broker port if specified
    if args.command == "paper" and args.port:
        system.broker = __import__(
            "trading_system.broker.ibkr_broker", fromlist=["IBKRBroker"]
        ).IBKRBroker(port=args.port)

    commands = {
        "scan": cmd_scan,
        "analyze": cmd_analyze,
        "train": cmd_train,
        "backtest": cmd_backtest,
        "sentiment": cmd_sentiment,
        "paper": cmd_paper,
    }

    commands[args.command](system, args)


def cmd_journal(args):
    """View trade journal and learning data."""
    from trading_system.automation.trade_journal import TradeJournal
    journal = TradeJournal()

    if args.action == "summary":
        print(journal.stats_summary())

    elif args.action == "signals":
        signals = journal.get_signal_history(days=args.days)
        if not signals:
            print("\n  No signals logged yet. Run 'python run.py auto run-once' first.\n")
            return
        print(f"\n  Recent Signals (last {args.days} days): {len(signals)}")
        print(f"  {'Date':<20s} {'Ticker':<8s} {'Dir':<6s} {'Conf':>6s} {'Exec':>5s} {'Status'}")
        print(f"  {'-'*70}")
        for s in signals[:20]:
            print(f"  {s['timestamp'][:19]:<20s} {s['ticker']:<8s} {s['direction']:<6s} "
                  f"{s['confidence']:>6.2f} {'Yes' if s['was_executed'] else 'No':>5s} "
                  f"{s.get('execution_status', '')}")
        print()

    elif args.action == "trades":
        open_trades = journal.get_open_trades()
        closed_trades = journal.get_closed_trades(days=args.days)

        if open_trades:
            print(f"\n  Open Trades ({len(open_trades)}):")
            print(f"  {'Ticker':<8s} {'Dir':<6s} {'Shares':>7s} {'Entry':>10s} {'SL':>10s} {'TP':>10s}")
            print(f"  {'-'*55}")
            for t in open_trades:
                print(f"  {t['ticker']:<8s} {t['direction']:<6s} {t['shares']:>7d} "
                      f"${t['entry_price']:>9.2f} ${t['stop_loss']:>9.2f} ${t['target_price']:>9.2f}")

        if closed_trades:
            print(f"\n  Closed Trades ({len(closed_trades)}, last {args.days} days):")
            print(f"  {'Ticker':<8s} {'Dir':<6s} {'PnL':>10s} {'PnL%':>8s} {'Days':>5s} {'Exit Reason'}")
            print(f"  {'-'*60}")
            for t in closed_trades[:20]:
                pnl = t.get('pnl', 0) or 0
                pnl_pct = t.get('pnl_pct', 0) or 0
                days = t.get('holding_days') or 0
                print(f"  {t['ticker']:<8s} {t['direction']:<6s} ${pnl:>9.2f} "
                      f"{pnl_pct:>+7.2%} {days:>5d} {t.get('exit_reason', '')}")

        if not open_trades and not closed_trades:
            print("\n  No trades logged yet.\n")
        print()

    elif args.action == "performance":
        perf = journal.get_performance_summary(days=args.days)
        if perf.get("total_trades", 0) == 0:
            print(f"\n  No closed trades in the last {args.days} days.\n")
            return
        print(f"\n{'='*50}")
        print(f"  PERFORMANCE (last {args.days} days)")
        print(f"{'='*50}")
        print(f"  Total trades:    {perf['total_trades']}")
        print(f"  Win rate:        {perf['win_rate']:.1%}")
        print(f"  Avg PnL/trade:   {perf['avg_pnl_pct']:+.2%}")
        print(f"  Total PnL:       ${perf['total_pnl']:.2f}")
        print(f"  Avg win:         {perf['avg_win_pct']:+.2%}")
        print(f"  Avg loss:        {perf['avg_loss_pct']:+.2%}")
        print(f"  Best trade:      {perf['best_trade']:+.2%}")
        print(f"  Worst trade:     {perf['worst_trade']:+.2%}")
        print(f"  Avg hold days:   {perf['avg_holding_days']:.1f}")
        print(f"{'='*50}\n")

    elif args.action == "strategy":
        accuracy = journal.get_strategy_accuracy()
        if isinstance(accuracy, dict) and "message" in accuracy:
            print(f"\n  {accuracy['message']}\n")
            return
        print(f"\n{'='*50}")
        print(f"  STRATEGY ACCURACY")
        print(f"{'='*50}")
        print(f"  {'Strategy':<20s} {'Win%':>8s} {'AvgPnL':>8s} {'Trades':>8s}")
        print(f"  {'-'*46}")
        for name, stats in sorted(accuracy.items(), key=lambda x: x[1]["win_rate"], reverse=True):
            print(f"  {name:<20s} {stats['win_rate']:>7.1%} {stats['avg_pnl']:>+7.2%} {stats['total_trades']:>8d}")
        print(f"{'='*50}\n")


def cmd_learn(args):
    """View or trigger adaptive learning."""
    from trading_system.automation.adaptive_learner import AdaptiveLearner
    learner = AdaptiveLearner()

    if args.action == "status":
        print(learner.summary())

    elif args.action == "run":
        result = learner.learn()
        if result["status"] == "insufficient_data":
            print(f"\n  {result['message']}\n")
        else:
            print(f"\n  Learning complete! Iteration #{result['iteration']}")
            print(f"  Trades analyzed: {result['trades_analyzed']}")
            lessons = result.get("lessons", {})
            if lessons.get("regime"):
                print(f"  Market regime: {lessons['regime']['old_regime']} -> {lessons['regime']['new_regime']}")
            if lessons.get("strategy_weights"):
                print(f"\n  Strategy adjustments:")
                for strat, adj in lessons["strategy_weights"].items():
                    direction = "+" if adj["adjustment"] > 0 else ""
                    print(f"    {strat}: {adj['old_weight']:.3f} -> {adj['new_weight']:.3f} "
                          f"(win rate: {adj['win_rate']:.1%})")
            print()


def cmd_auto(args):
    """Handle automation commands."""
    from trading_system.automation.scheduler import TradingScheduler, create_windows_task

    dry_run = args.dry_run

    if args.action == "install":
        create_windows_task()
        return

    scheduler = TradingScheduler(dry_run=dry_run)

    if args.action == "start":
        from trading_system.automation.scheduler import et_to_local_str
        print(f"\n  Starting Trading Daemon ({'DRY RUN' if dry_run else 'LIVE'})")
        print(f"  {'='*52}")
        print(f"  Schedule:          ET        Your Time")
        print(f"    Pre-market:      {et_to_local_str(9, 0)}")
        print(f"    Execute:         {et_to_local_str(9, 35)}")
        print(f"    Midday:          {et_to_local_str(12, 30)}")
        print(f"    EOD summary:     {et_to_local_str(16, 5)}")
        print(f"    Retrain:         Saturday 10:00 AM ET")
        print(f"  {'='*52}")
        print(f"  Press Ctrl+C to stop\n")
        scheduler.run_daemon()

    elif args.action == "run-once":
        print(f"  Running single scan+execute cycle ({'DRY RUN' if dry_run else 'LIVE'})...")
        scheduler.run_once()

    elif args.action == "premarket":
        scheduler.run_premarket()

    elif args.action == "eod":
        scheduler.run_eod_summary()

    elif args.action == "retrain":
        scheduler.run_retrain()


if __name__ == "__main__":
    main()
