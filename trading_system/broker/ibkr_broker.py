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
            acct_sub = self.ib.reqAccountSummary()
            util.sleep(1)  # give IBKR time to respond
            summary = self.ib.accountSummary()

            # Cancel subscription immediately to avoid Error 322 buildup
            self.ib.cancelAccountSummary(acct_sub)

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
            self.ib.reqMktData(contract, '', True, False)  # snapshot=True (auto-cancels)
            util.sleep(2)  # give time for data to arrive
        except Exception:
            pass  # best effort — order may still work without it

    def get_positions(self) -> list[dict]:
        """Get all open positions."""
        self._ensure_connected()
        positions = self.ib.positions()

        results = []
        contracts_to_cancel = []
        for pos in positions:
            ticker = pos.contract.symbol
            shares = int(pos.position)
            avg_cost = pos.avgCost

            # Get current price
            current_price = avg_cost  # fallback
            try:
                contract = self._make_contract(ticker)
                self.ib.reqMktData(contract, '', True, False)  # snapshot=True
                contracts_to_cancel.append(contract)
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

        # Cancel all market data subscriptions to avoid leak
        for contract in contracts_to_cancel:
            try:
                self.ib.cancelMktData(contract)
            except Exception:
                pass

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
        signal_entry_price: float | None = None,
        limit_price: float | None = None,
    ) -> dict:
        """
        Submit a bracket: market entry, then TP/SL recalculated from actual fill.

        The signal's TP/SL are RELATIVE distances from signal_entry_price.
        We preserve those distances and re-anchor on the actual fill price,
        so a stale signal price can never invert the bracket.
        """
        self._ensure_connected()
        contract = self._make_contract(ticker)

        # Warm up market data
        self._warm_market_data(contract)

        action = side.upper()
        reverse_action = "SELL" if action == "BUY" else "BUY"

        # Convert numpy floats
        take_profit_price = float(take_profit_price)
        stop_loss_price = float(stop_loss_price)
        shares = int(shares)
        ref_entry = float(signal_entry_price) if signal_entry_price else None

        # Compute SL/TP DISTANCES from the signal's reference entry.
        # These distances are what really encode the strategy's risk/reward.
        if ref_entry and ref_entry > 0:
            if action == "BUY":
                sl_distance = max(0.01, ref_entry - stop_loss_price)
                tp_distance = max(0.01, take_profit_price - ref_entry)
            else:
                sl_distance = max(0.01, stop_loss_price - ref_entry)
                tp_distance = max(0.01, ref_entry - take_profit_price)
        else:
            sl_distance = tp_distance = None

        # Step 1: Submit market entry alone, wait for fill
        parent = MarketOrder(action, shares)
        parent.tif = "GTC"
        logger.info(f"Submitting market {action} {shares} {ticker} (signal entry ~${ref_entry or 0:.2f})")

        trade_parent = self.ib.placeOrder(contract, parent)

        # Wait up to 15 seconds for the parent to fill
        fill_price = 0.0
        for _ in range(15):
            util.sleep(1)
            status = trade_parent.orderStatus.status
            filled = trade_parent.orderStatus.filled
            if status == "Filled" and filled > 0:
                fill_price = float(trade_parent.orderStatus.avgFillPrice)
                break
            if status in ("Cancelled", "Inactive"):
                break

        # If we didn't fill, return early — no bracket children to submit
        if fill_price <= 0:
            logger.warning(f"{ticker} parent order did not fill: status={trade_parent.orderStatus.status}")
            return {
                "parent_order_id": trade_parent.order.orderId,
                "ticker": ticker, "side": action, "shares": shares,
                "status": trade_parent.orderStatus.status,
                "filled": trade_parent.orderStatus.filled,
                "avg_fill_price": 0.0,
                "take_profit_order_id": 0, "stop_loss_order_id": 0,
            }

        # Step 2: Recalculate TP/SL from the ACTUAL fill price
        if sl_distance is not None and tp_distance is not None:
            if action == "BUY":
                final_sl = round(fill_price - sl_distance, 2)
                final_tp = round(fill_price + tp_distance, 2)
            else:
                final_sl = round(fill_price + sl_distance, 2)
                final_tp = round(fill_price - tp_distance, 2)
        else:
            # No reference entry given — use the signal's prices as-is
            final_sl = stop_loss_price
            final_tp = take_profit_price

        logger.info(
            f"Filled {ticker} @ ${fill_price:.2f}. "
            f"Bracket children: TP=${final_tp:.2f} SL=${final_sl:.2f} "
            f"(signal said TP=${take_profit_price:.2f} SL=${stop_loss_price:.2f})"
        )

        # Step 3: Submit TP and SL as standalone GTC orders (not technical OCA,
        # but functionally equivalent — when one fills, the other becomes orphaned
        # but won't have shares to trade since position is closed)
        take_profit = LimitOrder(reverse_action, shares, final_tp)
        take_profit.tif = "GTC"
        trade_tp = self.ib.placeOrder(contract, take_profit)

        stop_loss = StopOrder(reverse_action, shares, final_sl)
        stop_loss.tif = "GTC"
        trade_sl = self.ib.placeOrder(contract, stop_loss)

        util.sleep(1)  # let orders register

        return {
            "parent_order_id": trade_parent.order.orderId,
            "ticker": ticker,
            "side": action,
            "shares": shares,
            "status": "Filled",
            "filled": trade_parent.orderStatus.filled,
            "avg_fill_price": fill_price,
            "take_profit_order_id": trade_tp.order.orderId,
            "stop_loss_order_id": trade_sl.order.orderId,
            "final_take_profit": final_tp,
            "final_stop_loss": final_sl,
        }

    def manage_open_positions(self, journal_trades: list[dict]) -> list[dict]:
        """
        Active position management:
        - Move stop to breakeven once price moves +1 ATR favorably
        - Convert fixed stop to trailing stop (1 ATR) once price moves +1.5 ATR favorably

        journal_trades: list of open trades from journal with entry_price, stop_loss, target_price.
        Returns list of actions taken.
        """
        self._ensure_connected()
        self.ib.reqMarketDataType(3)
        actions = []

        # Map ticker -> open SL orders
        open_orders = self.ib.openTrades()
        sl_by_ticker = {}  # ticker -> Trade (for STP orders)
        for t in open_orders:
            if t.order.orderType in ("STP", "TRAIL"):
                sl_by_ticker[t.contract.symbol] = t

        # IBKR positions to know what's actually held
        positions = {p.contract.symbol: p for p in self.ib.positions()}

        contracts_to_cancel = []
        for trade in journal_trades:
            ticker = trade["ticker"]
            if ticker not in positions:
                continue  # not actually held

            pos = positions[ticker]
            shares = int(abs(pos.position))
            avg_cost = float(pos.avgCost)
            direction = trade.get("direction", "BUY")

            # Get current price
            contract = self._make_contract(ticker)
            self.ib.reqMktData(contract, '', True, False)  # snapshot
            contracts_to_cancel.append(contract)
            util.sleep(1)
            tdata = self.ib.ticker(contract)
            current_px = None
            if tdata:
                if tdata.last and tdata.last > 0:
                    current_px = float(tdata.last)
                elif tdata.close and tdata.close > 0:
                    current_px = float(tdata.close)

            if not current_px:
                continue

            entry = float(trade.get("entry_price", avg_cost))
            current_sl = float(trade.get("stop_loss", 0))
            target = float(trade.get("target_price", 0))

            # Risk and reward distances (the unit of "ATR" in our setup)
            if direction == "BUY":
                risk_unit = entry - current_sl  # positive
                profit_so_far = current_px - entry  # positive if winning
            else:  # SELL/short
                risk_unit = current_sl - entry  # positive
                profit_so_far = entry - current_px  # positive if winning

            if risk_unit <= 0:
                continue

            r_multiple = profit_so_far / risk_unit  # how many R have we gained?

            # Find the existing SL order
            sl_trade = sl_by_ticker.get(ticker)
            if not sl_trade:
                continue

            existing_order_type = sl_trade.order.orderType
            existing_sl_price = float(sl_trade.order.auxPrice or sl_trade.order.lmtPrice or 0)

            # Decision logic
            new_action = None

            if r_multiple >= 1.5 and existing_order_type != "TRAIL":
                # Upgrade to trailing stop (trail by 1R)
                new_action = "trail"
            elif r_multiple >= 1.0:
                # Move stop to breakeven (entry + small buffer)
                buffer = 0.01 * entry  # 0.5% buffer past breakeven
                if direction == "BUY":
                    new_breakeven = round(entry + buffer, 2)
                    if new_breakeven > existing_sl_price:
                        new_action = "breakeven"
                else:
                    new_breakeven = round(entry - buffer, 2)
                    if new_breakeven < existing_sl_price:
                        new_action = "breakeven"

            if not new_action:
                continue

            # Cancel existing SL
            try:
                self.ib.cancelOrder(sl_trade.order)
                util.sleep(0.5)
            except Exception as e:
                logger.debug(f"Could not cancel SL for {ticker}: {e}")
                continue

            reverse = "SELL" if direction == "BUY" else "BUY"

            try:
                if new_action == "trail":
                    # Trailing stop with auxPrice = trail amount (in dollars)
                    trail_order = Order()
                    trail_order.action = reverse
                    trail_order.orderType = "TRAIL"
                    trail_order.totalQuantity = shares
                    trail_order.auxPrice = round(risk_unit, 2)  # trail by 1R
                    trail_order.tif = "GTC"
                    self.ib.placeOrder(contract, trail_order)
                    actions.append({
                        "ticker": ticker, "action": "trailing_stop",
                        "trail_amount": round(risk_unit, 2),
                        "current_price": current_px, "r_multiple": round(r_multiple, 2),
                    })
                    logger.info(f"{ticker}: upgraded to TRAILING STOP (trail=${risk_unit:.2f}, R={r_multiple:.2f})")

                elif new_action == "breakeven":
                    new_sl = StopOrder(reverse, shares, new_breakeven)
                    new_sl.tif = "GTC"
                    self.ib.placeOrder(contract, new_sl)
                    actions.append({
                        "ticker": ticker, "action": "breakeven_stop",
                        "new_stop": new_breakeven, "old_stop": existing_sl_price,
                        "current_price": current_px, "r_multiple": round(r_multiple, 2),
                    })
                    logger.info(f"{ticker}: moved stop to BREAKEVEN ${new_breakeven:.2f} (R={r_multiple:.2f})")
            except Exception as e:
                logger.warning(f"Failed to update stop for {ticker}: {e}")

        # Cleanup market data subscriptions
        for c in contracts_to_cancel:
            try:
                self.ib.cancelMktData(c)
            except Exception:
                pass

        return actions

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
                logger.warning(
                    f"Skipping {signal.ticker} — invalid risk_per_share={risk_per_share} "
                    f"(entry=${signal.entry_price}, stop=${signal.stop_loss})"
                )
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
                        signal_entry_price=signal.entry_price,
                    )
                    status = result.get("status", "unknown")
                    filled = result.get("filled", 0)

                    # Only count as successful if actually filled
                    if status == "Filled" and filled > 0:
                        action["status"] = status
                        action["order_id"] = result.get("parent_order_id", "")
                        action["filled"] = True
                        action["shares"] = int(filled)
                        action["avg_fill_price"] = result.get("avg_fill_price", signal.entry_price)
                        # Use the recalculated bracket prices (anchored to actual fill)
                        action["stop_loss"] = result.get("final_stop_loss", signal.stop_loss)
                        action["target"] = result.get("final_take_profit", signal.target_price)
                    else:
                        action["status"] = "rejected"
                        action["error"] = f"Order {status} — filled={filled}"
                        logger.warning(f"Order NOT FILLED for {signal.ticker}: status={status}, filled={filled}")
                except Exception as e:
                    action["status"] = "error"
                    action["error"] = str(e)
                    logger.error(f"Order EXCEPTION for {signal.ticker}: {type(e).__name__}: {e}", exc_info=True)

            results.append(action)

            # Track this ticker so we don't buy it again in same batch
            existing_tickers.add(signal.ticker)
            available_capital -= shares * signal.entry_price

        return results
