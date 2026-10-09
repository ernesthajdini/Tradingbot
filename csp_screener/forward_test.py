"""
FORWARD TEST — the owner's own weekly NVDA rules, run every trading day on
live quotes, on paper. Amendment 18 in backtest/MANIFEST.md.

Why this exists. The research programme (408 configurations, 0 survivors)
cannot say the strategy works, and a 22-trade record in a rising market
cannot either. The only evidence that can settle it is evidence nobody has
seen yet: the rules, unchanged, applied to the weeks that come next and
scored against a bar fixed before the first one.

TWO ARMS, ONE ENTRY. Both arms take the same signal (weekly_csp.evaluate:
expected-move strike, Friday weekly, 1 contract against $30,000). They
differ only when the put is in the money at expiry:

    take_loss   buy it back and start fresh          (Amendment 17's control)
    roll        roll down and out for a net credit   (what the owner does);
                if no strike pays a net credit, take the loss

Mirrors Amendment 17: managed only at expiry; a new sequence opens the same
day the last one ends; entries filled at the BID, buybacks at the ASK,
$1/contract/side — the worst normal fill, not the hoped-for one.

Decisions are taken 60-120 minutes before the close, as a person would. A
put let go as out-of-the-money is checked against the official close the
next day; if it actually finished in the money the assignment loss is
booked (an `adjust` event), so the record cannot flatter itself.

PAPER ONLY. No broker is imported and no order is placed. The fwd_* topics
are read by nothing else — not the go-live gate, not the evaluator, not the
owner's real book.

    python -m csp_screener.forward_test                  # act now (in window)
    python -m csp_screener.forward_test --report-only    # scoreboard only
    python -m csp_screener.forward_test --state-dir state --email
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import statistics
import sys
from dataclasses import asdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from csp_screener import book, journal
from csp_screener import weekly_csp as w

logger = logging.getLogger(__name__)

TICKER = "NVDA"
CASH = 30_000.0
CONTRACTS = 1
START = date(2026, 10, 9)
ARMS = {"take_loss": "fwd_take_loss", "roll": "fwd_roll"}
RUN_TOPIC = "fwd_runs"
ET = ZoneInfo("America/New_York")
WINDOW = (dtime(14, 0), dtime(16, 0))          # ET; the decision window
# weekly_csp REJECT reasons that mean the feed failed, not that the rules said no
DATA_FAILURES = ("no live price", "cannot price the ATM straddle")

# ---------------------------------------------------------------------------
# Amendment 18 — THE BAR, fixed on 2026-10-09 before the first trade.
# Changing any of these once data exists invalidates the test.
# ---------------------------------------------------------------------------
BAR_MIN_SEQUENCES = 26        # closed sequences in the arm
BAR_MIN_WEEKS = 26            # calendar weeks since START
BAR_DRAWDOWN_PCT = 10.0       # window must contain a >=10% 5-day NVDA fall
BAR_MAX_SHARE = 0.40          # no single sequence > 40% of total P&L
KILL_LOSS = -3_000.0          # 10% of the paper account: FAILED at once


# ---------------------------------------------------------------------------
# Data seams — module-level so tests can replace them
# ---------------------------------------------------------------------------

def _fetch_chain(keep=()):
    return w.fetch_weekly_chain(TICKER, dte_min=0, dte_max=w.DTE_MAX, keep=keep)


def _evaluate():
    return w.evaluate(TICKER, CASH, check_book=False)


def _next_earnings() -> Optional[datetime]:
    try:
        from csp_screener.earnings import fetch_next_earnings
        return fetch_next_earnings(TICKER)
    except Exception as e:
        logger.warning(f"earnings lookup failed: {e}")
        return None


def _closes_since(start: date) -> dict:
    """{date: official close} for TICKER from `start` to today."""
    import yfinance as yf
    h = yf.Ticker(TICKER).history(start=start.isoformat(),
                                  end=(date.today() + timedelta(days=1)).isoformat(),
                                  auto_adjust=False)
    return {ix.date(): float(r["Close"]) for ix, r in h.iterrows()}


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def _put(chains, exp: date, strike: float):
    for e, sides in chains.items():
        if e.date() == exp:
            for p in sides["puts"]:
                if abs(p.strike - strike) < 1e-6:
                    return p
    return None


def roll_target(chains, spot, buyback, today: date, next_earnings=None):
    """Amendment 17A verbatim: on the nearest Friday weekly 3-10 days out
    (skipping one that spans earnings), the LOWEST strike below spot whose
    bid still exceeds the cost of closing the loser. Lowest = most
    protection; it is the strike the owner chose on 21 Aug (210P over 215P)."""
    for e in sorted(chains):
        dte = (e.date() - today).days
        if not (w.DTE_MIN <= dte <= w.DTE_MAX):
            continue
        if next_earnings and today < next_earnings.date() <= e.date():
            continue
        ok = [p for p in chains[e]["puts"]
              if p.bid > buyback and p.ask > 0 and p.strike < spot
              and p.open_interest >= w.MIN_OPEN_INTEREST]
        if ok:
            return e, min(ok, key=lambda p: p.strike)
    return None


def manage(arm, topic, today: date, spot, chains, next_earnings=None,
           closes=None) -> list:
    """Resolve the arm's open sequence if its leg has reached expiry."""
    out = []
    closes = closes or {}
    for s in list(book.open_sequences(topic).values()):
        leg = s.current
        exp = date.fromisoformat(leg.expiration)
        if exp > today:
            continue

        if exp < today:
            # The run missed expiry day. An expired contract has no quote;
            # settle on that day's official close. No roll is possible now.
            px = closes.get(exp)
            if px is None:
                out.append(f"{s.seq}: expired {exp}, closing price not yet "
                           f"available — left open, retried next run")
                continue
            if px >= leg.strike:
                book.record_close(s.seq, 0.0, True,
                                  "expired OTM (settled late on the close)",
                                  topic=topic)
                out.append(f"{s.seq}: expired OTM on {exp} (close ${px:.2f}) "
                           f"— settled late, kept ${leg.credit:.2f}")
            else:
                cost = round(leg.strike - px, 2)
                book.record_close(s.seq, cost, False,
                                  "ITM at expiry — settled late at intrinsic",
                                  topic=topic)
                out.append(f"{s.seq}: ITM on {exp} (close ${px:.2f}) — RUN "
                           f"MISSED EXPIRY, settled at intrinsic ${cost:.2f}, "
                           f"no roll possible")
            continue

        # Expiry day, decided on the live quote.
        if spot >= leg.strike:
            book.record_close(s.seq, 0.0, True, "expired OTM", topic=topic,
                              note=f"spot {spot:.2f} at decision")
            out.append(f"{s.seq}: ${leg.strike:g} put out of the money at "
                       f"expiry (NVDA ${spot:.2f}) — let it expire, kept "
                       f"${leg.credit:.2f}")
            continue

        p = _put(chains, exp, leg.strike)
        if p and p.bid > 0 and p.ask > 0:
            cost, src = p.ask, "ask"
        else:
            cost, src = round(leg.strike - spot, 2), "intrinsic, no two-sided quote"

        if arm == "roll":
            tgt = roll_target(chains, spot, cost, today, next_earnings)
            if tgt:
                e, np_ = tgt
                book.record_roll(s.seq, cost, np_.strike, e.strftime("%Y-%m-%d"),
                                 np_.bid, topic=topic,
                                 note=f"spot {spot:.2f}; buyback {src}")
                net = (np_.bid - cost) * s.contracts * 100
                out.append(f"{s.seq}: ITM (NVDA ${spot:.2f} < ${leg.strike:g}) "
                           f"— bought back ${cost:.2f}, ROLLED to "
                           f"{e:%d%b} ${np_.strike:g} at the bid "
                           f"${np_.bid:.2f} (net credit ${net:,.2f})")
                continue
            outcome = "ITM at expiry — no net-credit roll, loss taken"
        else:
            outcome = "ITM at expiry — loss taken"
        book.record_close(s.seq, cost, False, outcome, topic=topic,
                          note=f"spot {spot:.2f}; buyback {src}")
        loss = book.leg_pnl(leg.credit, cost, s.contracts)
        out.append(f"{s.seq}: ITM (NVDA ${spot:.2f} < ${leg.strike:g}) — "
                   f"{outcome.split(' — ')[1]}: bought back ${cost:.2f} "
                   f"({src}), leg ${loss:,.2f}")
    return out


