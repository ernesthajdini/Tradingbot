"""
Interactive Brokers (IBKR) integration via ib_insync.

Setup:
  1. Download Trader Workstation (TWS) or IB Gateway from:
     https://www.interactivebrokers.com/en/trading/tws.php
  2. In TWS: File → Global Configuration → API → Settings:
     - Enable "ActiveX and Socket Clients"
     - Uncheck "Read-Only API"
     - Socket port: 7497 (paper) or 7496 (live)
     - Add 127.0.0.1 to trusted IPs
  3. For paper trading, log into TWS with your paper trading credentials
     (same username, password is "edemo" or your paper password)

Ports:
  TWS Paper:      7497
  TWS Live:       7496
  IB Gateway Paper: 4002
  IB Gateway Live:  4001
"""

import logging
from datetime import datetime

from ib_insync import (
    IB, Stock, MarketOrder, LimitOrder, StopOrder,
    BracketOrder, Contract, Trade, Order, util,
)

logger = logging.getLogger(__name__)


class IBKRBroker:
    """
    Interactive Brokers broker integration via ib_insync.
    Defaults to paper trading (port 7497).
    """

    PORTS = {
        "tws_paper": 7497,
        "tws_live": 7496,
        "gateway_paper": 4002,
        "gateway_live": 4001,
    }

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        client_id: int = 1,
        paper: bool = True,
    ):
        self.host = host
        self.port = port or (self.PORTS["tws_paper"] if paper else self.PORTS["tws_live"])
        self.client_id = client_id
        self.paper = paper
        self.ib = IB()
        self._connected = False

    def connect(self) -> dict:
        """Connect to TWS / IB Gateway and return account info."""
        if self._connected and self.ib.isConnected():
            return self._account_summary()

        try:
            self.ib.connect(
                self.host, self.port, clientId=self.client_id,
                timeout=10, readonly=False,
            )
            self._connected = True
            logger.info(f"Connected to IBKR on {self.host}:{self.port}")
            return self._account_summary()
        except Exception as e:
            mode = "paper" if self.paper else "LIVE"
            return {
                "status": "error",
                "message": str(e),
                "help": (
                    f"Could not connect to IBKR ({mode}) on {self.host}:{self.port}.\n"
                    f"Make sure TWS or IB Gateway is running with API enabled.\n"
                    f"TWS: File > Global Config > API > Settings > Enable Socket Clients\n"
                    f"Port should be {self.port} for {mode} trading."
                ),
            }

    def disconnect(self):
        """Disconnect from IBKR."""
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR")

    def _ensure_connected(self):
        """Ensure we have an active connection."""
        if not self._connected or not self.ib.isConnected():
            result = self.connect()
            if result.get("status") == "error":
                raise ConnectionError(result["message"])
            # Enable delayed market data (free, no subscription needed)
            self.ib.reqMarketDataType(3)  # 3 = delayed data

    def _account_summary(self) -> dict:
        """Get account summary."""
        try:
            self.ib.reqAccountSummary()
            util.sleep(1)  # give IBKR time to respond
            summary = self.ib.accountSummary()

            values = {}
            for item in summary:
                values[item.tag] = item.value

            return {
                "status": "connected",
                "mode": "paper" if self.paper else "LIVE",
                "account": values.get("AccountType", "Unknown"),
                "equity": float(values.get("NetLiquidation", 0)),
                "cash": float(values.get("TotalCashValue", 0)),
                "buying_power": float(values.get("BuyingPower", 0)),
                "unrealized_pnl": float(values.get("UnrealizedPnL", 0)),
                "realized_pnl": float(values.get("RealizedPnL", 0)),
                "cushion": values.get("Cushion", "N/A"),
            }
        except Exception as e:
            return {
                "status": "connected",
                "mode": "paper" if self.paper else "LIVE",
                "note": f"Connected but could not fetch summary: {e}",
            }

    def _make_contract(self, ticker: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
        """Create a stock contract."""
        contract = Stock(ticker, exchange, currency)
        self.ib.qualifyContracts(contract)
        return contract

    def _warm_market_data(self, contract: Stock):
        """
        Request market data for a contract before placing orders.
        This satisfies IBKR's precaution check (Error 354).
        """
        try:
            self.ib.reqMarketDataType(3)  # delayed data (free)
            self.ib.reqMktData(contract, '', False, False)
            util.sleep(2)  # give time for data to arrive
        except Exception:
            pass  # best effort — order may still work without it

    def get_positions(self) -> list[dict]:
        """Get all open positions."""
        self._ensure_connected()
        positions = self.ib.positions()

        results = []
        for pos in positions:
            ticker = pos.contract.symbol
            shares = int(pos.position)
            avg_cost = pos.avgCost

            # Get current price
            current_price = avg_cost  # fallback
            try:
                contract = self._make_contract(ticker)
                self.ib.reqMktData(contract, '', False, False)
                util.sleep(1)
                ticker_data = self.ib.ticker(contract)
                if ticker_data and ticker_data.last and ticker_data.last > 0:
                    current_price = ticker_data.last
                elif ticker_data and ticker_data.close and ticker_data.close > 0:
                    current_price = ticker_data.close
            except Exception:
                pass

            pnl = (current_price - avg_cost) * shares
            pnl_pct = (current_price / avg_cost - 1) if avg_cost > 0 else 0

            results.append({
                "ticker": ticker,
                "shares": shares,
                "side": "long" if shares > 0 else "short",
                "entry_price": round(avg_cost, 2),
                "current_price": round(current_price, 2),
                "market_value": round(current_price * abs(shares), 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 4),
            })

        return results

    def submit_order(
        self,
        ticker: str,
        shares: int,
        side: str,  # "buy" or "sell"
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> dict:
        """Submit a single order."""
        self._ensure_connected()
        contract = self._make_contract(ticker)

        action = side.upper()  # IBKR uses "BUY" / "SELL"

        if order_type == "market":
            order = MarketOrder(action, shares)
        elif order_type == "limit" and limit_price:
            order = LimitOrder(action, shares, limit_price)
        elif order_type == "stop" and stop_price:
            order = StopOrder(action, shares, stop_price)
        else:
            order = MarketOrder(action, shares)

        logger.info(f"Submitting: {action} {shares} {ticker} ({order_type})")
        trade = self.ib.placeOrder(contract, order)
        util.sleep(1)  # allow fill

        return {
            "order_id": trade.order.orderId,
            "ticker": ticker,
            "side": action,
            "shares": shares,
            "type": order_type,
            "status": trade.orderStatus.status,
            "filled": trade.orderStatus.filled,
            "avg_fill_price": trade.orderStatus.avgFillPrice,
        }

    def submit_bracket_order(
        self,
        ticker: str,
        shares: int,
        side: str,
        take_profit_price: float,
        stop_loss_price: float,
        limit_price: float | None = None,
    ) -> dict:
        """
        Submit a bracket order: entry + take profit + stop loss.
        Uses market order for entry, GTC limit/stop for exits.
        """
        self._ensure_connected()
        contract = self._make_contract(ticker)

        # Warm up market data to avoid Error 354 (blind trading precaution)
        self._warm_market_data(contract)

        action = side.upper()
        reverse_action = "SELL" if action == "BUY" else "BUY"

        # Convert numpy floats to native Python floats
        take_profit_price = float(take_profit_price)
        stop_loss_price = float(stop_loss_price)
        shares = int(shares)

        # Parent: market order for immediate entry
        parent = MarketOrder(action, shares)
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        parent.tif = "GTC"

        # Take profit: limit order
        take_profit = LimitOrder(reverse_action, shares, take_profit_price)
        take_profit.orderId = self.ib.client.getReqId()
        take_profit.parentId = parent.orderId
        take_profit.transmit = False
        take_profit.tif = "GTC"

        # Stop loss: stop order (this one transmits the whole group)
        stop_loss = StopOrder(reverse_action, shares, stop_loss_price)
        stop_loss.orderId = self.ib.client.getReqId()
        stop_loss.parentId = parent.orderId
        stop_loss.transmit = True  # transmit all three together
        stop_loss.tif = "GTC"

        logger.info(
            f"Bracket: {action} {shares} {ticker} "
            f"TP=${take_profit_price:.2f} SL=${stop_loss_price:.2f}"
        )

        trade_parent = self.ib.placeOrder(contract, parent)
        trade_tp = self.ib.placeOrder(contract, take_profit)
        trade_sl = self.ib.placeOrder(contract, stop_loss)
        util.sleep(2)  # wait for fills

        return {
            "parent_order_id": trade_parent.order.orderId,
            "ticker": ticker,
            "side": action,
            "shares": shares,
            "status": trade_parent.orderStatus.status,
            "filled": trade_parent.orderStatus.filled,
            "avg_fill_price": trade_parent.orderStatus.avgFillPrice,
            "take_profit_order_id": trade_tp.order.orderId,
            "stop_loss_order_id": trade_sl.order.orderId,
        }

    def cancel_order(self, order_id: int | None = None, trade: Trade | None = None) -> dict:
        """Cancel an open order by order_id or Trade object."""
        self._ensure_connected()

        if trade:
            self.ib.cancelOrder(trade.order)
            return {"status": "cancel_requested", "order_id": trade.order.orderId}

        # Find trade by order_id
        for t in self.ib.openTrades():
            if t.order.orderId == order_id:
                self.ib.cancelOrder(t.order)
                return {"status": "cancel_requested", "order_id": order_id}

        return {"status": "not_found", "order_id": order_id}

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        self._ensure_connected()
        self.ib.reqGlobalCancel()
        return {"status": "global_cancel_requested"}

    def close_position(self, ticker: str) -> dict:
        """Close an open position by submitting a market order in the opposite direction."""
        self._ensure_connected()

        for pos in self.ib.positions():
            if pos.contract.symbol == ticker:
                shares = int(abs(pos.position))
                side = "SELL" if pos.position > 0 else "BUY"
                contract = self._make_contract(ticker)
                order = MarketOrder(side, shares)
                trade = self.ib.placeOrder(contract, order)
                util.sleep(1)
                return {
                    "status": "close_submitted",
                    "ticker": ticker,
                    "side": side,
                    "shares": shares,
                    "order_status": trade.orderStatus.status,
                }

        return {"status": "no_position", "ticker": ticker}

    def close_all_positions(self) -> dict:
        """Close all open positions (emergency liquidation)."""
        self._ensure_connected()
        results = []
        for pos in self.ib.positions():
            ticker = pos.contract.symbol
            result = self.close_position(ticker)
            results.append(result)
        return {"status": "closing_all", "results": results}

    def get_open_orders(self) -> list[dict]:
        """Get all open/pending orders."""
        self._ensure_connected()
        trades = self.ib.openTrades()
        return [{
            "order_id": t.order.orderId,
            "ticker": t.contract.symbol,
            "side": t.order.action,
            "shares": int(t.order.totalQuantity),
            "type": t.order.orderType,
            "limit_price": t.order.lmtPrice,
            "status": t.orderStatus.status,
            "filled": t.orderStatus.filled,
        } for t in trades]

    def get_historical_data(
        self,
        ticker: str,
        duration: str = "1 Y",
        bar_size: str = "1 day",
    ) -> list[dict]:
        """Fetch historical bars from IBKR (alternative to yfinance)."""
        self._ensure_connected()
        contract = self._make_contract(ticker)
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="ADJUSTED_LAST",
            useRTH=True,
        )
        return [{"date": b.date, "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume}
                for b in bars]

    def execute_signals(
        self,
        signals: list,
        dry_run: bool = True,
        capital: float = 1000.0,
    ) -> list[dict]:
        """
        Execute a list of TradingSignal objects via IBKR.
        dry_run=True (default): logs what would happen without placing orders.
        dry_run=False: places real bracket orders.
        capital: total capital to size positions against.
        """
        results = []

        # Get existing positions to prevent duplicates
        existing_tickers = set()
        capital_deployed = 0.0
        try:
            self._ensure_connected()
            for pos in self.ib.positions():
                ticker = pos.contract.symbol
                existing_tickers.add(ticker)
                capital_deployed += abs(pos.position) * pos.avgCost
        except Exception:
            pass

        available_capital = capital - capital_deployed
        if available_capital <= 0:
            logger.info(f"No capital available (${capital:.0f} deployed of ${capital:.0f})")
            return results

        for signal in signals:
            # SKIP if we already hold this ticker
            if signal.ticker in existing_tickers:
                logger.info(f"Skipping {signal.ticker} — already have a position")
                continue

            side = "BUY" if signal.direction == "BUY" else "SELL"

            # Position sizing: risk 1% of capital per trade, scaled by confidence
            risk_per_share = abs(signal.entry_price - signal.stop_loss)
            if risk_per_share <= 0:
                continue
            risk_budget = capital * 0.01 * signal.confidence
            shares = max(1, int(risk_budget / risk_per_share))

            # Cap at 10% of capital per position
            max_shares = max(1, int((capital * 0.10) / signal.entry_price))
            shares = min(shares, max_shares)

            # Don't exceed available capital
            position_cost = shares * signal.entry_price
            if position_cost > available_capital:
                shares = max(1, int(available_capital / signal.entry_price))
                if shares * signal.entry_price > available_capital:
                    logger.info(f"Skipping {signal.ticker} — insufficient capital (${available_capital:.0f} left)")
                    continue

            action = {
                "ticker": signal.ticker,
                "side": side,
                "shares": shares,
                "entry": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "target": signal.target_price,
                "confidence": signal.confidence,
                "risk_amount": round(shares * risk_per_share, 2),
            }

            if dry_run:
                action["status"] = "DRY_RUN"
                logger.info(f"[DRY RUN] Would {side} {shares} {signal.ticker} @ ${signal.entry_price:.2f}")
            else:
                try:
                    result = self.submit_bracket_order(
                        ticker=signal.ticker,
                        shares=shares,
                        side=side,
                        take_profit_price=signal.target_price,
                        stop_loss_price=signal.stop_loss,
                    )
                    status = result.get("status", "unknown")
                    filled = result.get("filled", 0)

                    # Only count as successful if actually filled or submitted
                    if status in ("Cancelled", "Inactive") or (status == "PendingSubmit" and filled == 0):
                        action["status"] = "rejected"
                        action["error"] = f"Order {status} — not filled"
                        logger.warning(f"Order REJECTED for {signal.ticker}: {status}")
                    else:
                        action["status"] = status
                        action["order_id"] = result.get("parent_order_id", "")
                        action["filled"] = True
                except Exception as e:
                    action["status"] = "error"
                    action["error"] = str(e)

            results.append(action)

            # Track this ticker so we don't buy it again in same batch
            existing_tickers.add(signal.ticker)
            available_capital -= shares * signal.entry_price

        return results
