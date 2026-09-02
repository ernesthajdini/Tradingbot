"""
PAPER BROKER — mirror the screener's paper book into an IBKR paper account.

Architecture: the cloud decides, this executes, and fills flow back.
  1. hydrate the journal from Supabase (what the cloud screener opened/closed)
  2. every OPEN virtual trade without a broker order  -> submit the open
  3. every CLOSED virtual trade whose open filled     -> submit the close
  4. poll every working order; record fills, commissions, slippage vs the
     journal's yfinance price; re-price stale orders at the live mid
  5. print the reconciliation: real fills vs paper fills, in dollars

It never screens, never opens or closes a virtual trade, never touches
virtual_trades. It only appends to its own topic, `paper_orders`, which the
go-live gate cannot read. The paper journal stays the source of truth; this
is the broker's opinion of it.

Runs on THIS machine (TWS is local) — the cloud crons pass --no-ibkr.

    python -m csp_screener.paper_broker --once            # one cycle
    python -m csp_screener.paper_broker --loop 1800       # every 30 min
    python -m csp_screener.paper_broker --once --dry-run  # contracts + what-if only

Env (put in csp_screener/.env.local, never committed):
    SUPABASE_URL, SUPABASE_SERVICE_KEY   to hydrate the cloud journal
    IBKR_PORT=7497                        TWS paper (4002 = Gateway paper)
    PAPER_TIERS=live                      or live,sandbox
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from csp_screener import journal
from csp_screener import ibkr_paper as bk

logger = logging.getLogger("csp_screener.paper_broker")

TOPIC = "paper_orders"
REPRICE_AFTER_MIN = 20
MIN_CREDIT_PER_SHARE = 0.05
OPEN_FLOOR_FRACTION = 0.50       # never chase an open below half the journal credit
CLOSE_CEILING_MULT = 2.00        # never pay more than 2x the journal exit
CLOSE_BUFFER = 0.02              # cents past mid to get a close done
MAX_NEW_ORDERS_PER_CYCLE = 10


# ---------------------------------------------------------------------------
# env + journal helpers
# ---------------------------------------------------------------------------

def load_env_local():
    """KEY=VALUE lines from csp_screener/.env.local into os.environ. Values are
    never logged. Existing environment wins."""
    p = Path(__file__).resolve().parent / ".env.local"
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def replay_orders() -> dict:
    """{(trade_id, side): latest event} from the append-only topic."""
    state = {}
    for ev in journal.read_all(TOPIC):
        k = (ev.get("trade_id"), ev.get("side"))
        if k[0] and k[1]:
            state[k] = ev
    return state


def record(ev: dict) -> dict:
    ev.setdefault("at", datetime.now().isoformat())
    return journal.append(TOPIC, ev)


def closed_events() -> dict:
    out = {}
    for ev in journal.read_all("virtual_trades"):
        if ev.get("event") == "close" and ev.get("trade_id"):
            out[ev["trade_id"]] = ev
    return out


def working(ev: dict) -> bool:
    return ev.get("event") in ("submitted", "repriced")


def filled(ev: dict) -> bool:
    return ev.get("event") == "filled"


# ---------------------------------------------------------------------------
# one cycle
# ---------------------------------------------------------------------------

def cycle(ib, tiers: set[str], dry_run: bool = False, now: datetime | None = None,
          market_open: bool = True) -> dict:
    from csp_screener import virtual_tracker
    now = now or datetime.now()
    today = now.date()
    state = replay_orders()
    open_trades = [t for t in virtual_tracker.get_open_virtual_trades()
                   if t.tier in tiers]
    closed = closed_events()
    stats = {"opens_submitted": 0, "closes_submitted": 0, "fills": 0,
             "repriced": 0, "cancelled": 0, "dead": 0, "skipped": 0,
             "slippage_open": [], "slippage_close": [], "commission": []}
    new_orders = 0

    # ---- 1. opens for trades the broker has never seen
    for t in open_trades:
        k = (t.trade_id, "open")
        if k in state and state[k].get("event") in ("submitted", "repriced",
                                                     "filled"):
            continue
        if new_orders >= MAX_NEW_ORDERS_PER_CYCLE:
            break
        exp = t.expiration.date() if isinstance(t.expiration, datetime) else t.expiration
        if exp <= today:
            stats["skipped"] += 1
            continue
        credit = float(t.credit_received) / 100.0
        if credit < MIN_CREDIT_PER_SHARE:
            stats["skipped"] += 1
            continue
        try:
            contract, legs = bk.build_contract(ib, t.ticker, exp, t.strike,
                                               t.long_strike)
        except Exception as e:
            record({"event": "rejected", "trade_id": t.trade_id, "side": "open",
                    "ticker": t.ticker, "structure": t.structure,
                    "expiration": exp.isoformat(), "strike": t.strike,
                    "long_strike": t.long_strike, "qty": 1,
                    "limit_per_share": credit, "status_text": f"contract: {e}"})
            stats["dead"] += 1
            continue
        if dry_run:
            logger.info(f"DRY-RUN open {t.ticker} {exp} {t.strike}/"
                        f"{t.long_strike} @ credit {credit:.2f} legs={legs}")
            continue
        tk = bk.place(ib, contract, "open", credit, ref=f"csp:{t.ticker}:open")
        record({"event": "submitted", "trade_id": t.trade_id, "side": "open",
                "ticker": t.ticker, "structure": t.structure,
                "expiration": exp.isoformat(), "strike": t.strike,
                "long_strike": t.long_strike, "qty": 1,
                "limit_per_share": tk.limit_per_share,
                "journal_price_dollars": float(t.credit_received),
                "ibkr_order_id": tk.order_id, "ibkr_perm_id": tk.perm_id,
                "account": tk.account, "status_text": tk.status})
        stats["opens_submitted"] += 1
        new_orders += 1

    # ---- 2. closes for trades the journal closed after the broker filled
    for tid, cev in closed.items():
        ko, kc = (tid, "open"), (tid, "close")
        oev = state.get(ko)
        if not oev:
            continue                         # broker never held it
        if kc in state and state[kc].get("event") in ("submitted", "repriced",
                                                       "filled"):
            continue
        if working(oev):
            # journal closed before the broker ever filled: pull the order
            if not dry_run and bk.cancel(ib, int(oev.get("ibkr_order_id", 0))):
                record({**{k: oev.get(k) for k in ("trade_id", "side", "ticker",
                                                    "structure", "expiration",
                                                    "strike", "long_strike",
                                                    "qty", "ibkr_order_id",
                                                    "ibkr_perm_id", "account")},
                        "event": "cancelled",
                        "status_text": "journal closed before fill"})
                stats["cancelled"] += 1
            continue
        if not filled(oev):
            continue
        if new_orders >= MAX_NEW_ORDERS_PER_CYCLE:
            break
        exp = date.fromisoformat(str(oev["expiration"])[:10])
        journal_exit = float(cev.get("final_put_price") or 0) / 100.0
        try:
            contract, _ = bk.build_contract(ib, oev["ticker"], exp,
                                            float(oev["strike"]),
                                            oev.get("long_strike"))
        except Exception as e:
            record({**oev, "event": "rejected", "side": "close",
                    "status_text": f"contract: {e}"})
            stats["dead"] += 1
            continue
        # pay the live mid plus a small buffer, capped against the journal
        debit = journal_exit + CLOSE_BUFFER
        q = None if dry_run else bk.combo_quote(ib, contract)
        if q:
            mid = abs(0.5 * (q[0] + q[1]))
            debit = min(mid + CLOSE_BUFFER,
                        max(journal_exit, 0.01) * CLOSE_CEILING_MULT)
        debit = max(debit, 0.01)
        if dry_run:
            logger.info(f"DRY-RUN close {oev['ticker']} @ debit {debit:.2f}")
            continue
        tk = bk.place(ib, contract, "close", debit, ref=f"csp:{oev['ticker']}:close")
        record({"event": "submitted", "trade_id": tid, "side": "close",
                "ticker": oev["ticker"], "structure": oev.get("structure"),
                "expiration": exp.isoformat(), "strike": oev["strike"],
                "long_strike": oev.get("long_strike"), "qty": 1,
                "limit_per_share": tk.limit_per_share,
                "journal_price_dollars": float(cev.get("final_put_price") or 0),
                "ibkr_order_id": tk.order_id, "ibkr_perm_id": tk.perm_id,
                "account": tk.account, "status_text": tk.status})
        stats["closes_submitted"] += 1
        new_orders += 1

    if dry_run:
        return stats

    # ---- 3. poll everything working; reprice what has gone stale
    state = replay_orders()
    for (tid, side), ev in state.items():
        if not working(ev):
            continue
        tk = bk.lookup(ib, int(ev.get("ibkr_order_id", 0)),
                       int(ev.get("ibkr_perm_id", 0) or 0))
        base = {k: ev.get(k) for k in ("trade_id", "side", "ticker", "structure",
                                        "expiration", "strike", "long_strike",
                                        "qty", "ibkr_order_id", "ibkr_perm_id",
                                        "account", "journal_price_dollars",
                                        "limit_per_share")}
        if tk is None:
            continue                          # not visible this session; retry later
        if tk.status in bk.FILLED and tk.fill_per_share is not None:
            fill_d = abs(tk.fill_per_share) * 100.0
            jd = float(ev.get("journal_price_dollars") or 0)
            # opens: positive slippage = broker gave LESS credit than paper
            # closes: positive slippage = broker charged MORE than paper
            slip = (jd - fill_d) if side == "open" else (fill_d - jd)
            record({**base, "event": "filled", "fill_per_share": tk.fill_per_share,
                    "fill_dollars": round(fill_d, 2),
                    "commission": tk.commission,
                    "slippage_dollars": round(slip, 2), "status_text": tk.status})
            stats["fills"] += 1
            stats[f"slippage_{side}"].append(slip)
            if tk.commission is not None:
                stats["commission"].append(tk.commission)
        elif tk.status in bk.DEAD:
            record({**base, "event": "cancelled" if "ancel" in tk.status
                    else "rejected", "status_text": f"{tk.status} {tk.text}".strip()})
            stats["dead"] += 1
        elif market_open:
            age_min = (now - datetime.fromisoformat(ev["at"])).total_seconds() / 60
            if age_min >= REPRICE_AFTER_MIN:
                _reprice(ib, ev, base, side, stats)
    return stats


def _reprice(ib, ev, base, side, stats):
    exp = date.fromisoformat(str(ev["expiration"])[:10])
    try:
        contract, _ = bk.build_contract(ib, ev["ticker"], exp,
                                        float(ev["strike"]), ev.get("long_strike"))
    except Exception:
        return
    q = bk.combo_quote(ib, contract)
    if not q:
        return
    mid = abs(0.5 * (q[0] + q[1]))
    jd = float(ev.get("journal_price_dollars") or 0) / 100.0
    if side == "open":
        new = max(mid, jd * OPEN_FLOOR_FRACTION)
        if new < MIN_CREDIT_PER_SHARE:
            return
    else:
        new = min(mid + CLOSE_BUFFER, max(jd, 0.01) * CLOSE_CEILING_MULT)
    if abs(new - float(ev.get("limit_per_share") or 0)) < 0.01:
        return
    if not bk.cancel(ib, int(ev.get("ibkr_order_id", 0))):
        return
    tk = bk.place(ib, contract, side, new, ref=f"csp:{ev['ticker']}:{side}")
    record({**base, "event": "repriced", "limit_per_share": tk.limit_per_share,
            "ibkr_order_id": tk.order_id, "ibkr_perm_id": tk.perm_id,
            "account": tk.account, "status_text": tk.status})
    stats["repriced"] += 1


# ---------------------------------------------------------------------------
# reconciliation report
# ---------------------------------------------------------------------------

def report() -> str:
    state = replay_orders()
    rows = sorted(state.values(), key=lambda e: e.get("at", ""))
    by = {}
    for e in rows:
        by[e["event"]] = by.get(e["event"], 0) + 1
    fills = [e for e in rows if e["event"] == "filled"]
    lines = ["PAPER BROKER — reconciliation",
             f"  orders by state: {by or 'none yet'}"]
    for side in ("open", "close"):
        s = [float(e.get("slippage_dollars") or 0) for e in fills
             if e.get("side") == side]
        if s:
            lines.append(f"  {side:5} fills: n={len(s)} slippage vs paper "
                         f"mean ${sum(s)/len(s):+.2f} worst ${max(s):+.2f} "
                         f"(positive = broker worse than paper)")
    comm = [float(e["commission"]) for e in fills if e.get("commission") is not None]
    if comm:
        lines.append(f"  commissions: n={len(comm)} mean ${sum(comm)/len(comm):.2f}")
    for e in rows[-12:]:
        lines.append(f"  {e.get('at','')[:16]} {e['event']:9} {e.get('side'):5} "
                     f"{e.get('ticker'):6} {e.get('strike')}/{e.get('long_strike')} "
                     f"lmt {e.get('limit_per_share')} fill {e.get('fill_per_share')} "
                     f"{e.get('status_text','')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IBKR paper broker for the paper book")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="seconds between cycles")
    ap.add_argument("--dry-run", action="store_true",
                    help="build contracts and log intents; place nothing")
    ap.add_argument("--force", action="store_true",
                    help="run even when the US market is closed")
    ap.add_argument("--report", action="store_true", help="print and exit")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    load_env_local()
    if a.report:
        print(report())
        return 0
    tiers = {x.strip() for x in os.environ.get("PAPER_TIERS", "live").split(",")
             if x.strip()}

    from csp_screener.main import us_market_likely_open
    from csp_screener import supabase_sync

    def one():
        is_open = us_market_likely_open()
        if not is_open and not a.force:
            logger.info("US market closed — nothing to do (use --force to override)")
            return
        if supabase_sync.is_enabled():
            h = supabase_sync.hydrate_virtual_trades()
            logger.info(f"hydrated from Supabase: {h}")
        else:
            logger.warning("Supabase not configured — using the LOCAL journal only")
        ib = bk.connect_paper()
        try:
            st = cycle(ib, tiers, dry_run=a.dry_run, market_open=is_open)
            logger.info(f"cycle: {st}")
        finally:
            ib.disconnect()
        print(report())

    if a.loop > 0:
        while True:
            try:
                one()
            except bk.NotPaperAccount:
                raise
            except Exception as e:
                logger.error(f"cycle failed: {e!r}")
            time.sleep(a.loop)
    else:
        one()
    return 0


if __name__ == "__main__":
    sys.exit(main())