def reconcile(topic, closes: dict, today: date) -> list:
    """A put let go as OTM on the live quote but which CLOSED in the money
    would have been assigned. Book that loss — once. Only finished sessions
    count: today's 'close' is an intraday price until 16:00."""
    out = []
    evs = journal.read_all(topic)
    done = {e["seq"] for e in evs if e.get("event") == "adjust"}
    for e in evs:
        if e.get("event") != "close" or e.get("outcome") != "expired OTM":
            continue
        if e["seq"] in done:
            continue
        exp = date.fromisoformat(e["expiration"])
        if exp >= today:
            continue
        px = closes.get(exp)
        if px is None or px >= float(e["strike"]):
            continue
        seq = book.replay(topic).get(e["seq"])
        n = seq.contracts if seq else CONTRACTS
        amount = -round((float(e["strike"]) - px) * n * 100, 2)
        journal.append(topic, {
            "event": "adjust", "seq": e["seq"], "amount": amount,
            "reason": (f"let go as OTM at the decision, but NVDA closed "
                       f"${px:.2f} below the ${float(e['strike']):g} strike — "
                       f"assigned; loss booked"),
            "at": datetime.now().isoformat()})
        done.add(e["seq"])
        out.append(f"{e['seq']}: ADJUSTED {amount:,.2f} — closed ${px:.2f}, "
                   f"under the strike after the decision (assignment)")
    return out


