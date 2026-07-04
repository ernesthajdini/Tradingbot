"""
Main orchestrator for the trading signal system.
Ties together all components: data → features → signals → risk → output.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from trading_system.config.settings import SystemConfig, get_config
from trading_system.data.pipeline import DataPipeline
from trading_system.models.features import FeatureEngine
from trading_system.models.ml_model import MLSignalModel
from trading_system.signals.rule_based import RuleBasedSignals
from trading_system.signals.signal_combiner import SignalCombiner, TradingSignal
from trading_system.risk.manager import RiskManager
from trading_system.backtest.engine import BacktestEngine
from trading_system.signals.sentiment import SentimentAnalyzer
from trading_system.broker.ibkr_broker import IBKRBroker
from trading_system.utils.logger import setup_logging

logger = logging.getLogger(__name__)


class TradingSystem:
    """Main orchestrator that runs the full analysis pipeline."""

    def __init__(self, config: SystemConfig | None = None):
        self.config = config or get_config()
        self.pipeline = DataPipeline(self.config.data)
        self.features = FeatureEngine(self.config.features)
        self.rule_signals = RuleBasedSignals(self.config.signals)
        self.ml_model = MLSignalModel(self.config.ml)
        self.combiner = SignalCombiner(rule_weight=0.6, ml_weight=0.4)
        self.risk_manager = RiskManager(self.config.risk)
        self.backtest_engine = BacktestEngine(self.config.backtest)
        self.sentiment = SentimentAnalyzer()
        self.broker = IBKRBroker()

        self.output_dir = Path(self.config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Try to load pre-trained ML model
        model_path = self.output_dir / "model" / "xgb_signal_model.pkl"
        if model_path.exists():
            try:
                self.ml_model.load(model_path)
                logger.info("Loaded pre-trained ML model")
            except Exception as e:
                logger.warning(f"Could not load ML model: {e}")

    def _fetch_earnings_dates(self, tickers: list[str]) -> dict[str, list]:
        """
        Fetch upcoming earnings dates for tickers via yfinance (best-effort).
        Returns ticker -> list of pd.Timestamp dates.
        Cached for the trading day to avoid repeated calls.
        """
        import yfinance as yf
        from datetime import datetime, timedelta
        # Simple in-memory cache keyed by date
        if not hasattr(self, '_earnings_cache'):
            self._earnings_cache = {}
        today = datetime.now().date()
        if self._earnings_cache.get('date') == today:
            return self._earnings_cache.get('data', {})

        out = {}
        for t in tickers:
            try:
                ticker_obj = yf.Ticker(t)
                cal = ticker_obj.calendar
                if cal is not None:
                    # yfinance returns calendar as dict or DataFrame depending on version
                    if isinstance(cal, dict):
                        ed = cal.get("Earnings Date")
                        if ed:
                            out[t] = [ed] if not isinstance(ed, list) else ed
                    elif hasattr(cal, "T"):
                        try:
                            ed = cal.T.iloc[0].get("Earnings Date")
                            if pd.notna(ed):
                                out[t] = [ed]
                        except Exception:
                            pass
            except Exception:
                continue

        self._earnings_cache = {'date': today, 'data': out}
        return out

    def scan(self, tickers: list[str] | None = None, use_ml: bool = False) -> list[TradingSignal]:
        """
        Run a full scan: fetch data, compute features, generate signals.
        Returns list of TradingSignals sorted by confidence.
        """
        tickers = tickers or self.config.data.universe
        logger.info(f"Starting scan for {len(tickers)} tickers...")

        # 1. Fetch data
        price_data = self.pipeline.fetch_prices(tickers)
        if not price_data:
            logger.error("No price data loaded")
            return []

        # 1b. Fetch benchmark (SPY) and macro (VIX) for cross-sectional/macro features
        benchmark_df = None
        vix_df = None
        current_vix = None
        try:
            macro_data = self.pipeline.fetch_prices(["SPY", "^VIX"])
            benchmark_df = macro_data.get("SPY")
            vix_df = macro_data.get("^VIX")
            if vix_df is not None and not vix_df.empty:
                current_vix = float(vix_df["Close"].iloc[-1])
                # Inject VIX into risk manager for volatility-scaled sizing
                self.risk_manager.set_vix(current_vix)
                logger.info(f"Current VIX: {current_vix:.2f} (sizing factor: {self.risk_manager._vol_scale_factor():.2f}x)")
        except Exception as e:
            logger.debug(f"Macro data fetch failed: {e}")

        # 1c. Fetch earnings dates for blackout filter (best-effort, batched)
        try:
            earnings = self._fetch_earnings_dates(tickers[:30])  # cap to avoid slow yfinance calls
            if earnings:
                self.combiner.set_earnings_dates(earnings)
        except Exception as e:
            logger.debug(f"Earnings fetch failed: {e}")

        # 2. Compute features
        features_dict = {}
        rule_signals_dict = {}
        ml_predictions_dict = {}

        for ticker, df in price_data.items():
            try:
                feat_df = self.features.compute_all(df, benchmark_df=benchmark_df, vix_df=vix_df)
                features_dict[ticker] = feat_df

                # 3. Rule-based signals
                rule_sigs = self.rule_signals.generate(feat_df)
                rule_signals_dict[ticker] = rule_sigs

                # 4. ML signals (optional)
                if use_ml and self.ml_model.is_trained:
                    model_features = self.features.get_model_features(feat_df)
                    available_features = [f for f in self.ml_model.feature_names if f in feat_df.columns]
                    if available_features:
                        X = feat_df[available_features].iloc[-1:].fillna(0)
                        ml_pred = self.ml_model.predict(X)
                        ml_predictions_dict[ticker] = ml_pred

            except Exception as e:
                logger.warning(f"Error processing {ticker}: {e}")

        # 5. Gather sentiment for tickers with strong technical signals
        #    (only scrape for tickers that have some signal, not all 50)
        try:
            from trading_system.data.scrapers import SentimentAggregator
            sentiment_agg = SentimentAggregator()
            # Quick pre-filter: tickers with any technical signal
            candidate_tickers = [t for t in features_dict if t in rule_signals_dict
                                 and abs(float(rule_signals_dict[t].iloc[-1].get("rule_score", 0))) > 0.1]
            if candidate_tickers:
                logger.info(f"Gathering sentiment for {len(candidate_tickers)} candidates...")
                sentiment_data = sentiment_agg.get_batch_sentiment(candidate_tickers[:10])  # cap at 10 to be fast
                self.combiner.set_sentiment_data(sentiment_data)
        except Exception as e:
            logger.debug(f"Sentiment scraping skipped: {e}")

        # 6. Combine signals (now includes sentiment)
        signals = self.combiner.generate_signals_batch(
            features_dict, rule_signals_dict,
            ml_predictions_dict if ml_predictions_dict else None
        )

        # 7. Apply risk management
        portfolio_value = self.config.backtest.initial_capital
        approved = self.risk_manager.filter_signals(signals, portfolio_value)

        logger.info(f"Scan complete: {len(signals)} raw signals, {len(approved)} approved")
        return [sig for sig, _ in approved]

    def analyze_ticker(self, ticker: str) -> dict:
        """
        Deep analysis of a single ticker.
        Returns comprehensive analysis dict.
        """
        logger.info(f"Analyzing {ticker}...")

        # Fetch data
        price_data = self.pipeline.fetch_prices([ticker])
        if ticker not in price_data:
            return {"error": f"No data for {ticker}"}

        df = price_data[ticker]
        feat_df = self.features.compute_all(df)
        rule_sigs = self.rule_signals.generate(feat_df)

        latest = feat_df.iloc[-1]
        latest_sig = rule_sigs.iloc[-1]

        # Build analysis
        price = latest.get("Adj Close", latest.get("Close", 0))
        analysis = {
            "ticker": ticker,
            "date": str(feat_df.index[-1].date()),
            "price": round(float(price), 2),
            "technical": {
                "rsi": round(float(latest.get("rsi", 0)), 1),
                "macd_histogram": round(float(latest.get("macd_histogram", 0)), 4),
                "bb_position": round(float(latest.get("bb_position", 0.5)), 2),
                "sma_50_slope": round(float(latest.get("sma_50_slope", 0)), 4),
                "volume_ratio": round(float(latest.get("volume_ratio", 1)), 2),
                "atr_pct": round(float(latest.get("atr_pct", 0)), 4),
                "dist_from_52w_high": round(float(latest.get("dist_from_52w_high", 0)), 4),
            },
            "returns": {
                "1d": round(float(latest.get("return_1d", 0)), 4),
                "5d": round(float(latest.get("return_5d", 0)), 4),
                "20d": round(float(latest.get("return_20d", 0)), 4),
            },
            "signal": {
                "direction": latest_sig.get("rule_direction", "HOLD"),
                "score": round(float(latest_sig.get("rule_score", 0)), 3),
                "confidence": round(float(latest_sig.get("rule_confidence", 0)), 3),
                "reasoning": latest_sig.get("rule_reasons", ""),
            },
            "sub_signals": {
                "ma_crossover": round(float(latest_sig.get("sig_ma_crossover", 0)), 3),
                "rsi_reversal": round(float(latest_sig.get("sig_rsi_reversal", 0)), 3),
                "macd_momentum": round(float(latest_sig.get("sig_macd_momentum", 0)), 3),
                "bollinger": round(float(latest_sig.get("sig_bollinger_mean_revert", 0)), 3),
                "volume_breakout": round(float(latest_sig.get("sig_volume_breakout", 0)), 3),
                "trend_following": round(float(latest_sig.get("sig_trend_following", 0)), 3),
            },
        }

        # Add sentiment analysis
        try:
            sentiment = self.sentiment.get_sentiment(ticker)
            analysis["sentiment"] = sentiment
        except Exception as e:
            analysis["sentiment"] = {"score": 0, "label": "UNAVAILABLE", "error": str(e)}

        return analysis

    def train_ml(self, tickers: list[str] | None = None) -> dict:
        """
        Train the ML model using walk-forward validation.
        Returns training results.
        """
        tickers = tickers or self.config.data.universe
        logger.info(f"Training ML model on {len(tickers)} tickers...")

        # Fetch and prepare data
        price_data = self.pipeline.fetch_prices(tickers)

        # Fetch benchmark + macro for cross-sectional features during training
        benchmark_df = None
        vix_df = None
        try:
            macro_data = self.pipeline.fetch_prices(["SPY", "^VIX"])
            benchmark_df = macro_data.get("SPY")
            vix_df = macro_data.get("^VIX")
        except Exception as e:
            logger.debug(f"Macro data fetch failed: {e}")

        features_dict = {}

        for ticker, df in price_data.items():
            try:
                feat_df = self.features.compute_all(df, benchmark_df=benchmark_df, vix_df=vix_df)
                target = self.ml_model.prepare_target(feat_df)
                feat_df["target"] = target
                features_dict[ticker] = feat_df
            except Exception as e:
                logger.warning(f"Error preparing {ticker}: {e}")

        if not features_dict:
            return {"error": "No data prepared for training"}

        # Get feature columns
        sample_df = next(iter(features_dict.values()))
        feature_cols = self.features.get_model_features(sample_df)
        # Filter to features that exist in the data
        feature_cols = [f for f in feature_cols if f in sample_df.columns]

        logger.info(f"Using {len(feature_cols)} features: {feature_cols[:10]}...")

        # Walk-forward training
        results = self.ml_model.walk_forward_train(
            features_dict,
            feature_cols,
            train_days=self.config.backtest.train_window_days,
            test_days=self.config.backtest.test_window_days,
            step_days=self.config.backtest.step_days,
            purge_days=self.config.ml.purge_gap_days,
        )

        # Save model
        model_path = self.output_dir / "model" / "xgb_signal_model.pkl"
        self.ml_model.save(model_path)
        logger.info(f"Model saved to {model_path}")

        return {
            "n_folds": len(results),
            "results": results,
            "feature_importance": dict(list(self.ml_model.feature_importance.items())[:15]),
        }

    def backtest(self, tickers: list[str] | None = None):
        """
        Run backtest on historical data.
        Returns BacktestResult.
        """
        tickers = tickers or self.config.data.universe
        logger.info(f"Running backtest on {len(tickers)} tickers...")

        # Fetch data
        price_data = self.pipeline.fetch_prices(tickers)
        benchmark = self.pipeline.fetch_benchmark()

        # Fetch macro data for cross-sectional/macro features
        benchmark_df = None
        vix_df = None
        try:
            macro_data = self.pipeline.fetch_prices(["SPY", "^VIX"])
            benchmark_df = macro_data.get("SPY")
            vix_df = macro_data.get("^VIX")
        except Exception as e:
            logger.debug(f"Macro fetch failed: {e}")

        # Generate signals for all historical dates
        signals_by_date: dict[str, list] = {}

        for ticker, df in price_data.items():
            try:
                feat_df = self.features.compute_all(df, benchmark_df=benchmark_df, vix_df=vix_df)
                rule_sigs = self.rule_signals.generate(feat_df)

                # Warmup period: skip first 200 days
                warmup = 200
                for i in range(warmup, len(feat_df)):
                    date_str = feat_df.index[i].strftime("%Y-%m-%d")
                    signal = self.combiner.combine(
                        ticker, feat_df.iloc[:i+1], rule_sigs.iloc[:i+1]
                    )
                    if signal is not None:
                        if date_str not in signals_by_date:
                            signals_by_date[date_str] = []
                        signals_by_date[date_str].append(signal)
            except Exception as e:
                logger.warning(f"Backtest error for {ticker}: {e}")

        logger.info(f"Generated signals for {len(signals_by_date)} trading days")

        result = self.backtest_engine.run(signals_by_date, price_data, benchmark)

        # Save results
        self._save_backtest_results(result)
        return result

    def _save_backtest_results(self, result):
        """Save backtest results to output directory."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = self.output_dir / "backtests" / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)

        # Equity curve
        if not result.equity_curve.empty:
            result.equity_curve.to_csv(out_dir / "equity_curve.csv")

        # Trade log
        if result.trades:
            trades_df = pd.DataFrame([{
                "ticker": t.ticker,
                "direction": t.direction,
                "entry_date": t.entry_date,
                "entry_price": t.entry_price,
                "exit_date": t.exit_date,
                "exit_price": t.exit_price,
                "shares": t.shares,
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "holding_days": t.holding_days,
                "exit_reason": t.exit_reason,
            } for t in result.trades])
            trades_df.to_csv(out_dir / "trades.csv", index=False)

        # Summary
        summary = {
            "total_return": result.total_return,
            "annualized_return": result.annualized_return,
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "profit_factor": result.profit_factor,
        }
        with open(out_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        logger.info(f"Backtest results saved to {out_dir}")
