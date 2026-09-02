"""
IBKR PAPER order path — real bid/ask fills for the paper track.

The screener's paper record fills at yfinance EOD quotes. This module sends
the same positions to an Interactive Brokers PAPER account so the fill, the
slippage and the commission are the broker's numbers, not ours.

SAFETY, BY CONSTRUCTION — none of this is configurable:
  * connects ONLY to the paper ports (7497 TWS paper, 4002 Gateway paper);
    the live ports (7496, 4001) are refused before any socket is opened
  * after connecting, every managed account must start with "DU" — the
    prefix IBKR gives paper accounts — or the session is torn down before
    any order call exists
  * quantity is hard-capped at MAX_QTY contracts per order
  * there is no market order anywhere in this file; every order is a limit

This is the ONLY module that imports order classes. main.py never does.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

PAPER_PORTS = {7497, 4002}
LIVE_PORTS = {7496, 4001}
PAPER_ACCOUNT_PREFIX = "DU"
MAX_QTY = 1
CONNECT_TIMEOUT = 10
DEFAULT_CLIENT_ID = 31          # distinct from main's 20 and the old daemon


class NotPaperAccount(RuntimeError):
    """Raised when anything about the session could reach real money."""


@dataclass
class OrderTicket:
    """What the broker did with one side of one paper trade."""
    order_id: int
    perm_id: int
    account: str
    status: str                  # Submitted | Filled | Cancelled | Rejected | ...
    limit_per_share: float
    fill_per_share: Optional[float] = None
    commission: Optional[float] = None
    text: str = ""


# ---------------------------------------------------------------------------
# Connection guard
# ---------------------------------------------------------------------------

def connect_paper(ib=None, host: str = "127.0.0.1", port: Optional[int] = None,
                  client_id: Optional[int] = None):
    """Connect to a PAPER session or raise. `ib` may be injected for tests."""
    port = int(port or os.environ.get("IBKR_PORT", "7497"))
    client_id = int(client_id or os.environ.get("IBKR_PAPER_CLIENT_ID",
                                                DEFAULT_CLIENT_ID))
    if port in LIVE_PORTS:
        raise NotPaperAccount(f"port {port} is a LIVE port — refusing")
    if port not in PAPER_PORTS:
        raise NotPaperAccount(f"port {port} is not a known paper port "
                              f"{sorted(PAPER_PORTS)} — refusing")
    if ib is None:
        from ib_insync import IB
        ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=CONNECT_TIMEOUT,
               readonly=False)
    accounts = list(ib.managedAccounts() or [])
    bad = [a for a in accounts if not str(a).startswith(PAPER_ACCOUNT_PREFIX)]
    if not accounts or bad:
        try:
            ib.disconnect()
        finally:
            raise NotPaperAccount(
                f"managed accounts {accounts} are not all paper "
                f"({PAPER_ACCOUNT_PREFIX}*) — refusing")
    logger.info(f"IBKR PAPER session: port {port}, accounts {accounts}")
    return ib


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def _exp_str(expiration) -> str:
    if isinstance(expiration, (date, datetime)):
        return expiration.strftime("%Y%m%d")
    return str(expiration)[:10].replace("-", "")


def put_contract(ticker: str, expiration, strike: float):
    from ib_insync import Option
    return Option(ticker, _exp_str(expiration), float(strike), "P", "SMART",
                  multiplier="100", currency="USD")


def build_contract(ib, ticker: str, expiration, strike: float,
                   long_strike: Optional[float] = None):
    """
    A qualified contract for the position:
      * long_strike None  -> the single short put (cash-secured put)
      * long_strike given -> a Bag: SELL `strike` put, BUY `long_strike` put
    Returns (contract, legs) where legs is [(right, strike, action), ...].
    """
    short = put_contract(ticker, expiration, strike)
    if long_strike is None:
        ib.qualifyContracts(short)
        if not getattr(short, "conId", 0):
            raise ValueError(f"could not qualify {ticker} {expiration} {strike}P")
        return short, [("P", float(strike), "SELL")]

    from ib_insync import Bag, ComboLeg
    long = put_contract(ticker, expiration, long_strike)
    ib.qualifyContracts(short, long)
    if not getattr(short, "conId", 0) or not getattr(long, "conId", 0):
        raise ValueError(f"could not qualify {ticker} {expiration} spread "
                         f"{strike}/{long_strike}")
    bag = Bag(symbol=ticker, exchange="SMART", currency="USD")
    bag.comboLegs = [
        ComboLeg(conId=short.conId, ratio=1, action="SELL", exchange="SMART"),
        ComboLeg(conId=long.conId, ratio=1, action="BUY", exchange="SMART"),
    ]
    return bag, [("P", float(strike), "SELL"), ("P", float(long_strike), "BUY")]


# ---------------------------------------------------------------------------
# Orders — the sign convention lives in ONE place
# ---------------------------------------------------------------------------

def combo_limit(side: str, net_per_share: float, is_bag: bool) -> tuple[str, float]:
    """
    (action, lmtPrice) for one side of the position.

    Single put:  open = SELL at +credit, close = BUY at +debit. No ambiguity.

    Bag (legs defined SELL short / BUY long): IBKR prices a combo relative
    to BUYING it — a NEGATIVE limit means the buyer RECEIVES that much.
    So opening the credit spread is BUY at -credit, and closing it (paying
    the debit) is the mirror: SELL at -debit. Both sides negative.
    The paper broker verifies this against the live combo quote's sign
    before trusting a fill, and tests pin it.
    """
    net = round(abs(float(net_per_share)), 2)
    if not is_bag:
        return ("SELL", net) if side == "open" else ("BUY", net)
    return ("BUY", -net) if side == "open" else ("SELL", -net)


def _limit_order(action: str, qty: int, price: float, ref: str):
    from ib_insync import LimitOrder
    o = LimitOrder(action, qty, price)
    o.tif = "DAY"
    o.orderRef = ref[:32]
    o.transmit = True
    return o


def place(ib, contract, side: str, net_per_share: float, ref: str,
          qty: int = 1) -> OrderTicket:
    """Submit one limit order. qty is hard-capped; nothing here is a market."""
    qty = max(1, min(int(qty), MAX_QTY))
    is_bag = getattr(contract, "secType", "") == "BAG"
    action, price = combo_limit(side, net_per_share, is_bag)
    order = _limit_order(action, qty, price, ref)
    trade = ib.placeOrder(contract, order)
    # let TWS acknowledge so orderStatus/permId populate
    try:
        ib.sleep(1.0)
    except Exception:
        pass
    st = trade.orderStatus
    return OrderTicket(order_id=int(trade.order.orderId),
                       perm_id=int(getattr(trade.order, "permId", 0) or 0),
                       account=str(getattr(trade.order, "account", "") or ""),
                       status=str(st.status), limit_per_share=price,
                       fill_per_share=(float(st.avgFillPrice)
                                       if st.filled else None),
                       text=str(getattr(st, "whyHeld", "") or ""))


def cancel(ib, order_id: int) -> bool:
    for t in ib.openTrades():
        if int(t.order.orderId) == int(order_id):
            ib.cancelOrder(t.order)
            return True
    return False


# ---------------------------------------------------------------------------
# Status lookup
# ---------------------------------------------------------------------------

FILLED = {"Filled"}
DEAD = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}


def lookup(ib, order_id: int, perm_id: int = 0) -> Optional[OrderTicket]:
    """Find an order in this session's trades, open orders, or fills."""
    for t in list(ib.trades()) + list(ib.openTrades()):
        o = t.order
        if int(o.orderId) == int(order_id) or (perm_id and
                                                int(getattr(o, "permId", 0) or 0)
                                                == int(perm_id)):
            st = t.orderStatus
            comm = None
            try:
                comm = sum(float(f.commissionReport.commission)
                           for f in t.fills if f.commissionReport)
            except Exception:
                pass
            return OrderTicket(order_id=int(o.orderId),
                               perm_id=int(getattr(o, "permId", 0) or 0),
                               account=str(getattr(o, "account", "") or ""),
                               status=str(st.status),
                               limit_per_share=float(o.lmtPrice),
                               fill_per_share=(float(st.avgFillPrice)
                                               if st.filled else None),
                               commission=comm,
                               text=str(getattr(st, "whyHeld", "") or ""))
    return None


def combo_quote(ib, contract, wait: float = 2.0) -> Optional[tuple[float, float]]:
    """(bid, ask) for the contract in IBKR's own sign convention, or None."""
    try:
        ib.reqMarketDataType(3)      # delayed is fine for a paper limit
        tk = ib.reqMktData(contract, "", False, False)
        ib.sleep(wait)
        bid, ask = tk.bid, tk.ask
        ib.cancelMktData(contract)
        if bid is None or ask is None or bid != bid or ask != ask:
            return None
        return float(bid), float(ask)
    except Exception as e:
        logger.debug(f"combo quote failed: {e}")
        return None
