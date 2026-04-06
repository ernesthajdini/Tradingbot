"""
Backtesting engine.
Simulates strategy performance on historical data with realistic costs.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from trading_system.config.settings import BacktestConfig

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """Record of a single completed trade."""
    ticker: str
    direction: str
    entry_date: datetime
    entry_price: float
    exit_date: datetime
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    holding_days: int
    exit_reason: str  # signal, stop_loss, take_profit, time_limit


@dataclass
class BacktestResult:
    """Complete backtest results."""
    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    avg_trade_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_holding_days: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    equity_curve: pd.Series = field(default_factory=pd.Series)
    drawdown_curve: pd.Series = field(default_factory=pd.Series)
    trades: list[Trade] = field(default_factory=list)
    monthly_returns: pd.Series = field(default_factory=pd.Series)
    benchmark_return: float = 0.0
    alpha: float = 0.0

    def summary(self) -> str:
        return (
            f"\n{'='*60}\n"
            f"  BACKTEST RESULTS\n"
            f"{'='*60}\n"
            f"  Total Return:      {self.total_return:>10.2%}\n"
            f"  Annualized Return: {self.annualized_return:>10.2%}\n"
            f"  Sharpe Ratio:      {self.sharpe_ratio:>10.2f}\n"
            f"  Max Drawdown:      {self.max_drawdown:>10.2%}\n"
            f"  Calmar Ratio:      {self.calmar_ratio:>10.2f}\n"
            f"  Win Rate:          {self.win_rate:>10.2%}\n"
            f"  Profit Factor:     {self.profit_factor:>10.2f}\n"
            f"  Total Trades:      {self.total_trades:>10d}\n"
            f"  Avg Trade PnL:     {self.avg_trade_pnl:>10.2%}\n"
            f"  Avg Win:           {self.avg_win:>10.2%}\n"
            f"  Avg Loss:          {self.avg_loss:>10.2%}\n"
            f"  Avg Hold Days:     {self.avg_holding_days:>10.1f}\n"
            f"  Best Trade:        {self.best_trade:>10.2%}\n"
            f"  Worst Trade:       {self.worst_trade:>10.2%}\n"
            f"  Benchmark Return:  {self.benchmark_return:>10.2%}\n"
            f"  Alpha:             {self.alpha:>10.2%}\n"
            f"{'='*60}"
        )


class BacktestEngine:
    """Event-driven backtesting engine."""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(
        self,
        signals_by_date: dict[str, list],
        price_data: dict[str, pd.DataFrame],
        benchmark_data: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """
        Run backtest simulation.

        signals_by_date: date_str -> list of TradingSignal objects
        price_data: ticker -> OHLCV DataFrame
        """
        capital = self.config.initial_capital
        cash = capital
        positions: dict[str, dict] = {}  # ticker -> {shares, entry_price, entry_date, stop_loss, target}
        trades: list[Trade] = []
        equity_history = {}

        # Get all dates across all tickers
        all_dates = set()
        for df in price_data.values():
            all_dates.update(df.index)
        all_dates = sorted(all_dates)

        slippage_mult = 1 + self.config.slippage_bps / 10_000

        for date in all_dates:
            date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)

            # 1. Check stop-losses and take-profits for existing positions
            closed_tickers = []
            for ticker, pos in positions.items():
                if ticker not in price_data:
                    continue
                df = price_data[ticker]
                if date not in df.index:
                    continue

                current_price = df.loc[date, "Close"]
                low = df.loc[date, "Low"]
                high = df.loc[date, "High"]

                exit_reason = None
                exit_price = current_price

                if pos["direction"] == "BUY":
                    if low <= pos["stop_loss"]:
                        exit_reason = "stop_loss"
                        exit_price = pos["stop_loss"]
                    elif high >= pos["target"]:
                        exit_reason = "take_profit"
                        exit_price = pos["target"]
                else:  # SELL (short)
                    if high >= pos["stop_loss"]:
                        exit_reason = "stop_loss"
                        exit_price = pos["stop_loss"]
                    elif low <= pos["target"]:
                        exit_reason = "take_profit"
                        exit_price = pos["target"]

                if exit_reason:
                    exit_price *= slippage_mult if pos["direction"] == "SELL" else (1 / slippage_mult)
                    pnl = self._calc_pnl(pos, exit_price)
                    cash += pos["shares"] * exit_price + pnl if pos["direction"] == "SELL" else pos["shares"] * exit_price
                    hold_days = (date - pos["entry_date"]).days if hasattr(date, "__sub__") else 0

                    trades.append(Trade(
                        ticker=ticker,
                        direction=pos["direction"],
                        entry_date=pos["entry_date"],
                        entry_price=pos["entry_price"],
                        exit_date=date,
                        exit_price=round(exit_price, 2),
                        shares=pos["shares"],
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl / (pos["shares"] * pos["entry_price"]), 4),
                        holding_days=hold_days,
                        exit_reason=exit_reason,
                    ))
                    closed_tickers.append(ticker)

            for t in closed_tickers:
                cash += positions[t]["shares"] * positions[t]["entry_price"]  # simplified
                del positions[t]

            # 2. Process new signals for this date
            if date_str in signals_by_date:
                for signal in signals_by_date[date_str]:
                    if signal.ticker in positions:
                        continue  # already have a position
                    if len(positions) >= 10:
                        continue  # max positions limit

                    if signal.direction == "HOLD":
                        continue

                    # Position sizing: risk 1% of capital per trade
                    risk_amount = capital * self.config.commission_per_trade if self.config.commission_per_trade > 0 else capital * 0.01
                    risk_per_share = abs(signal.entry_price - signal.stop_loss)
                    if risk_per_share <= 0:
                        continue

                    shares = int(risk_amount / risk_per_share)
                    cost = shares * signal.entry_price * slippage_mult
                    if cost > cash * 0.95:  # leave 5% cash buffer
                        shares = int((cash * 0.95) / (signal.entry_price * slippage_mult))

                    if shares <= 0:
                        continue

                    actual_entry = signal.entry_price * slippage_mult
                    cash -= shares * actual_entry

                    positions[signal.ticker] = {
                        "direction": signal.direction,
                        "shares": shares,
                        "entry_price": actual_entry,
                        "entry_date": date,
                        "stop_loss": signal.stop_loss,
                        "target": signal.target_price,
                    }

            # 3. Calculate equity
            portfolio_value = cash
            for ticker, pos in positions.items():
                if ticker in price_data and date in price_data[ticker].index:
                    portfolio_value += pos["shares"] * price_data[ticker].loc[date, "Close"]
                else:
                    portfolio_value += pos["shares"] * pos["entry_price"]

            equity_history[date] = portfolio_value

        # Build result
        equity = pd.Series(equity_history).sort_index()
        result = self._compute_metrics(equity, trades, capital, benchmark_data)
        return result

    def _calc_pnl(self, position: dict, exit_price: float) -> float:
        if position["direction"] == "BUY":
            return (exit_price - position["entry_price"]) * position["shares"]
        else:
            return (position["entry_price"] - exit_price) * position["shares"]

    def _compute_metrics(
        self,
        equity: pd.Series,
        trades: list[Trade],
        initial_capital: float,
        benchmark_data: pd.DataFrame | None,
    ) -> BacktestResult:
        result = BacktestResult()
        result.trades = trades
        result.equity_curve = equity

        if equity.empty:
            return result

        # Returns
        total_return = (equity.iloc[-1] / initial_capital) - 1
        n_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 0.01)
        ann_return = (1 + total_return) ** (1 / n_years) - 1

        # Daily returns for Sharpe
        daily_returns = equity.pct_change().dropna()
        if len(daily_returns) > 0 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        # Drawdown
        peak = equity.expanding().max()
        drawdown = (equity - peak) / peak
        max_dd = drawdown.min()
        result.drawdown_curve = drawdown

        # Calmar
        calmar = ann_return / abs(max_dd) if max_dd != 0 else 0.0

        # Trade stats
        if trades:
            pnls = [t.pnl_pct for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            gross_profit = sum(w for w in wins) if wins else 0
            gross_loss = abs(sum(l for l in losses)) if losses else 0.001

            result.total_trades = len(trades)
            result.win_rate = len(wins) / len(trades) if trades else 0
            result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
            result.avg_trade_pnl = np.mean(pnls)
            result.avg_win = np.mean(wins) if wins else 0
            result.avg_loss = np.mean(losses) if losses else 0
            result.avg_holding_days = np.mean([t.holding_days for t in trades])
            result.best_trade = max(pnls)
            result.worst_trade = min(pnls)

        result.total_return = total_return
        result.annualized_return = ann_return
        result.sharpe_ratio = sharpe
        result.max_drawdown = max_dd
        result.calmar_ratio = calmar

        # Monthly returns
        if len(equity) > 20:
            monthly = equity.resample("ME").last().pct_change().dropna()
            result.monthly_returns = monthly

        # Benchmark comparison
        if benchmark_data is not None and not benchmark_data.empty:
            bench_col = "Adj Close" if "Adj Close" in benchmark_data.columns else "Close"
            bench_start = benchmark_data[bench_col].iloc[0]
            bench_end = benchmark_data[bench_col].iloc[-1]
            result.benchmark_return = (bench_end / bench_start) - 1
            result.alpha = total_return - result.benchmark_return

        return result