def open_if_flat(topic, d) -> Optional[str]:
    if book.has_open(TICKER, topic) or d.verdict != "EXECUTE":
        return None
    book.record_open(TICKER, d.strike, d.expiration, d.bid, CONTRACTS,
                     topic=topic,
                     note=json.dumps({"mid": d.proposed_limit, "ask": d.ask,
                                      "spot": d.current_price,
                                      "delta": d.delta, "iv": d.iv}))
    return (f"opened 1x {d.expiration} ${d.strike:g} put at the BID "
            f"${d.bid:.2f} (your limit at mid ${d.proposed_limit:.2f}; "
            f"scored at the worse fill)")


# ---------------------------------------------------------------------------
# Scoring and the bar
# ---------------------------------------------------------------------------

def scoreboard(topic) -> dict:
    seqs = book.replay(topic)
    adj = {}
    for e in journal.read_all(topic):
        if e.get("event") == "adjust":
            adj[e["seq"]] = adj.get(e["seq"], 0.0) + float(e["amount"])
    closed = [s for s in seqs.values() if s.status == "closed"]
    pnls = [round(s.realised + adj.get(s.seq, 0.0), 2) for s in closed]
    tot = round(sum(pnls), 2)
    return {
        "n": len(pnls), "total": tot,
        "mean": round(tot / len(pnls), 2) if pnls else None,
        "median": round(statistics.median(pnls), 2) if pnls else None,
        "win_rate": round(sum(p > 0 for p in pnls) / len(pnls), 3) if pnls else None,
        "worst": min(pnls) if pnls else None,
        "best": max(pnls) if pnls else None,
        "top_share": (round(max(pnls) / tot, 3) if pnls and tot > 0 else None),
        "rolled": sum(1 for s in closed if s.rolls),
        "adjusted": len(adj),
        "open": [s for s in seqs.values() if s.status == "open"],
    }


def worst_5d_drop(closes: dict) -> Optional[float]:
    """Worst close-to-close % change over 5 trading days."""
    px = [closes[k] for k in sorted(closes)]
    if len(px) < 6:
        return None
    return round(min(100 * (px[i] / px[i - 5] - 1) for i in range(5, len(px))), 2)


