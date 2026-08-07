"""
For each ranked candidate, generate a concrete VIRTUAL setup:
  - which put strike (closest to target delta or % OTM)
  - which expiration (within DTE window)
  - estimated premium / max loss / breakeven
  - data quality flags

This is what gets tracked as a "virtual trade" by virtual_tracker, so the
screener accumulates real performance data even when no live order is placed.

We are deliberately permissive about MISSING data — if greeks or IV are
unavailable, we still generate a setup based on % OTM, but flag the data
quality so the email shows the caveat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Optional

from csp_screener import config
from csp_screener.options_data import OptionsChain, OptionContract

logger = logging.getLogger(__name__)

# Hard cap on |delta| for a CSP candidate — the playbook trades 25-30 delta;
# anything above 0.40 is a rule-breaking near-ATM/ITM selection, not a
# candidate. (Structural bound, deliberately outside config.py's cooldown.)
MAX_CSP_DELTA = 0.40


def earnings_inside_hold(dte: int, next_earnings_days: Optional[int]) -> bool:
    """
    Would this expiration's position still be open when earnings report?

    The ticker-level blackout (EARNINGS_EXCLUSION_DAYS) runs BEFORE any
    expiration is chosen, so it cannot know the hold. Max hold is
    dte - VIRTUAL_FORCE_EXIT_DTE (the 21-DTE forced close). With the real
    expiry calendar the selector routinely picks 37 DTE → a 16-day hold,
    longer than the 15-day effective blackout — so a company reporting in
    16 days passed the filter and reported the day BEFORE the scheduled
    exit. `<=` because before-open vs after-close is a coin flip.
    """
    if next_earnings_days is None or next_earnings_days < 0:
        return False  # unknown earnings date — the ticker filter owns that call
    hold_days = max(0, dte - config.VIRTUAL_FORCE_EXIT_DTE)
    return next_earnings_days <= hold_days


def net_at_tp_exit(credit_per_contract: float, structure: str = "csp") -> float:
    """
    What a position actually nets at its own 50% take-profit, after friction.

    Every setup here attaches a GTC buyback at 50% of credit and a 21-DTE
    forced close, so "credit minus friction" (the expire-worthless number)
    overstates the designed outcome by ~2.3x. One helper so the sandbox
    gate, the live ticket and the email can never disagree again.
    """
    exit_price = (1.0 - config.VIRTUAL_TP_PCT) * credit_per_contract
    legs_round_trip = 4 if structure == "put_credit_spread" else 2
    friction = (legs_round_trip * config.COMMISSION_PER_CONTRACT
                + config.SLIPPAGE_PCT_OF_PREMIUM * (credit_per_contract + exit_price))
    return config.VIRTUAL_TP_PCT * credit_per_contract - friction


@dataclass
class VirtualSetup:
    ticker: str
    spot_at_screen: float
    expiration: str          # ISO date
    dte: int
    strike: float            # short put strike
    pct_otm: float           # how far OTM as fraction of spot
    delta: Optional[float]
    iv: Optional[float]
    bid: float
    ask: float
    mid: float
    bid_ask_pct: float
    open_interest: int
    volume: int
    estimated_credit_per_contract: float  # net premium received per contract
    max_loss_per_contract: float          # CSP: strike*100-credit; spread: width*100-credit
    breakeven: float
    data_quality: str
    reasoning: list[str]
    # Two-tier extensions (playbook). Defaults keep old CSP records valid.
    structure: str = "csp"                # "csp" | "put_credit_spread"
    long_strike: Optional[float] = None   # spread only
    tier: str = "sandbox"                 # "live" | "sandbox"
    net_credit_after_friction: Optional[float] = None
    friction_estimate: Optional[float] = None
    ticket: Optional[str] = None          # staged order text (live tier)
    # SIZING annotation — never a filter. The signal stands on its own; this
    # only says how much of it today's account can carry.
    affordable_contracts: Optional[int] = None
    fits_account: Optional[bool] = None
    pct_of_equity: Optional[float] = None
    sizing_note: Optional[str] = None
    net_at_designed_exit: Optional[float] = None  # what the 50% TP delivers

    def to_dict(self) -> dict:
        return asdict(self)


def _pick_best_put(
    chain: OptionsChain,
    expiration: datetime,
    spot: float,
    target_delta: float = config.TARGET_DELTA,
) -> tuple[Optional[OptionContract], list[str]]:
    """
    Pick the put closest to target_delta if delta is available, else closest
    to ~5% OTM as a proxy. Returns (contract, quality_warnings).

    Liquidity gates degrade gracefully instead of rejecting everything:

    - Live quotes (bid>0 and ask>0): enforce the spread gate strictly.
    - Stale quotes (bid=ask=0 but lastPrice>0): accept with a warning.
      This is the NORMAL state on weekends — the Sunday screen would
      otherwise never open a single virtual position.
    - OI: if ANY contract in this expiration reports OI, enforce MIN_OI.
      If the whole chain reports zero OI (IBKR snapshot without the OI
      generic tick, or a yfinance data gap), treat OI as UNKNOWN and
      accept with a warning rather than rejecting the entire chain.

    The warnings flow into the setup's data_quality/reasoning so the email
    and dashboard show exactly how much to trust the numbers.
    """
    candidates = chain.puts_for_expiration(expiration)
    if not candidates:
        return None, []

    quoted = [c for c in candidates if c.mid > 0]
    if not quoted:
        return None, []

    oi_known = any(c.open_interest > 0 for c in quoted)

    def passes(c: OptionContract) -> bool:
        # Rule enforcement (BB incident: sold a $12 put on an $11.10 stock at
        # delta -0.53 against a 0.30 target because nothing forbade it).
        # A cash-secured put candidate must be OUT of the money, and the
        # playbook's spec is "short leg 25-30 delta" — cap hard at 0.40.
        # Constants live here, not config.py (structural rule bounds, like
        # sanity.py — not tunable strategy thresholds).
        if c.strike >= spot:
            return False  # never sell ITM/ATM
        if c.delta is not None and abs(c.delta) > MAX_CSP_DELTA:
            return False
        has_live_quote = c.bid > 0 and c.ask > 0
        if has_live_quote:
            if c.bid_ask_spread_pct > config.MAX_BID_ASK_PCT_OF_MID:
                return False
        elif c.last <= 0:
            return False  # no live quote AND no last price — nothing to anchor on
        if oi_known and c.open_interest < config.MIN_OPEN_INTEREST:
            return False
        return True

    liquid = [c for c in quoted if passes(c)]
    if not liquid:
        return None, []

    with_delta = [c for c in liquid if c.delta is not None]
    if with_delta:
        chosen = min(with_delta, key=lambda c: abs(abs(c.delta) - target_delta))
    else:
        target_strike = spot * 0.95
        chosen = min(liquid, key=lambda c: abs(c.strike - target_strike))

    warnings: list[str] = []
    if not (chosen.bid > 0 and chosen.ask > 0):
        warnings.append(
            "STALE QUOTE: no live bid/ask (market closed?) — premium anchored "
            "to last trade. Verify in IBKR before any real order."
        )
    if not oi_known:
        warnings.append(
            "OI UNVERIFIED: no open-interest data in this chain snapshot. "
            "Check OI in IBKR/barchart before any real order."
        )
    return chosen, warnings


def generate_setup(
    ticker: str,
    spot: float,
    chain: Optional[OptionsChain],
    target_delta: float = config.TARGET_DELTA,
    next_earnings_days: Optional[int] = None,
    today: Optional[date] = None,
) -> Optional[VirtualSetup]:
    """
    Build a VirtualSetup for the given candidate. Returns None if no liquid
    contract is available in our DTE window.

    today: injectable as-of date. Production callers omit it; the backtest
    engine replays historical dates. Every DTE in the setup derives from
    this date, never from the wall clock.
    """
    reasoning: list[str] = []
    if chain is None:
        reasoning.append("no chain data — skipped")
        return None

    # Pick the expiration in the middle of our DTE window if possible
    if not chain.expirations:
        return None

    today = today or datetime.now().date()
    # Prefer mid-DTE expirations (35 ± 5)
    target_dte = (config.DTE_MIN + config.DTE_MAX) // 2
    expirations_sorted = sorted(
        chain.expirations,
        key=lambda e: abs((e.date() - today).days - target_dte),
    )

    chosen_contract = None
    chosen_exp = None
    quality_warnings: list[str] = []
    for exp in expirations_sorted:
        exp_dte = (exp.date() - today).days
        if earnings_inside_hold(exp_dte, next_earnings_days):
            reasoning.append(
                f"{exp.date()}: skipped — earnings in {next_earnings_days}d land "
                f"inside the {max(0, exp_dte - config.VIRTUAL_FORCE_EXIT_DTE)}d hold")
            continue
        c, warns = _pick_best_put(chain, exp, spot, target_delta)
        if c is not None:
            chosen_contract = c
            chosen_exp = exp
            quality_warnings = warns
            break

    if chosen_contract is None:
        reasoning.append("no liquid put found in DTE window")
        return None

    chosen_dte = max(0, (chosen_exp.date() - today).days)
    credit_per_share = chosen_contract.mid
    credit_per_contract = credit_per_share * 100
    strike = chosen_contract.strike

    # Credit-sanity gate: a garbage/misaligned quote (the LCID incident —
    # deep-OTM delta with deep-ITM premium) must never become a setup.
    from csp_screener.sanity import csp_credit_is_sane
    sane, why = csp_credit_is_sane(credit_per_contract, strike, chosen_contract.delta)
    if not sane:
        logger.warning(f"{ticker}: setup rejected by credit-sanity gate — {why}")
        return None

    # Friction-viability gate: a trade must be able to NET a profit at its
    # own take-profit exit. Production evidence (Jul 2026): T puts collecting
    # $1-2 credit fired 'take_profit_50pct' closes with NEGATIVE net P&L —
    # capturing 50% of a $2 credit ($1 gross) cannot survive the $2 round-trip
    # commission. Those trades were guaranteed losers at entry; refusing them
    # is economics, not strategy tuning (the live tier's $25 net floor is the
    # same idea — this is its scaled-down sandbox sibling).
    net_at_best_exit = net_at_tp_exit(credit_per_contract, "csp")
    if net_at_best_exit <= 0:
        logger.info(
            f"{ticker}: setup rejected — credit ${credit_per_contract:.2f} too "
            f"small to survive friction (a perfect 50% take-profit would net "
            f"${net_at_best_exit:.2f})"
        )
        return None

    max_loss_per_contract = (strike * 100) - credit_per_contract
    breakeven = strike - credit_per_share

    # Determine data quality flag
    if chosen_contract.source == "ibkr" and chosen_contract.delta is not None:
        quality = "ibkr_greeks"
    elif chosen_contract.iv is not None:
        quality = "yfinance_iv_estimated_delta"
    else:
        quality = "premium_only_no_greeks"
    if quality_warnings:
        quality += "_unverified_liquidity"
        reasoning.extend(quality_warnings)

    reasoning.append(
        f"Selected put at strike ${strike:.2f}, expiration {chosen_exp.date()}, "
        f"DTE {chosen_dte}, "
        f"{'delta ' + format(chosen_contract.delta, '+.3f') if chosen_contract.delta is not None else 'no delta'}"
    )
    reasoning.append(
        f"Liquidity OK: OI {chosen_contract.open_interest}, "
        f"bid/ask {chosen_contract.bid:.2f}/{chosen_contract.ask:.2f} "
        f"({chosen_contract.bid_ask_spread_pct:.1%} spread)"
    )

    from csp_screener import account as _account
    _afford = _account.assess_affordability(max_loss_per_contract)

    return VirtualSetup(
        ticker=ticker,
        spot_at_screen=round(spot, 2),
        expiration=chosen_exp.date().isoformat(),
        dte=chosen_dte,
        strike=strike,
        pct_otm=round((spot - strike) / spot, 4),
        delta=chosen_contract.delta,
        iv=chosen_contract.iv,
        bid=round(chosen_contract.bid, 4),
        ask=round(chosen_contract.ask, 4),
        mid=round(chosen_contract.mid, 4),
        bid_ask_pct=round(chosen_contract.bid_ask_spread_pct, 4),
        open_interest=chosen_contract.open_interest,
        volume=chosen_contract.volume,
        estimated_credit_per_contract=round(credit_per_contract, 2),
        max_loss_per_contract=round(max_loss_per_contract, 2),
        breakeven=round(breakeven, 2),
        data_quality=quality,
        reasoning=reasoning,
        structure="csp",
        tier="sandbox",
        affordable_contracts=_afford["affordable_contracts"],
        fits_account=_afford["fits_account"],
        pct_of_equity=round(_afford["pct_of_equity"], 4),
        sizing_note=_afford["note"],
        net_at_designed_exit=round(net_at_best_exit, 2),
    )


# ---------------------------------------------------------------------------
# LIVE tier: put credit spreads with staged tickets (playbook changes #1,5,7)
# ---------------------------------------------------------------------------

def generate_spread_setup(
    ticker: str,
    spot: float,
    chain: Optional[OptionsChain],
    target_delta: float = config.TARGET_DELTA,
    diagnostics: Optional[list] = None,
    next_earnings_days: Optional[int] = None,
    today: Optional[date] = None,
) -> Optional[VirtualSetup]:
    """
    Build a PUT CREDIT SPREAD setup for a live-tier candidate:
      - short put near target delta (25-30Δ) with LIVE quotes only
      - long put 1-2 dollars lower (defined risk)
      - hard viability gates: net credit after friction >= $25,
        friction <= 20% of credit, max risk <= MAX_RISK_PER_SPREAD
      - staged order ticket attached (the human only approves)

    Returns None if no viable spread exists — 'no trade' is a first-class
    outcome for the live tier.

    diagnostics: optional list — when provided, every void gets a
    human-readable reason appended (the NEAR-MISS LEDGER). A silent 'no'
    for months is the documented override trigger; 'no because credit was
    $31 vs the $40 floor' is a system working visibly.
    """
    def _diag(msg: str) -> None:
        # Dedupe: both width targets can resolve to the same long leg and
        # emit identical reasons — one entry per distinct reason.
        if diagnostics is not None and msg not in diagnostics:
            diagnostics.append(msg)

    if chain is None or not chain.expirations:
        _diag("no option chain data")
        return None

    replay = today is not None
    today = today or datetime.now().date()
    target_dte = (config.DTE_MIN + config.DTE_MAX) // 2
    expirations_sorted = sorted(
        chain.expirations,
        key=lambda e: abs((e.date() - today).days - target_dte),
    )

    for exp in expirations_sorted:
        exp_dte = (exp.date() - today).days
        if earnings_inside_hold(exp_dte, next_earnings_days):
            _diag(f"{exp.date()}: earnings in {next_earnings_days}d land inside "
                  f"the {max(0, exp_dte - config.VIRTUAL_FORCE_EXIT_DTE)}d hold")
            continue
        puts = chain.puts_for_expiration(exp)
        # LIVE tier demands live two-sided quotes — no stale-quote
        # degradation here. That leniency is sandbox-only.
        live_quoted = [p for p in puts if p.bid > 0 and p.ask > 0 and p.mid > 0]
        if puts and not live_quoted:
            _diag(f"{exp.date()}: no live two-sided quotes "
                  f"(market closed or data outage — not a market verdict)")

        # OPEN-INTEREST gate — the real-money tier had NO OI floor while the
        # paper sandbox enforced 500 (measured: 96% of the contracts the live
        # path would consider sat under the floor, median OI 36; one staged
        # ticket's short leg had OI 30). Same graceful degradation as the
        # sandbox: if the whole snapshot reports zero OI, treat it as UNKNOWN
        # rather than voiding the chain.
        oi_known = any(p.open_interest > 0 for p in live_quoted)

        def oi_ok(p: OptionContract) -> bool:
            return (not oi_known) or p.open_interest >= config.MIN_OPEN_INTEREST

        # SHORT leg gate: % of mid (this is where the premium lives)
        short_candidates = [
            p for p in live_quoted
            if p.bid_ask_spread_pct <= config.MAX_BID_ASK_PCT_OF_MID and oi_ok(p)
        ]
        # LONG leg gate: cheap wings always look wide in % terms — what
        # costs you is absolute cents. Allow <= $0.05 or <= 10% of mid.
        long_candidates = [
            p for p in live_quoted
            if (p.ask - p.bid) <= max(0.05, 0.10 * p.mid) and oi_ok(p)
        ]
        if live_quoted and not short_candidates:
            deepest = max((p.open_interest for p in live_quoted), default=0)
            _diag(f"{exp.date()}: no strike passes both the spread gate and "
                  f"OI >= {config.MIN_OPEN_INTEREST} (best OI in chain: {deepest})")
        if not short_candidates or len(long_candidates) < 1:
            if live_quoted:
                _diag(f"{exp.date()}: quotes live but spreads too wide "
                      f"(> {config.MAX_BID_ASK_PCT_OF_MID:.0%} of mid)")
            continue

        # Short leg: closest to target delta, else ~5% OTM.
        # Delta cap: the playbook spec is a 25-30 delta short leg. Without
        # the cap the closest-to-target pick can land at 0.45+ when the
        # chain is sparse — a rule violation, not a candidate. (Red-team
        # finding: this cap existed only in the sandbox path.)
        with_delta = [p for p in short_candidates
                      if p.delta is not None and abs(p.delta) <= MAX_CSP_DELTA]
        if with_delta:
            short_leg = min(with_delta, key=lambda p: abs(abs(p.delta) - target_delta))
        else:
            no_delta = [p for p in short_candidates if p.delta is None]
            if not no_delta:
                _diag(f"{exp.date()}: all quoted strikes above the "
                      f"{MAX_CSP_DELTA:.2f} delta cap — too close to the money")
                continue
            short_leg = min(no_delta, key=lambda p: abs(p.strike - spot * 0.95))
        if short_leg.strike >= spot:  # never sell ITM puts
            _diag(f"{exp.date()}: best short leg would be ITM (strike "
                  f"{short_leg.strike:g} >= spot {spot:.2f})")
            continue

        # Long leg: try the tier-preferred width first, then the other —
        # the friction gates are tight enough that only one width may pass.
        lower = [p for p in long_candidates if p.strike < short_leg.strike]
        if not lower:
            _diag(f"{exp.date()}: no liquid long leg below the "
                  f"{short_leg.strike:g} short strike")
            continue
        preferred = (config.SPREAD_WIDTH_NARROW if spot <= 30
                     else config.SPREAD_WIDTH_WIDE)
        widths_to_try = [preferred] + [
            w for w in (config.SPREAD_WIDTH_NARROW, config.SPREAD_WIDTH_WIDE)
            if w != preferred
        ]

        long_leg = None
        width = credit = max_loss = friction = net_after_friction = None
        net_credit_share = None
        for width_target in widths_to_try:
            cand = min(lower, key=lambda p: abs((short_leg.strike - p.strike) - width_target))
            w = short_leg.strike - cand.strike
            if w <= 0 or w > config.SPREAD_WIDTH_WIDE * 1.5:
                continue
            ncs = short_leg.mid - cand.mid
            if ncs <= 0:
                _diag(f"{exp.date()}: ${w:g}-wide at {short_leg.strike:g}/"
                      f"{cand.strike:g} nets zero credit")
                continue
            cr = ncs * 100.0
            ml = w * 100.0 - cr
            # STRUCTURAL ceiling only (the strategy's defined-risk design).
            # Deliberately NOT the account's equity-scaled cap: signal
            # quality and position sizing are separate jobs. A good spread
            # must not disappear because this month's balance is small —
            # it gets found, tracked, and annotated with what's affordable.
            if ml > config.MAX_RISK_PER_SPREAD:
                _diag(f"{exp.date()}: ${w:g}-wide risks ${ml:.0f} > "
                      f"${config.MAX_RISK_PER_SPREAD:.0f} structural ceiling")
                continue
            # Friction: 2 legs open + 2 legs close = 4 contracts of commission,
            # plus slippage on the credit both ways.
            fr = 4 * config.COMMISSION_PER_CONTRACT + \
                2 * config.SLIPPAGE_PCT_OF_PREMIUM * cr
            naf = cr - fr
            # Viability gates (playbook change #7). The floor is measured on
            # what the trade ACTUALLY nets at its own designed exit — the
            # attached 50% take-profit — not on the expire-worthless number.
            # Measuring the latter let a "$25 net" ticket really deliver ~$14.
            net_designed = net_at_tp_exit(cr, "put_credit_spread")
            if net_designed < config.MIN_NET_CREDIT_AFTER_FRICTION:
                _diag(f"{exp.date()}: ${w:g}-wide at {short_leg.strike:g}/"
                      f"{cand.strike:g} nets ${net_designed:.0f} at its 50% "
                      f"take-profit vs the ${config.MIN_NET_CREDIT_AFTER_FRICTION:.0f} "
                      f"floor (credit ${cr:.0f}; ${naf:.0f} only if held to "
                      f"expiry) — NEAR MISS"
                      if net_designed > 0 else
                      f"{exp.date()}: ${w:g}-wide credit ${cr:.0f} cannot "
                      f"survive its own exit friction (${fr:.2f})")
                continue
            if fr > config.MAX_FRICTION_PCT_OF_CREDIT * cr:
                _diag(f"{exp.date()}: friction ${fr:.2f} is "
                      f"{fr / cr:.0%} of the ${cr:.0f} credit "
                      f"(cap {config.MAX_FRICTION_PCT_OF_CREDIT:.0%})")
                continue
            long_leg, width, net_credit_share = cand, w, ncs
            credit, max_loss, friction, net_after_friction = cr, ml, fr, naf
            break

        if long_leg is None:
            continue

        # Credit-sanity gate: net credit approaching/exceeding the strike
        # width means a garbage or misaligned quote pair.
        from csp_screener.sanity import spread_credit_is_sane
        sane, why = spread_credit_is_sane(credit, short_leg.strike, long_leg.strike)
        if not sane:
            logger.warning(f"{ticker}: spread rejected by credit-sanity gate — {why}")
            continue

        breakeven = short_leg.strike - net_credit_share
        short_dte = max(0, (exp.date() - today).days)
        exp_str = exp.strftime("%d%b%y").upper()
        tp_price = round(net_credit_share * (1 - config.VIRTUAL_TP_PCT), 2)
        close_by = (exp.date() - timedelta(days=config.VIRTUAL_FORCE_EXIT_DTE)).isoformat()

        # HONEST NET: `net_after_friction` is the net only if the spread
        # expires worthless — the one outcome this ticket's own exit plan
        # forbids (a GTC buyback at 50% and a forced 21-DTE close). Entries
        # land at 30-37 DTE, so the 21-DTE exit always fires first. The
        # designed outcome nets ~2.3x LESS. Print both; lead with the one
        # the attached plan actually delivers.
        net_at_designed_exit = net_at_tp_exit(credit, "put_credit_spread")

        # The go-live gate decides what this text CLAIMS to be. Before the
        # gate opens it is a research signal, never an order to place.
        # Replay mode never reads the gate (it reads the live journal, which
        # a backtest must not touch) — a replayed ticket is never approvable.
        from csp_screener import golive
        if replay:
            gate = {
                "passed": False, "closed_trades": 0,
                "required_trades": golive.MIN_CLOSED_PAPER_TRADES,
                "earliest_date": golive.EARLIEST_LIVE_DATE.isoformat(),
            }
        else:
            gate = golive.gate_status()
        if gate["passed"]:
            header = (f"SELL -1 {ticker} {exp_str} {short_leg.strike:g}P / "
                      f"BUY +1 {ticker} {exp_str} {long_leg.strike:g}P")
        else:
            header = (
                f"RESEARCH SIGNAL — NOT approved for real money\n"
                f"({gate['closed_trades']}/{gate['required_trades']} paper "
                f"trades · gate opens {gate['earliest_date']})\n"
                f"Would be: SELL -1 {ticker} {exp_str} {short_leg.strike:g}P / "
                f"BUY +1 {ticker} {exp_str} {long_leg.strike:g}P")
        ticket = (
            f"{header}\n"
            f"LMT @ {net_credit_share:.2f} credit (mid) DAY | "
            f"GTC buyback @ {tp_price:.2f} (50% TP) | "
            f"close by {close_by} (21 DTE)\n"
            f"Max risk ${max_loss:.0f} | "
            f"Net at the attached 50% take-profit ~${net_at_designed_exit:.0f} "
            f"(${net_after_friction:.0f} only if held to expiry, which this "
            f"plan does not do)"
        )

        # Sizing annotation (never a gate — see account.assess_affordability)
        from csp_screener import account
        afford = account.assess_affordability(max_loss)

        quality = ("ibkr_greeks" if (short_leg.source == "ibkr" and short_leg.delta is not None)
                   else "yfinance_iv_estimated_delta" if short_leg.iv is not None
                   else "premium_only_no_greeks")

        return VirtualSetup(
            ticker=ticker,
            spot_at_screen=round(spot, 2),
            expiration=exp.date().isoformat(),
            dte=short_dte,
            strike=short_leg.strike,
            pct_otm=round((spot - short_leg.strike) / spot, 4),
            delta=short_leg.delta,
            iv=short_leg.iv,
            bid=round(short_leg.bid, 4),
            ask=round(short_leg.ask, 4),
            mid=round(net_credit_share, 4),  # mid = NET spread credit per share
            bid_ask_pct=round(short_leg.bid_ask_spread_pct, 4),
            open_interest=short_leg.open_interest,
            volume=short_leg.volume,
            estimated_credit_per_contract=round(credit, 2),
            max_loss_per_contract=round(max_loss, 2),
            breakeven=round(breakeven, 2),
            data_quality=quality,
            reasoning=[
                f"Put credit spread {short_leg.strike:g}/{long_leg.strike:g}, "
                f"width ${width:g}, DTE {short_dte}",
                f"Credit ${credit:.2f}, friction ~${friction:.2f}, "
                f"net ${net_after_friction:.2f}, max risk ${max_loss:.0f}",
            ],
            structure="put_credit_spread",
            long_strike=long_leg.strike,
            tier="live",
            net_credit_after_friction=round(net_after_friction, 2),
            friction_estimate=round(friction, 2),
            ticket=ticket,
            affordable_contracts=afford["affordable_contracts"],
            fits_account=afford["fits_account"],
            pct_of_equity=round(afford["pct_of_equity"], 4),
            sizing_note=afford["note"],
            net_at_designed_exit=round(net_at_designed_exit, 2),
        )

    return None
