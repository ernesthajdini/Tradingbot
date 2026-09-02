"""
Paper order path — the safety guards and the reconciliation state machine,
against a fake IB so nothing here needs TWS.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from csp_screener import ibkr_paper as bk
from csp_screener import journal, paper_broker as pb


# ---------------------------------------------------------------------------
# fake IB
# ---------------------------------------------------------------------------

class FakeStatus:
    def __init__(self, status="Submitted", filled=0, avg=0.0):
        self.status, self.filled, self.avgFillPrice, self.whyHeld = status, filled, avg, ""


class FakeIB:
    def __init__(self, accounts=("DU1234567",), fills=None):
        self.accounts = list(accounts)
        self.connected_to = None
        self._trades = []
        self._next = 100
        self.cancelled = []
        self.quotes = {}
        self.fills_script = fills or {}

    def connect(self, host, port, clientId, timeout, readonly):
        self.connected_to = port

    def disconnect(self):
        self.connected_to = None

    def managedAccounts(self):
        return self.accounts

    def qualifyContracts(self, *cs):
        for i, c in enumerate(cs, 1):
            c.conId = 1000 + i + int(getattr(c, "strike", 0) or 0)
        return cs

    def placeOrder(self, contract, order):
        order.orderId = self._next
        order.permId = 900000 + self._next
        order.account = self.accounts[0]
        self._next += 1
        st = FakeStatus()
        t = SimpleNamespace(contract=contract, order=order, orderStatus=st, fills=[])
        self._trades.append(t)
        return t

    def sleep(self, s):
        pass

    def openTrades(self):
        return [t for t in self._trades if t.orderStatus.status in ("Submitted",
                                                                    "PreSubmitted")]

    def trades(self):
        return list(self._trades)

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        for t in self._trades:
            if t.order.orderId == order.orderId:
                t.orderStatus.status = "Cancelled"

    def fill(self, order_id, price, commission=1.0):
        for t in self._trades:
            if t.order.orderId == order_id:
                t.orderStatus = FakeStatus("Filled", 1, price)
                rep = SimpleNamespace(commission=commission)
                t.fills = [SimpleNamespace(commissionReport=rep)]

    def reqMarketDataType(self, n):
        pass

    def reqMktData(self, contract, a, b, c):
        return SimpleNamespace(**self.quotes.get(getattr(contract, "symbol", ""),
                                                 {"bid": float("nan"),
                                                  "ask": float("nan")}))

    def cancelMktData(self, contract):
        pass


@pytest.fixture(autouse=True)
def tmp_journal(tmp_path, monkeypatch):
    files = {t: tmp_path / f"{t}.jsonl" for t in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", files)
    monkeypatch.setattr(journal, "_supabase_dual_write", lambda *a, **k: None,
                        raising=False)
    yield


def _open_event(tid="s1::ACME::20.0::2030-01-17", ticker="ACME", strike=20.0,
                long_strike=15.0, credit=74.0, tier="live"):
    return {"event": "open", "trade_id": tid, "screen_id": "s1", "ticker": ticker,
            "opened_at": datetime.now().isoformat(), "spot_at_open": 25.0,
            "expiration": "2030-01-17", "dte_at_open": 35, "strike": strike,
            "credit_received": credit, "max_loss": 426.0, "breakeven": 19.26,
            "iv_at_open": 0.5, "structure": "put_credit_spread",
            "long_strike": long_strike, "tier": tier, "data_quality": "ok",
            "delta_at_open": -0.28}


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def test_refuses_live_ports():
    for port in (7496, 4001):
        with pytest.raises(bk.NotPaperAccount):
            bk.connect_paper(FakeIB(), port=port)


def test_refuses_unknown_port():
    with pytest.raises(bk.NotPaperAccount):
        bk.connect_paper(FakeIB(), port=8888)


def test_refuses_non_paper_account():
    ib = FakeIB(accounts=("U1234567",))
    with pytest.raises(bk.NotPaperAccount):
        bk.connect_paper(ib, port=7497)
    assert ib.connected_to is None          # torn down


def test_refuses_mixed_accounts():
    with pytest.raises(bk.NotPaperAccount):
        bk.connect_paper(FakeIB(accounts=("DU1", "U2")), port=7497)


def test_accepts_paper():
    ib = bk.connect_paper(FakeIB(), port=7497)
    assert ib.connected_to == 7497


def test_qty_hard_cap():
    ib = FakeIB()
    c, _ = bk.build_contract(ib, "ACME", "2030-01-17", 20.0, None)
    tk = bk.place(ib, c, "open", 0.74, "x", qty=50)
    assert ib.trades()[0].order.totalQuantity == bk.MAX_QTY


def test_no_market_orders_exist():
    import inspect
    src = inspect.getsource(bk) + inspect.getsource(pb)
    assert "MarketOrder" not in src


# ---------------------------------------------------------------------------
# contracts + sign convention
# ---------------------------------------------------------------------------

def test_single_put_orders_are_plain():
    assert bk.combo_limit("open", 0.74, is_bag=False) == ("SELL", 0.74)
    assert bk.combo_limit("close", 0.30, is_bag=False) == ("BUY", 0.30)


def test_bag_orders_are_negative_both_sides():
    assert bk.combo_limit("open", 0.74, is_bag=True) == ("BUY", -0.74)
    assert bk.combo_limit("close", 0.30, is_bag=True) == ("SELL", -0.30)
    # a caller passing a negative credit must not flip the side
    assert bk.combo_limit("open", -0.74, is_bag=True) == ("BUY", -0.74)


def test_spread_bag_legs():
    ib = FakeIB()
    bag, legs = bk.build_contract(ib, "ACME", "2030-01-17", 20.0, 15.0)
    assert bag.secType == "BAG"
    acts = [(l.action, l.ratio) for l in bag.comboLegs]
    assert acts == [("SELL", 1), ("BUY", 1)]
    assert legs == [("P", 20.0, "SELL"), ("P", 15.0, "BUY")]


def test_single_put_contract():
    ib = FakeIB()
    c, legs = bk.build_contract(ib, "ACME", "2030-01-17", 20.0, None)
    assert c.secType == "OPT" and c.right == "P" and c.lastTradeDateOrContractMonth == "20300117"
    assert legs == [("P", 20.0, "SELL")]


# ---------------------------------------------------------------------------
# reconciliation state machine
# ---------------------------------------------------------------------------

def test_open_submitted_once_and_idempotent():
    journal.append("virtual_trades", _open_event())
    ib = FakeIB()
    st = pb.cycle(ib, {"live"})
    assert st["opens_submitted"] == 1
    st = pb.cycle(ib, {"live"})
    assert st["opens_submitted"] == 0           # already working
    assert len(ib.trades()) == 1
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    assert ev["event"] == "submitted"
    assert ev["limit_per_share"] == -0.74       # bag credit convention
    assert ev["journal_price_dollars"] == 74.0


def test_tier_filter():
    journal.append("virtual_trades", _open_event(tier="sandbox"))
    ib = FakeIB()
    assert pb.cycle(ib, {"live"})["opens_submitted"] == 0
    assert pb.cycle(ib, {"live", "sandbox"})["opens_submitted"] == 1


def test_fill_records_slippage_and_commission():
    journal.append("virtual_trades", _open_event(credit=74.0))
    ib = FakeIB()
    pb.cycle(ib, {"live"})
    oid = ib.trades()[0].order.orderId
    ib.fill(oid, -0.70, commission=1.3)         # broker gave 70, paper said 74
    st = pb.cycle(ib, {"live"})
    assert st["fills"] == 1
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    assert ev["event"] == "filled"
    assert ev["fill_dollars"] == 70.0
    assert ev["slippage_dollars"] == 4.0        # positive = broker worse
    assert ev["commission"] == 1.3


def test_close_submitted_after_journal_close_and_fill():
    tid = "s1::ACME::20.0::2030-01-17"
    journal.append("virtual_trades", _open_event(tid=tid))
    ib = FakeIB()
    pb.cycle(ib, {"live"})
    ib.fill(ib.trades()[0].order.orderId, -0.74)
    pb.cycle(ib, {"live"})                       # records the fill
    journal.append("virtual_trades", {"event": "close", "trade_id": tid,
                                      "ticker": "ACME", "strike": 20.0,
                                      "expiration": "2030-01-17",
                                      "final_put_price": 30.0,
                                      "credit_received": 74.0,
                                      "closed_at": datetime.now().isoformat(),
                                      "structure": "put_credit_spread",
                                      "long_strike": 15.0, "tier": "live"})
    st = pb.cycle(ib, {"live"})
    assert st["closes_submitted"] == 1
    ev = pb.replay_orders()[(tid, "close")]
    assert ev["side"] == "close" and ev["event"] == "submitted"
    assert ev["limit_per_share"] == -round(0.30 + pb.CLOSE_BUFFER, 2)


def test_journal_close_before_fill_cancels_open():
    tid = "s1::ACME::20.0::2030-01-17"
    journal.append("virtual_trades", _open_event(tid=tid))
    ib = FakeIB()
    pb.cycle(ib, {"live"})
    journal.append("virtual_trades", {"event": "close", "trade_id": tid,
                                      "ticker": "ACME", "final_put_price": 30.0,
                                      "credit_received": 74.0})
    st = pb.cycle(ib, {"live"})
    assert st["cancelled"] == 1 and st["closes_submitted"] == 0
    assert ib.cancelled == [ib.trades()[0].order.orderId]


def test_stale_open_repriced_at_mid_with_floor():
    journal.append("virtual_trades", _open_event(credit=74.0))
    ib = FakeIB()
    ib.quotes["ACME"] = {"bid": -0.60, "ask": -0.50}     # combo now worth ~0.55
    pb.cycle(ib, {"live"})
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    # age the order past the reprice threshold
    later = datetime.fromisoformat(ev["at"]) + timedelta(minutes=pb.REPRICE_AFTER_MIN + 1)
    st = pb.cycle(ib, {"live"}, now=later)
    assert st["repriced"] == 1
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    assert ev["event"] == "repriced" and ev["limit_per_share"] == -0.55


def test_reprice_never_below_half_journal_credit():
    journal.append("virtual_trades", _open_event(credit=74.0))
    ib = FakeIB()
    ib.quotes["ACME"] = {"bid": -0.20, "ask": -0.10}     # market collapsed
    pb.cycle(ib, {"live"})
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    later = datetime.fromisoformat(ev["at"]) + timedelta(minutes=pb.REPRICE_AFTER_MIN + 1)
    pb.cycle(ib, {"live"}, now=later)
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    assert ev["limit_per_share"] == -0.37                # 0.5 x 0.74, not 0.15


def test_no_reprice_when_market_closed():
    journal.append("virtual_trades", _open_event())
    ib = FakeIB()
    ib.quotes["ACME"] = {"bid": -0.60, "ask": -0.50}
    pb.cycle(ib, {"live"})
    ev = pb.replay_orders()[("s1::ACME::20.0::2030-01-17", "open")]
    later = datetime.fromisoformat(ev["at"]) + timedelta(hours=5)
    st = pb.cycle(ib, {"live"}, now=later, market_open=False)
    assert st["repriced"] == 0


def test_dry_run_places_nothing():
    journal.append("virtual_trades", _open_event())
    ib = FakeIB()
    pb.cycle(ib, {"live"}, dry_run=True)
    assert ib.trades() == [] and pb.replay_orders() == {}


def test_expired_or_dust_skipped():
    journal.append("virtual_trades", _open_event(tid="a", credit=2.0))   # $0.02/sh
    e = _open_event(tid="b"); e["expiration"] = "2020-01-17"
    journal.append("virtual_trades", e)
    ib = FakeIB()
    st = pb.cycle(ib, {"live"})
    assert st["opens_submitted"] == 0 and st["skipped"] == 2


def test_paper_orders_topic_is_not_virtual_trades():
    journal.append("virtual_trades", _open_event())
    pb.cycle(FakeIB(), {"live"})
    assert all(e.get("event") == "open" for e in journal.read_all("virtual_trades"))