def bar_status(sb: dict, weeks: float, drop: Optional[float],
               control: Optional[dict] = None) -> tuple:
    """(STATUS, reasons). STATUS is NOT YET | FAILED | CLEARED."""
    if sb["total"] <= KILL_LOSS:
        return "FAILED", [f"closed P&L ${sb['total']:,.2f} hit the "
                          f"${KILL_LOSS:,.0f} kill line"]
    missing = []
    if sb["n"] < BAR_MIN_SEQUENCES:
        missing.append(f"{sb['n']}/{BAR_MIN_SEQUENCES} sequences")
    if weeks < BAR_MIN_WEEKS:
        missing.append(f"{weeks:.0f}/{BAR_MIN_WEEKS} weeks")
    if drop is None or drop > -BAR_DRAWDOWN_PCT:
        missing.append(f"no >={BAR_DRAWDOWN_PCT:.0f}% NVDA week yet "
                       f"(worst so far "
                       f"{'n/a' if drop is None else f'{drop:+.2f}%'})")
    if missing:
        return "NOT YET", missing
    fails = []
    if not (sb["mean"] and sb["mean"] > 0):
        fails.append(f"mean ${sb['mean']} is not positive")
    if not (sb["median"] and sb["median"] > 0):
        fails.append(f"median ${sb['median']} is not positive")
    if sb["top_share"] is None or sb["top_share"] > BAR_MAX_SHARE:
        fails.append(f"one sequence is {sb['top_share']} of total P&L "
                     f"(max {BAR_MAX_SHARE})")
    if control is not None and sb["total"] <= control["total"]:
        fails.append(f"rolling (${sb['total']:,.2f}) did not beat taking the "
                     f"loss (${control['total']:,.2f})")
    return ("FAILED", fails) if fails else ("CLEARED", ["every condition met"])


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def run(now: Optional[datetime] = None, force: bool = False) -> dict:
    now = (now or datetime.now(ET)).astimezone(ET)
    today = now.date()
    res = {"date": today.isoformat(), "at": now.isoformat(), "acted": False,
           "reason": "", "spot": None, "decision": None,
           "events": {a: [] for a in ARMS}}

    already = any(r.get("date") == res["date"] and r.get("acted")
                  for r in journal.read_all(RUN_TOPIC))
    in_window = now.weekday() < 5 and WINDOW[0] <= now.time() < WINDOW[1]
    if already and not force:
        res["reason"] = "already ran today — nothing to do"
        return res
    if not in_window and not force:
        res["reason"] = (f"outside the decision window "
                         f"({WINDOW[0]:%H:%M}-{WINDOW[1]:%H:%M} ET, Mon-Fri)")
        return res

    held = {date.fromisoformat(s.current.expiration)
            for t in ARMS.values() for s in book.open_sequences(t).values()}
    spot, chains = _fetch_chain(keep=held)
    if not spot:
        res["reason"] = "no live NVDA price — nothing done, retried next run"
        journal.append(RUN_TOPIC, res)
        return res
    res["spot"] = round(spot, 2)

    try:
        closes = _closes_since(START - timedelta(days=10))
    except Exception as e:
        logger.warning(f"closing prices unavailable: {e}")
        closes = {}
    earn = _next_earnings()

    for arm, topic in ARMS.items():
        res["events"][arm] += reconcile(topic, closes, today)
        res["events"][arm] += manage(arm, topic, today, spot, chains, earn, closes)

    d = _evaluate()
    res["decision"] = asdict(d)
    if d.verdict == "REJECT" and d.reason.startswith(DATA_FAILURES):
        # A broken feed, not a decision by the rules. Expiry handling above is
        # idempotent, so the backup run can safely redo this whole run.
        res["reason"] = f"quotes unusable ({d.reason}) — retried next run"
        journal.append(RUN_TOPIC, res)
        return res
    for arm, topic in ARMS.items():
        msg = open_if_flat(topic, d)
        if msg:
            res["events"][arm].append(msg)

    res["acted"] = True
    res["reason"] = "ran"
    journal.append(RUN_TOPIC, res)
    return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _money(v):
    return "—" if v is None else f"${v:,.2f}"


