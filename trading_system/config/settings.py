"""
Central configuration for the trading system.
All tunable parameters live here.
"""

from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent


@dataclass
class DataConfig:
    """Data pipeline settings."""
    cache_dir: str = str(BASE_DIR / "trading_system" / "data" / "cache")
    universe: list[str] = field(default_factory=lambda: [
        # Mega-cap tech
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        # Semiconductors
        "AMD", "AVGO", "INTC", "QCOM", "TXN", "MU", "AMAT", "LRCX", "KLAC",
        # Software / Cloud / Internet
        "CRM", "ORCL", "CSCO", "NFLX", "ADBE", "NOW", "SNOW", "PLTR", "UBER",
        "ABNB", "SHOP", "SQ",
        # Financials
        "JPM", "BAC", "V", "MA", "GS", "MS", "WFC", "C", "AXP", "BLK", "SCHW",
        # Healthcare
        "JNJ", "UNH", "ABBV", "LLY", "MRK", "TMO", "PFE", "BMY", "GILD", "CVS",
        "ISRG", "DHR", "ABT",
        # Consumer
        "HD", "PG", "COST", "PEP", "KO", "DIS", "NKE", "MCD", "SBUX", "TGT",
        "LOW", "WMT",
        # Energy / Industrials
        "XOM", "CVX", "COP", "OXY", "CAT", "BA", "UPS", "GE", "HON", "DE",
        "RTX", "LMT", "MMM",
        # Telecom / Media
        "T", "VZ", "CMCSA", "TMUS", "PYPL", "SPOT",
        # Utilities / REITs
        "AMGN", "NEE", "DUK", "SO", "AMT", "PLD",
        # ETFs (high liquidity, useful for trend signals)
        "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK",
    ])
    lookback_years: int = 5
    min_volume: int = 5_000_000  # minimum average daily dollar volume
    benchmark: str = "SPY"


@dataclass
class FeatureConfig:
    """Feature engineering settings."""
    sma_periods: list[int] = field(default_factory=lambda: [20, 50, 200])
    ema_periods: list[int] = field(default_factory=lambda: [12, 26])
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bbands_period: int = 20
    bbands_std: float = 2.0
    atr_period: int = 14
    stochastic_period: int = 14
    roc_periods: list[int] = field(default_factory=lambda: [5, 10, 20])
    volume_ma_period: int = 20


@dataclass
class SignalConfig:
    """Signal generation settings."""
    min_confidence: float = 0.3  # below this, no trade
    strong_confidence: float = 0.7  # above this, full position
    # Rule-based thresholds
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    volume_surge_threshold: float = 2.0  # 2x average volume
    # Time horizons in trading days
    short_horizon: int = 5
    medium_horizon: int = 20
    long_horizon: int = 60


@dataclass
class RiskConfig:
    """Risk management settings."""
    max_position_pct: float = 0.10  # max 10% in one stock
    max_sector_pct: float = 0.30  # max 30% in one sector
    max_portfolio_risk_per_trade: float = 0.01  # risk 1% per trade
    max_daily_drawdown: float = 0.05  # 5% daily loss limit
    max_total_drawdown: float = 0.15  # 15% total drawdown limit
    max_correlation: float = 0.70  # max correlation between positions
    default_stop_loss_atr: float = 2.0  # stop loss at 2x ATR
    default_take_profit_atr: float = 3.0  # take profit at 3x ATR
    max_open_positions: int = 8  # $10K capital allows more diversification


@dataclass
class BacktestConfig:
    """Backtesting settings."""
    initial_capital: float = 10_000.0
    commission_per_trade: float = 0.0  # most brokers are zero-commission now
    slippage_bps: float = 5.0  # 5 basis points slippage estimate
    train_window_days: int = 504  # ~2 years of trading days
    test_window_days: int = 63  # ~3 months
    step_days: int = 21  # ~1 month step
    min_trades_for_significance: int = 30


@dataclass
class MLConfig:
    """Machine learning model settings."""
    target_horizon: int = 5  # predict 5-day forward return
    classification_threshold: float = 0.01  # 1% move = significant
    n_estimators: int = 500
    max_depth: int = 6
    learning_rate: float = 0.05
    min_child_weight: int = 10
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    early_stopping_rounds: int = 50
    purge_gap_days: int = 5  # gap between train/test to prevent leakage


@dataclass
class SystemConfig:
    """Top-level system configuration."""
    data: DataConfig = field(default_factory=DataConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    output_dir: str = str(BASE_DIR / "trading_system" / "output")
    log_level: str = "INFO"


def get_config() -> SystemConfig:
    """Return default system configuration."""
    return SystemConfig()
