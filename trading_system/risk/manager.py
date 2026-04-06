"""
Risk management module.
Handles position sizing, portfolio limits, and risk controls.
"""

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading_system.config.settings import RiskConfig
from trading_system.signals.signal_combiner import TradingSignal

logger = logging.getLogger(__name__)


# Sector mapping for universe stocks
SECTOR_MAP = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "NVDA": "Technology", "META": "Technology", "AMD": "Technology",
    "AVGO": "Technology", "INTC": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "CRM": "Technology", "ORCL": "Technology",
    "CSCO": "Technology", "ADBE": "Technology", "NOW": "Technology",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "NKE": "Consumer Discretionary",
    "MCD": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    # Communication Services
    "NFLX": "Communication Services", "DIS": "Communication Services",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "V": "Financials",
    "MA": "Financials", "GS": "Financials", "MS": "Financials",
    "PYPL": "Financials", "SQ": "Financials",
    # Healthcare
    "JNJ": "Healthcare", "UNH": "Healthcare", "ABBV": "Healthcare",
    "LLY": "Healthcare", "MRK": "Healthcare", "TMO": "Healthcare",
    "PFE": "Healthcare", "AMGN": "Healthcare",
    # Consumer Staples
    "COST": "Consumer Staples", "PG": "Consumer Staples",
    "PEP": "Consumer Staples", "KO": "Consumer Staples",
    "WMT": "Consumer Staples",
    # Energy
    "XOM": "Energy", "CVX": "Energy",
    # Industrials
    "CAT": "Industrials", "BA": "Industrials", "UPS": "Industrials",
    "GE": "Industrials",
    # Utilities
    "NEE": "Utilities",
}


@dataclass
class PositionSize:
    """Calculated position sizing result."""
    ticker: str
    shares: int
    dollar_amount: float
    risk_amount: float
    position_pct: float  # % of portfolio
    approved: bool
    rejection_reason: str = ""


class RiskManager:
    """Portfolio risk management and position sizing."""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self.current_positions: dict[str, dict] = {}  # ticker -> {shares, entry_price, sector}
        self.portfolio_value: float = 0.0
        self.daily_pnl: float = 0.0

    def size_position(
        self,
        signal: TradingSignal,
        portfolio_value: float,
        current_positions: dict[str, dict] | None = None,
    ) -> PositionSize:
        """
        Calculate position size for a signal, applying all risk limits.
        """
        if current_positions is not None:
            self.current_positions = current_positions
        self.portfolio_value = portfolio_value

        ticker = signal.ticker
        entry = signal.entry_price
        stop = signal.stop_loss

        # 1. Base size from risk budget
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return PositionSize(ticker, 0, 0, 0, 0, False, "Invalid stop loss")

        risk_budget = portfolio_value * self.config.max_portfolio_risk_per_trade
        # Scale risk by confidence
        adjusted_risk = risk_budget * signal.confidence
        base_shares = int(adjusted_risk / risk_per_share)

        if base_shares <= 0:
            return PositionSize(ticker, 0, 0, 0, 0, False, "Position too small")

        dollar_amount = base_shares * entry
        position_pct = dollar_amount / portfolio_value

        # 2. Max position size limit
        max_dollars = portfolio_value * self.config.max_position_pct
        if dollar_amount > max_dollars:
            base_shares = int(max_dollars / entry)
            dollar_amount = base_shares * entry
            position_pct = dollar_amount / portfolio_value

        # 3. Check max open positions
        if len(self.current_positions) >= self.config.max_open_positions:
            return PositionSize(ticker, 0, 0, 0, 0, False,
                              f"Max {self.config.max_open_positions} positions reached")

        # 4. Sector concentration check
        sector = SECTOR_MAP.get(ticker, "Unknown")
        sector_exposure = sum(
            pos.get("shares", 0) * pos.get("entry_price", 0)
            for t, pos in self.current_positions.items()
            if SECTOR_MAP.get(t, "Unknown") == sector
        )
        if (sector_exposure + dollar_amount) / portfolio_value > self.config.max_sector_pct:
            # Reduce to fit sector limit
            remaining = (self.config.max_sector_pct * portfolio_value) - sector_exposure
            if remaining <= 0:
                return PositionSize(ticker, 0, 0, 0, 0, False,
                                  f"Sector {sector} at {self.config.max_sector_pct:.0%} limit")
            base_shares = int(remaining / entry)
            dollar_amount = base_shares * entry
            position_pct = dollar_amount / portfolio_value

        # 5. Correlation check (simplified — skip duplicate tickers)
        if ticker in self.current_positions:
            return PositionSize(ticker, 0, 0, 0, 0, False, "Already have position")

        # 6. Drawdown circuit breaker
        if self.daily_pnl / max(portfolio_value, 1) < -self.config.max_daily_drawdown:
            return PositionSize(ticker, 0, 0, 0, 0, False, "Daily drawdown limit hit")

        risk_amount = base_shares * risk_per_share
        return PositionSize(
            ticker=ticker,
            shares=base_shares,
            dollar_amount=round(dollar_amount, 2),
            risk_amount=round(risk_amount, 2),
            position_pct=round(position_pct, 4),
            approved=True,
        )

    def filter_signals(
        self,
        signals: list[TradingSignal],
        portfolio_value: float,
        current_positions: dict[str, dict] | None = None,
    ) -> list[tuple[TradingSignal, PositionSize]]:
        """
        Filter and size a batch of signals through risk management.
        Returns list of (signal, position_size) tuples that pass all checks.
        """
        approved = []

        # Process signals in confidence order (highest first)
        sorted_signals = sorted(signals, key=lambda s: s.confidence, reverse=True)

        simulated_positions = dict(current_positions) if current_positions else {}

        for signal in sorted_signals:
            sizing = self.size_position(signal, portfolio_value, simulated_positions)

            if sizing.approved and sizing.shares > 0:
                approved.append((signal, sizing))

                # Update simulated state for next signal evaluation
                simulated_positions[signal.ticker] = {
                    "shares": sizing.shares,
                    "entry_price": signal.entry_price,
                    "sector": SECTOR_MAP.get(signal.ticker, "Unknown"),
                }
                portfolio_value -= sizing.dollar_amount  # reduce available capital
            else:
                logger.debug(f"Rejected {signal.ticker}: {sizing.rejection_reason}")

        return approved

    def portfolio_summary(self, positions: dict[str, dict], portfolio_value: float) -> dict:
        """Generate current portfolio risk summary."""
        if not positions:
            return {"status": "no_positions", "cash_pct": 1.0}

        total_invested = sum(
            pos["shares"] * pos["entry_price"] for pos in positions.values()
        )

        sector_exposure = {}
        for ticker, pos in positions.items():
            sector = SECTOR_MAP.get(ticker, "Unknown")
            sector_exposure[sector] = sector_exposure.get(sector, 0) + pos["shares"] * pos["entry_price"]

        return {
            "num_positions": len(positions),
            "total_invested": round(total_invested, 2),
            "cash_pct": round(1 - total_invested / portfolio_value, 4),
            "largest_position_pct": round(
                max(pos["shares"] * pos["entry_price"] for pos in positions.values()) / portfolio_value, 4
            ),
            "sector_exposure": {
                s: round(v / portfolio_value, 4) for s, v in sector_exposure.items()
            },
        }