def _marks(chains, sb) -> list:
    L = []
    for s in sb["open"]:
        c = s.current
        p = _put(chains or {}, date.fromisoformat(c.expiration), c.strike)
        mark = (f"buy back now ${p.ask:.2f} -> open P&L "
                f"${book.leg_pnl(c.credit, p.ask, s.contracts):,.2f}"
                if p and p.ask > 0 else "no live quote")
        L.append(f"    {s.seq}: 1x ${c.strike:g} put exp {c.expiration}, sold "
                 f"${c.credit:.2f}, rolls {s.rolls}, realised so far "
                 f"${s.realised:,.2f}; {mark}")
    return L


def build_report(res: Optional[dict] = None, closes: Optional[dict] = None,
                 chains=None, today: Optional[date] = None) -> tuple:
    """(subject, text). Reads the journal; `res` is today's run, if any."""
    today = today or datetime.now(ET).date()
    closes = {k: v for k, v in (closes or {}).items() if k >= START}
    weeks = max(0.0, (today - START).days / 7)
    drop = worst_5d_drop(closes)
    sbs = {a: scoreboard(t) for a, t in ARMS.items()}
    st = {"take_loss": bar_status(sbs["take_loss"], weeks, drop),
          "roll": bar_status(sbs["roll"], weeks, drop, control=sbs["take_loss"])}
    headline = ("CLEARED" if any(s[0] == "CLEARED" for s in st.values())
                else "FAILED" if all(s[0] == "FAILED" for s in st.values())
                else "NOT YET")

    L = ["=" * 72,
         f"  FORWARD TEST — YOUR NVDA RULES ON PAPER     {today:%a %d %b %Y}",
         f"  week {weeks:.1f} of at least {BAR_MIN_WEEKS}   (started {START:%d %b %Y})",
         "=" * 72, "",
         f"  CAN I TRADE IT YET?   {headline}", ""]
    for a in ARMS:
        s, why = st[a]
        L.append(f"    {a:10} {s:8} {'; '.join(why)}")
    L += ["",
          f"    bar (fixed 9 Oct 2026, before any trade): >= {BAR_MIN_SEQUENCES} "
          f"closed sequences, >= {BAR_MIN_WEEKS} weeks,",
          f"    a >= {BAR_DRAWDOWN_PCT:.0f}% NVDA week inside the window, then "
          f"positive mean AND median at",
          f"    worst fills, no sequence > {BAR_MAX_SHARE:.0%} of the total; "
          f"rolling must also beat taking",
          f"    the loss. Either arm losing ${-KILL_LOSS:,.0f} kills the test "
          f"at once.", ""]

    L += ["-" * 72, "  WHAT YOUR RULES SAID TODAY", "-" * 72]
    if res and res.get("decision"):
        d = res["decision"]
        L.append(f"    {d['verdict']}: {d['reason']}")
        if d["verdict"] == "EXECUTE":
            L.append(f"    NVDA ${d['current_price']}, expected move "
                     f"${d['expected_move']} -> reference ${d['downside_reference']}, "
                     f"delta {d['delta']}, IV {d['iv']}")
            L.append(f"    bid ${d['bid']} / ask ${d['ask']}, OI "
                     f"{d['open_interest']}, earnings before expiry: "
                     f"{d['earnings_before_expiry']}")
    else:
        L.append(f"    (no run today: {res['reason'] if res else 'report only'})")
    L.append("")

    L += ["-" * 72, "  WHAT THE PAPER BOOK DID TODAY", "-" * 72]
    acted = False
    for a in ARMS:
        for e in (res or {}).get("events", {}).get(a, []):
            L.append(f"    {a:10} {e}")
            acted = True
    if not acted:
        L.append("    nothing")
    L.append("")

    L += ["-" * 72, "  OPEN PAPER POSITIONS", "-" * 72]
    any_open = False
    for a in ARMS:
        m = _marks(chains, sbs[a])
        if m:
            any_open = True
            L.append(f"  {a}")
            L += m
    if not any_open:
        L.append("    none")
    L.append("")

    L += ["-" * 72, "  SCOREBOARD — closed sequences, bid in / ask out, $1/contract/side",
          "-" * 72,
          f"    {'arm':10} {'n':>3} {'win%':>6} {'total':>11} {'mean':>10} "
          f"{'median':>10} {'worst':>11} {'rolled':>6}"]
    for a in ARMS:
        s = sbs[a]
        wr = "—" if s["win_rate"] is None else f"{100 * s['win_rate']:.0f}%"
        L.append(f"    {a:10} {s['n']:>3} {wr:>6} {_money(s['total']):>11} "
                 f"{_money(s['mean']):>10} {_money(s['median']):>10} "
                 f"{_money(s['worst']):>11} {s['rolled']:>6}")
    L += ["", f"    NVDA worst 5-day move since start: "
              f"{'n/a' if drop is None else f'{drop:+.2f}%'}", ""]

    L += ["-" * 72,
          "  A paper test of the rules you wrote, scored against a bar fixed",
          "  before the first trade. Not advice and not an instruction to trade.",
          f"  Background: {w.EVIDENCE}",
          "=" * 72]

    tl, ro = sbs["take_loss"]["total"], sbs["roll"]["total"]
    verdict = (res or {}).get("decision", {}) or {}
    subj = (f"Forward test {today:%d %b}: rules say "
            f"{verdict.get('verdict', '—')} | take-loss ${tl:+,.0f} | "
            f"roll ${ro:+,.0f} | can trade: {headline}")
    return subj, "\n".join(L), st


def to_html(text: str) -> str:
    return ("<pre style=\"font-family:Consolas,Menlo,monospace;font-size:13px;"
            "line-height:1.4\">" + html.escape(text) + "</pre>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _redirect(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    for t in list(ARMS.values()) + [RUN_TOPIC]:
        journal.JOURNAL_FILES[t] = state_dir / f"{t}.jsonl"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Amendment 18 forward test (paper)")
    ap.add_argument("--state-dir", type=Path,
                    help="where the fwd_* journals live (the state branch in CI)")
    ap.add_argument("--email", action="store_true", help="email the report")
    ap.add_argument("--force", action="store_true",
                    help="act outside the window / again today (testing only)")
    ap.add_argument("--report-only", action="store_true",
                    help="no trades; print the scoreboard")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if a.state_dir:
        _redirect(a.state_dir)

    res = None if a.report_only else run(force=a.force)
    if res and not res["acted"] and res["reason"].startswith(("already", "outside")):
        print(res["reason"])
        return 0

    try:
        closes = _closes_since(START - timedelta(days=10))
    except Exception:
        closes = {}
    chains = None
    try:
        held = {date.fromisoformat(s.current.expiration)
                for t in ARMS.values() for s in book.open_sequences(t).values()}
        if held:
            _, chains = _fetch_chain(keep=held)
    except Exception as e:
        logger.warning(f"marks unavailable: {e}")

    subj, text, st = build_report(res, closes, chains)
    print(subj)
    print(text)

    if a.state_dir:
        (a.state_dir / "REPORT.md").write_text(
            f"# {subj}\n\n```\n{text}\n```\n", encoding="utf-8")
        (a.state_dir / "latest.json").write_text(json.dumps({
            "subject": subj, "date": (res or {}).get("date"),
            "status": {k: {"status": v[0], "why": v[1]} for k, v in st.items()},
            "scoreboard": {k: {kk: vv for kk, vv in scoreboard(t).items()
                               if kk != "open"} for k, t in ARMS.items()},
        }, indent=2, default=str), encoding="utf-8")

    if a.email:
        from csp_screener.notify import send_email
        if not send_email(subj, to_html(text)):
            print("EMAIL NOT SENT — check RESEND_API_KEY / RESEND_TO")
            return 1
    if res and not res["acted"]:
        return 1                       # data outage: loud, and the backup run retries
    return 0


if __name__ == "__main__":
    sys.exit(main())
