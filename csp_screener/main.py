"""
Sunday screener orchestrator. Run weekly via Windows Task Scheduler.

Pipeline:
  1. Mark virtual positions to market and apply exit rules (uses today's data)
  2. Run weekly screen: universe -> filters -> ranker -> setup generator
  3. Open new virtual positions for the top candidates
  4. Compute screener performance summary
  5. Render + send weekly email
  6. Append screen record to journal
  7. Ping deadman switch

Idempotent: running twice on the same Sunday will only OPEN one round of
virtual trades (deduped by trade_id).
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Add parent directory so 'csp_screener' is importable when running as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csp_screener import (
    account,
    config,
    data_pipeline,
    deadman,
    earnings,
    evaluator,
    filters,
    journal,
    notify,
    options_data,
    ranker,
    setup_generator,
    universe,
    virtual_tracker,
)
from csp_screener.filters import TickerContext


def _setup_logging():
    log_file = config.LOGS_DIR / f"screener_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


logger = logging.getLogger("csp_screener.main")

# Days a ticker sits out after any close before it may reopen (churn guard;
# structural bound like the sanity caps — deliberately outside config.py).
REOPEN_COOLDOWN_DAYS = 3


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_mark_virtual_to_market(price_data: dict, eur_usd_rate=None) -> dict:
    """
    Daily/Sunday update of open virtual positions. Uses current spot + realized
    vol as the IV proxy for the BS re-pricer.
    """
    def spot_resolver(ticker):
        df = price_data.get(ticker)
        return data_pipeline.last_price(df) if df is not None else None

    def iv_resolver(ticker):
        df = price_data.get(ticker)
        if df is None:
            return None
        # Use 30-day realized vol as IV proxy (slightly longer than 20 to dampen)
        return data_pipeline.recent_realized_vol(df, window=30) or 0.30  # 30% default

    # Market-quote marks only make sense while the market can quote —
    # otherwise every fetch is a wasted network call that returns None
    # anyway (the two-sided-quote gate rejects after-hours rows). The same
    # flag drives market-hours exit execution: off-hours exit crossings
    # defer to the next open run instead of closing on a model mark.
    market_open = us_market_likely_open()
    quote_resolver = (virtual_tracker.market_quote_resolver
                      if market_open else None)
    summary = virtual_tracker.update_all_open_positions(
        spot_resolver, iv_resolver, eur_usd_rate=eur_usd_rate,
        quote_resolver=quote_resolver, market_open=market_open)
    logger.info(
        f"Virtual mark-to-market: {summary['updated']} updated, "
        f"{summary['closed']} closed (PnL ${summary['closed_pnl_total']:+.2f}), "
        f"{summary.get('deferred', 0)} exit(s) deferred to market hours"
    )
    # Shadow book marks AFTER production (the production pass warms the
    # per-run quote cache the shadows free-ride on). Reuses the SAME
    # market_open flag — recomputing after minutes of quote fetches could
    # straddle the session boundary and give the two cohorts different
    # execution rules within one run. Never breaks a run.
    try:
        from csp_screener import shadow_book
        summary["shadow"] = shadow_book.mark_open_shadows(
            spot_resolver, iv_resolver, eur_usd_rate=eur_usd_rate,
            market_open=market_open)
    except Exception as e:
        logger.warning(f"Shadow marking skipped (non-fatal): {e}")
    return summary


def step_build_contexts(
    price_data: dict,
    earnings_data: dict,
    now: datetime,
    tier: str = "sandbox",
) -> list[TickerContext]:
    """Build TickerContext for every ticker in the given tier's universe."""
    if tier == "live":
        price_min, price_max = config.LIVE_PRICE_MIN, config.LIVE_PRICE_MAX
    else:
        price_min, price_max = config.PRICE_MIN, config.PRICE_MAX

    contexts = []
    stale_skipped = 0
    for ticker in universe.get_universe(tier):
        df = price_data.get(ticker)
        if df is None or df.empty:
            continue
        # Refuse to screen on stale prices. Every gate downstream (strike
        # choice, never-sell-ITM, delta cap, pct_otm) trusts this spot; a
        # data hole must degrade to "no candidate", never to a silent
        # decision on an old price.
        if data_pipeline.is_stale(df):
            stale_skipped += 1
            continue
        ctx = TickerContext(
            ticker=ticker,
            last_price=data_pipeline.last_price(df) or float("nan"),
            avg_volume_20d=data_pipeline.avg_volume_20d(df) or 0.0,
            next_earnings=earnings_data.get(ticker),
            price_history=(df["Adj Close"] if "Adj Close" in df.columns else df["Close"]).copy(),
            price_min=price_min,
            price_max=price_max,
        )
        contexts.append(ctx)
    if stale_skipped:
        logger.warning(
            f"[{tier}] skipped {stale_skipped} ticker(s) with stale price data "
            f"(newest bar older than the weekend/holiday tolerance)"
        )
    return contexts


def step_filter_and_rank(
    contexts: list[TickerContext],
    now: datetime,
    vix: float | None = None,
    shadow_pool: list | None = None,
    earnings_rejects: list | None = None,
) -> list[dict]:
    """
    Apply filters, then rank passing candidates (with learning-layer weights).

    shadow_pool / earnings_rejects: optional out-params (the near-miss-ledger
    idiom). When given, shadow_pool receives every filter-passer ranked BELOW
    the returned top-N (with its full-ranking position), and earnings_rejects
    receives tickers whose ONLY failing filter was the earnings blackout —
    the two cohorts the shadow book tracks. Passing neither changes nothing.
    """
    passing = []
    for ctx in contexts:
        result = filters.apply_all_filters(ctx, now=now)
        next_earn_days = None
        if ctx.next_earnings is not None:
            next_earn_days = (ctx.next_earnings - now).days
        if not result.passed:
            # Earnings runs LAST in ALL_FILTERS, so an earnings rejection
            # means every other filter passed — exactly the autopsy cohort.
            if (earnings_rejects is not None
                    and result.reason.startswith("earnings in")):
                earnings_rejects.append({
                    "ticker": ctx.ticker,
                    "last_price": ctx.last_price,
                    "avg_volume_20d": ctx.avg_volume_20d,
                    "next_earnings_days": next_earn_days,
                })
            continue
        passing.append({
            "ticker": ctx.ticker,
            "last_price": ctx.last_price,
            "avg_volume_20d": ctx.avg_volume_20d,
            "next_earnings_days": next_earn_days,
            "price_history": ctx.price_history,
        })

    logger.info(f"Filters: {len(contexts)} -> {len(passing)} candidates passed")

    # Learning layer: score tickers from closed virtual trade history
    ticker_scores = None
    try:
        from csp_screener.learning import ticker_scorer
        from csp_screener.evaluator import _reconstruct_closed_trades
        closed = _reconstruct_closed_trades()
        ticker_scores = ticker_scorer.score_all_tickers(closed)
        if ticker_scores:
            adjusted = sum(1 for s in ticker_scores.values() if s.sample_quality != "thin")
            logger.info(f"Learning: {adjusted} tickers have meaningful score (out of {len(ticker_scores)} with history)")
    except Exception as e:
        logger.debug(f"Learning layer skipped: {e}")

    ranked = ranker.rank_candidates(
        passing,
        top_n=config.MAX_CANDIDATES_IN_EMAIL,
        ticker_scores=ticker_scores,
        current_vix=vix,
    )
    logger.info(f"Ranker: top {len(ranked)} candidates selected (learning-aware)")

    # Shadow pool: the SAME ranking over the full field, so every discard
    # carries its true position. Re-ranking is milliseconds (pure pandas on
    # already-loaded history) and leaves the production top-N untouched.
    if shadow_pool is not None and passing:
        try:
            full = ranker.rank_candidates(
                passing, top_n=len(passing),
                ticker_scores=ticker_scores, current_vix=vix)
            top_tickers = {r.ticker for r in ranked}
            for pos, cand in enumerate(full, start=1):
                if cand.ticker in top_tickers:
                    continue
                shadow_pool.append({
                    "ticker": cand.ticker,
                    "last_price": cand.last_price,
                    "rank_full": pos,
                    "rv_percentile": cand.rv_percentile,
                    "rv_20d_annual": cand.rv_20d_annual,
                    "avg_volume_20d": cand.avg_volume_20d,
                    "next_earnings_days": cand.next_earnings_days,
                })
        except Exception as e:
            logger.warning(f"Shadow pool collection skipped (non-fatal): {e}")
    return ranked


def step_generate_setups(ranked, ibkr_client=None, tier: str = "sandbox") -> list[dict]:
    """
    For each ranked candidate, generate a virtual setup.
    LIVE tier: put credit spreads with strict gates + staged tickets,
    plus the ex-dividend blackout. SANDBOX: original CSP setups.
    """
    out = []
    for r in ranked:
        # LIVE-tier ex-div blackout (playbook change #10): a short put
        # assigned across the ex-date triggers 15% NRA withholding.
        if tier == "live":
            try:
                ex_div = earnings.fetch_ex_dividend(r.ticker)
                if ex_div is not None:
                    days_out = (ex_div.date() - datetime.now().date()).days
                    if 0 <= days_out <= config.DTE_MAX:
                        logger.info(f"[live] {r.ticker} skipped: ex-div in {days_out}d")
                        out.append({
                            "ticker": r.ticker, "last_price": r.last_price,
                            "rv_percentile": r.rv_percentile,
                            "rv_20d_annual": r.rv_20d_annual,
                            "avg_volume_20d": r.avg_volume_20d,
                            "next_earnings_days": r.next_earnings_days,
                            "rank": r.rank, "tier": tier, "setup": None,
                            "skip_reason": f"ex-dividend in {days_out}d (blackout)",
                        })
                        continue
            except Exception as e:
                logger.debug(f"ex-div check failed for {r.ticker}: {e}")

        chain = options_data.fetch_chain(r.ticker, r.last_price, ibkr_client=ibkr_client)
        void_reasons: list[str] = []
        if tier == "live":
            setup = setup_generator.generate_spread_setup(
                r.ticker, r.last_price, chain, diagnostics=void_reasons,
                next_earnings_days=r.next_earnings_days)
        else:
            setup = setup_generator.generate_setup(
                r.ticker, r.last_price, chain,
                next_earnings_days=r.next_earnings_days)
        entry = {
            "ticker": r.ticker,
            "last_price": r.last_price,
            "rv_percentile": r.rv_percentile,
            "rv_20d_annual": r.rv_20d_annual,
            "avg_volume_20d": r.avg_volume_20d,
            "next_earnings_days": r.next_earnings_days,
            "rank": r.rank,
            "tier": tier,
            "setup": setup.to_dict() if setup else None,
        }
        # Near-miss ledger: WHY a live candidate voided, with numbers —
        # a silent 'no' for months is the documented override trigger.
        if tier == "live" and setup is None and void_reasons:
            entry["void_reasons"] = void_reasons[:6]
            logger.info(f"[live] {r.ticker} voided: {void_reasons[0]}")
        out.append(entry)
    return out


def us_market_likely_open(now: datetime | None = None) -> bool:
    """
    Is the US equity market plausibly open right now? Computed in ACTUAL
    America/New_York time (09:30-16:00 ET, Mon-Fri), not a UTC union.

    History: this started as a coarse DST-union window (13:30-21:00 UTC)
    "used only for LABELING" — then market-hours exit execution promoted it
    to gating real behavior, and the adversarial review proved the union
    leaked: the 20:35 UTC cron slot is 16:35 EDT, AFTER the close, yet
    counted as open — so exits still executed there on after-hours model
    marks daily for the ~8 EDT months (the exact pathology the deferral
    exists to stop), and winter 13:30-14:30 UTC counted pre-open hours as
    open. DST-correct ET closes both holes; no legitimate slot is lost.

    `now` naive = interpreted as UTC (matches all callers and the crons).
    Holidays are still not modeled; worst case a holiday run executes at a
    model mark, same exposure as before.
    """
    from datetime import timezone
    from zoneinfo import ZoneInfo
    now = now or datetime.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def step_open_virtual_positions(
    candidates: list[dict],
    screen_id: str,
    vix: float | None = None,
) -> list[str]:
    """
    Open virtual positions for each candidate that has a valid setup.

    Idempotency is enforced by CONTRACT, not by screen_id: if a virtual
    position for the same (ticker, strike, expiration) is already open —
    from a re-run today or from last week's screen — we skip it. One open
    position per TICKER: with daily runs feeding the paper pool, the same
    name resurfaces day after day at drifting strikes, and stacking
    near-identical positions would fill the pool with correlated duplicates
    instead of the diverse 200+ trades the playbook gate needs. Also
    enforces the MAX_VIRTUAL_OPEN portfolio cap.
    """
    from csp_screener.setup_generator import VirtualSetup

    open_trades = virtual_tracker.get_open_virtual_trades()
    already_open = {
        (t.ticker, round(float(t.strike), 4), t.expiration.date().isoformat())
        for t in open_trades
    }
    open_tickers = {t.ticker for t in open_trades}
    # Reopen cooldown: a ticker that just closed must sit out a few days —
    # otherwise a name that take-profits overnight churns open→close→reopen
    # daily (T did four laps in four days), paying friction every lap.
    cooldown_tickers = virtual_tracker.recently_closed_tickers(REOPEN_COOLDOWN_DAYS)
    open_count = len(already_open)

    # Portfolio caps on the LIVE tier only. These were printed everywhere but
    # enforced nowhere, so the paper book could hold a portfolio the playbook
    # forbids — and "would this ACCOUNT have made money" became unanswerable.
    # Sandbox stays unthrottled on purpose (playbook change #8 wants volume).
    live_open = [t for t in open_trades if t.tier == "live"]
    live_slots_used = len(live_open)
    live_risk_used = sum(float(t.max_loss or 0) for t in live_open)

    opened = []
    for c in candidates:
        if not c.get("setup"):
            continue
        setup_dict = c["setup"]
        key = (
            setup_dict["ticker"],
            round(float(setup_dict["strike"]), 4),
            setup_dict["expiration"],
        )
        if key in already_open:
            logger.info(f"Skipping duplicate virtual open: {key}")
            continue
        if setup_dict["ticker"] in open_tickers:
            logger.info(
                f"Skipping {setup_dict['ticker']}: ticker already has an "
                f"open virtual position"
            )
            continue
        if setup_dict["ticker"] in cooldown_tickers:
            logger.info(
                f"Skipping {setup_dict['ticker']}: closed within the last "
                f"{REOPEN_COOLDOWN_DAYS}d (reopen cooldown)"
            )
            continue
        if open_count + len(opened) >= config.MAX_VIRTUAL_OPEN:
            logger.warning(
                f"MAX_VIRTUAL_OPEN ({config.MAX_VIRTUAL_OPEN}) reached — "
                f"not opening further virtual positions this run"
            )
            break
        # SIZING IS NOT A SIGNAL FILTER. Every valid signal is tracked so the
        # evidence base measures whether the STRATEGY works — that is what the
        # January gate needs, and it must not be thinned by whatever this
        # month's balance happens to be. What the account could actually have
        # carried is recorded alongside (portfolio_fit), so the account-level
        # scoreboard stays answerable too.
        risk = float(setup_dict.get("max_loss_per_contract") or 0)
        is_live = (setup_dict.get("tier") or "sandbox") == "live"
        portfolio_fit = True
        if is_live:
            if (live_slots_used >= config.MAX_OPEN_POSITIONS
                    or live_risk_used + risk > account.max_total_risk()):
                portfolio_fit = False
                logger.info(
                    f"{setup_dict['ticker']}: tracked as a signal, but the "
                    f"account could not have carried it "
                    f"(slots {live_slots_used}/{config.MAX_OPEN_POSITIONS}, "
                    f"open risk ${live_risk_used:.0f} + ${risk:.0f} vs "
                    f"${account.max_total_risk():.0f} budget)")
            else:
                live_slots_used += 1
                live_risk_used += risk
        setup = VirtualSetup(**setup_dict)
        trade_id = virtual_tracker.open_virtual_position(
            setup, screen_id,
            rv_percentile=c.get("rv_percentile"), vix=vix,
            portfolio_fit=portfolio_fit,
        )
        opened.append(trade_id)
        already_open.add(key)
        open_tickers.add(setup_dict["ticker"])
    return opened


def step_compute_open_positions_data(price_data: dict) -> list[dict]:
    """Compute current P&L for each open virtual trade — for email display."""
    rows = []
    open_trades = virtual_tracker.get_open_virtual_trades()
    # Same mark basis as the close path — a table showing model +$12 next to
    # a market-marked -$25 close erodes trust in both numbers. The per-run
    # quote cache is already warm from the mark step, so this is ~free.
    use_quotes = us_market_likely_open()
    for t in open_trades:
        df = price_data.get(t.ticker)
        if df is None or df.empty:
            continue
        spot = data_pipeline.last_price(df)
        iv = data_pipeline.recent_realized_vol(df, window=30) or 0.30
        market_price = (virtual_tracker.market_quote_resolver(t)
                        if use_quotes else None)
        result = virtual_tracker.evaluate_open_position(
            t, spot, iv, market_price=market_price)
        rows.append({
            "ticker": t.ticker,
            "strike": t.strike,
            "expiration": t.expiration.date().isoformat(),
            "dte_remaining": result["dte_remaining"],
            "credit_received": t.credit_received,
            "max_loss": t.max_loss,
            "pnl_now": result["pnl"],
            "pnl_pct_now": result["pnl_pct_of_credit"],
            "tier": t.tier,
            "structure": t.structure,
            "long_strike": t.long_strike,
        })
    return rows


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def assemble_health(
    vix: float,
    earnings_data: dict,
    ranked_count: int,
    earnings_canary: dict | None = None,
) -> dict:
    warnings = []
    if vix is None:
        warnings.append(
            "VIX unavailable this run — the VIX kill switch could not be "
            "evaluated. Check market regime manually before trading."
        )
    if vix is not None and vix > config.VIX_KILL_SWITCH:
        warnings.append(
            f"VIX {vix:.1f} > kill-switch {config.VIX_KILL_SWITCH}. "
            f"Screener returned no candidates."
        )
    missing_earnings = sum(1 for v in earnings_data.values() if v is None)
    if earnings_data and missing_earnings / len(earnings_data) > 0.5:
        if earnings_canary is not None and not earnings_canary.get("healthy", True):
            warnings.append(
                f"EARNINGS DATA SOURCES BROKEN (canaries failed: "
                f"{', '.join(earnings_canary.get('broken', []))}). The earnings "
                f"blackout filter is NOT protecting you this week — verify "
                f"earnings dates manually before any real trade."
            )
        else:
            warnings.append(
                f"Missing earnings data for {missing_earnings}/{len(earnings_data)} "
                f"tickers (sources appear healthy — thin coverage). "
                f"Check Finnhub API key if this persists."
            )
    if ranked_count == 0:
        warnings.append("Zero candidates passed filters this week.")
    return {"warnings": warnings, "vix": vix}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_weekly_screen(
    force_send: bool = False,
    use_ibkr: bool = True,
    run_type: str = "weekly",
) -> int:
    """
    Full screen pipeline. run_type='weekly' is the Sunday run (sends the
    email digest); run_type='daily' is the weekday indications run — same
    pipeline, same virtual opens (feeds the paper pool toward the playbook's
    200+ trade gate), but NO email: the dashboard's Daily tab is the surface.
    Returns exit code (0 OK, 1 failure).
    """
    is_daily = run_type == "daily"
    _setup_logging()
    deadman.ping_start()
    now = datetime.now()
    prefix = "daily" if is_daily else "screen"
    screen_id = f"{prefix}_{now.strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:6]}"

    logger.info("=" * 50)
    logger.info(f"CSP Screener {run_type} run starting (screen_id={screen_id})")

    try:
        # -1. Hydrate local journal from Supabase. CRITICAL on GitHub Actions:
        # the runner's filesystem is fresh every run, so open virtual positions
        # only exist in the cloud. Without this, mark-to-market sees nothing.
        try:
            from csp_screener import supabase_sync
            hyd = supabase_sync.hydrate_virtual_trades()
            if hyd.get("added"):
                logger.info(f"Hydration: pulled {hyd['added']} records from Supabase")
            shyd = supabase_sync.hydrate_shadow_trades()
            if shyd.get("added"):
                logger.info(f"Hydration: pulled {shyd['added']} shadow records")
        except Exception as e:
            logger.debug(f"Hydration skipped: {e}")

        # 0. Fetch all required market data up front (both tiers at once)
        tickers = universe.all_tickers()
        logger.info(f"Universe: {len(tickers)} tickers "
                    f"(live {len(universe.get_universe('live'))}, "
                    f"sandbox {len(universe.get_universe('sandbox'))})")
        price_data = data_pipeline.fetch_prices(tickers, lookback_days=400)
        logger.info(f"Price data: {len(price_data)} tickers loaded")

        vix = data_pipeline.get_vix_level()
        logger.info(f"VIX: {vix}")

        eur_usd = data_pipeline.get_eurusd_rate() if config.TRACK_EUR else None
        logger.info(f"EURUSD: {eur_usd}")

        post_spike = data_pipeline.get_vix_post_spike_window(
            kill_level=config.VIX_KILL_SWITCH,
            recovery_level=config.VIX_POST_SPIKE_RECOVERY,
            lookback_days=config.VIX_SPIKE_LOOKBACK_DAYS,
        )
        if post_spike:
            logger.info("POST-SPIKE WINDOW: VIX spiked and recovered — "
                        "richest premium-selling regime")

        from csp_screener import fomc
        fomc_days = fomc.days_to_fomc()

        # 0b. VIX kill switch (daily runs skip the email — weekly already sent it).
        # A minimal screens record is still journaled: without it the CI
        # "verify data landed" gate goes falsely red on kill days, the
        # dashboard shows stale data with no explanation, and kill days
        # vanish from the track record.
        if vix is not None and vix > config.VIX_KILL_SWITCH:
            logger.warning(f"VIX {vix:.1f} > kill switch {config.VIX_KILL_SWITCH}")
            if not is_daily:
                kill_email(now, vix)
            journal.append("screens", {
                "screen_id": screen_id,
                "run_type": run_type,
                "ran_at": now.isoformat(),
                "universe_size": len(tickers),
                "passed_filters": 0,
                "candidates_in_email": 0,
                "virtual_positions_opened": 0,
                "virtual_closed_this_run": 0,
                "virtual_closed_pnl": 0.0,
                "vix": vix,
                "email_sent": False,
                "tickers_in_email": [],
                "candidates_payload": [],
                "live_viable": 0,
                "no_trade_week": True,
                "eur_usd_rate": eur_usd,
            })
            deadman.ping_success(f"VIX kill switch triggered: {vix:.1f}")
            return 0

        # 1. Mark virtual positions to market FIRST (before opening new ones)
        mark_summary = step_mark_virtual_to_market(price_data, eur_usd_rate=eur_usd)

        # 2. Fetch earnings for the universe (cached daily inside)
        earnings_data = earnings.fetch_batch_earnings(tickers)

        # 2b. If earnings coverage collapsed, run the canary check to tell
        # apart "thin coverage" from "both data sources are broken" — the
        # earnings blackout filter silently stops protecting you in the
        # second case, and that MUST be surfaced loudly.
        earnings_canary = None
        missing = sum(1 for v in earnings_data.values() if v is None)
        if earnings_data and missing / len(earnings_data) > 0.5:
            try:
                earnings_canary = earnings.sanity_check_data_sources()
                logger.warning(
                    f"Earnings coverage low ({missing}/{len(earnings_data)} missing); "
                    f"canary healthy={earnings_canary.get('healthy')}"
                )
            except Exception as e:
                logger.debug(f"Earnings canary failed: {e}")

        # 3. Build contexts, filter, rank — BOTH TIERS (learning-aware)
        ibkr_client = _try_connect_ibkr() if use_ibkr else None

        # Shadow-book cohorts collected while ranking (out-param idiom):
        # discards below the cutoff + earnings-only rejects, per tier.
        live_shadow_pool: list = []
        live_earnings_rejects: list = []
        sandbox_shadow_pool: list = []
        sandbox_earnings_rejects: list = []

        # LIVE tier: $20-60 liquid names -> put credit spreads + tickets
        live_contexts = step_build_contexts(price_data, earnings_data, now, tier="live")
        live_ranked = step_filter_and_rank(
            live_contexts, now, vix=vix,
            shadow_pool=live_shadow_pool, earnings_rejects=live_earnings_rejects)
        live_candidates = step_generate_setups(live_ranked, ibkr_client=ibkr_client, tier="live")
        live_viable = [c for c in live_candidates if c.get("setup")]
        no_trade_week = len(live_viable) == 0
        if no_trade_week:
            logger.info("LIVE tier: NO TRADE THIS WEEK — no spread passed the gates")

        # SANDBOX tier: original $5-25 CSP research track (unchanged signals)
        sandbox_contexts = step_build_contexts(price_data, earnings_data, now, tier="sandbox")
        sandbox_ranked = step_filter_and_rank(
            sandbox_contexts, now, vix=vix,
            shadow_pool=sandbox_shadow_pool, earnings_rejects=sandbox_earnings_rejects)
        sandbox_candidates = step_generate_setups(sandbox_ranked, ibkr_client=ibkr_client, tier="sandbox")

        if ibkr_client is not None:
            try:
                ibkr_client.disconnect()
            except Exception:
                pass

        candidates = live_candidates + sandbox_candidates
        ranked = live_ranked + sandbox_ranked  # for health check counts

        # 6. Open virtual positions for BOTH tiers (unthrottled paper engine)
        opened = step_open_virtual_positions(candidates, screen_id, vix=vix)
        logger.info(f"Opened {len(opened)} virtual positions "
                    f"({len(live_viable)} live-tier viable)")

        # 7. Compute summaries + open positions data for email
        summaries = evaluator.all_periods_summary()
        open_positions = step_compute_open_positions_data(price_data)

        # 7b. Run learning layer — produces recommendations from history
        recommendations = []
        try:
            from csp_screener.learning import (
                ticker_scorer, feature_analyzer, recommender,
            )
            from csp_screener.evaluator import _reconstruct_closed_trades
            closed = _reconstruct_closed_trades()
            ts = ticker_scorer.score_all_tickers(closed)
            baseline = feature_analyzer.overall_baseline(closed)
            # screens_by_id lets the analyzer recover RV/VIX entry context
            # for trades that predate the at-open stamping. Local journal
            # first, then the cloud — on GitHub Actions the local screens
            # journal is empty (only virtual_trades hydrates), so Supabase
            # is the only source with history there.
            screens_by_id = {
                r.get("screen_id"): r
                for r in journal.read_all("screens") if r.get("screen_id")
            }
            try:
                from csp_screener import supabase_sync
                cloud_screens = supabase_sync.fetch_screens_map()
                # Local records win on conflict (they are the same data)
                screens_by_id = {**cloud_screens, **screens_by_id}
            except Exception as e:
                logger.debug(f"Cloud screens map skipped: {e}")
            buckets = feature_analyzer.analyze_features(closed, screens_by_id)
            recommendations = recommender.all_recommendations(
                baseline, ts, buckets, closed_trades=closed)
            logger.info(f"Learning: {len(recommendations)} recommendations generated")
        except Exception as e:
            logger.debug(f"Recommendations step skipped: {e}")

        # 8. Assemble health
        health = assemble_health(vix, earnings_data, len(ranked), earnings_canary)

        # 9. Render + send email (two-tier layout). Daily runs skip the
        # weekly digest — EXCEPT when a market-hours run stages a live
        # ticket, which is now THE acting signal (real two-sided quotes;
        # the Sunday digest is planning-only, its prices being Friday's).
        sent = False
        preview_path = None
        alert_failed = False
        # Alert dedup: a 30-45 DTE spread stays viable for DAYS — alerting on
        # 'viable' would command TAKE {ticker} every day for the same trade.
        # Only a ticket whose virtual position was NEWLY opened this run is
        # news; repeats are suppressed by the one-per-ticker dedup upstream.
        opened_tickers = {tid.split("::")[1] for tid in opened if "::" in tid}
        newly_staged_live = [c for c in live_viable if c["ticker"] in opened_tickers]
        if is_daily and newly_staged_live:
            alert_flags = {
                "live_risk_open": sum(
                    float(p.get("max_loss") or 0)
                    for p in open_positions if p.get("tier") == "live"),
                "budget_cap": account.max_total_risk(),
                "equity": account.current_equity(),
                "monthly_deposit": account.MONTHLY_DEPOSIT,
                "slots_used": sum(1 for p in open_positions if p.get("tier") == "live"),
                "slots_max": config.MAX_OPEN_POSITIONS,
                "max_risk_per_spread": account.max_risk_per_trade(),
                "live_viable_count": len(newly_staged_live),
                "no_trade_week": False,
                "opened_count": len(opened),
                "closed_count": mark_summary["closed"],
                "closed_pnl": mark_summary["closed_pnl_total"],
                "open_positions_count": len(open_positions),
                "market_open": us_market_likely_open(),
            }
            alert_candidates = [
                c for c in live_candidates
                if c.get("setup") is None or c["ticker"] in opened_tickers
            ]
            subject, html = notify.render_ticket_alert(alert_candidates, alert_flags)
            preview_path = notify.write_preview(subject, html)
            sent = notify.send_email(subject, html)
            if not sent:
                # A staged ticket the owner never hears about is the worst
                # silent failure this system has — go loudly red.
                alert_failed = True
            logger.info(f"TICKET ALERT {'sent' if sent else 'NOT SENT — failing run'}: {subject}")
        if not is_daily:
            week_label = now.strftime("Week of %Y-%m-%d")
            # Risk-budget ledger for the email: what the playbook caps allow
            # the reader to approve THIS week.
            live_open = [p for p in open_positions if p.get("tier") == "live"]
            flags = {
                "no_trade_week": no_trade_week,
                "post_spike_window": post_spike,
                "fomc_days": fomc_days,
                "eur_usd": eur_usd,
                "opened_count": len(opened),
                "closed_count": mark_summary["closed"],
                "closed_pnl": mark_summary["closed_pnl_total"],
                "open_positions_count": len(open_positions),
                "live_viable_count": len(live_viable),
                "live_risk_open": sum(float(p.get("max_loss") or 0) for p in live_open),
                "budget_cap": account.max_total_risk(),
                "equity": account.current_equity(),
                "monthly_deposit": account.MONTHLY_DEPOSIT,
                "slots_used": len(live_open),
                "slots_max": config.MAX_OPEN_POSITIONS,
                "max_risk_per_spread": account.max_risk_per_trade(),
                "market_open": us_market_likely_open(),
            }
            subject, html = notify.render_full_email(
                week_label=week_label,
                candidates=sandbox_candidates,
                summaries=summaries,
                open_positions=open_positions,
                health=health,
                recommendations=recommendations,
                live_candidates=live_candidates,
                flags=flags,
            )
            preview_path = notify.write_preview(subject, html)
            sent = notify.send_email(subject, html)
            if not sent:
                logger.warning(f"Email not sent (SMTP creds missing?). Preview at {preview_path}")

        # 10. Log the screen
        journal.append("screens", {
            "screen_id": screen_id,
            "run_type": run_type,
            "ran_at": now.isoformat(),
            "universe_size": len(tickers),
            "passed_filters": len(ranked),
            "candidates_in_email": len(candidates),
            "virtual_positions_opened": len(opened),
            "virtual_closed_this_run": mark_summary["closed"],
            "virtual_closed_pnl": mark_summary["closed_pnl_total"],
            "vix": vix,
            "email_sent": sent,
            "preview_path": preview_path,
            "summaries": {k: v.as_dict() for k, v in summaries.items()},
            "tickers_in_email": [c["ticker"] for c in candidates],
            "candidates_payload": candidates,
            "live_viable": len(live_viable),
            "no_trade_week": no_trade_week,
            "post_spike_window": post_spike,
            "fomc_days": fomc_days,
            "eur_usd_rate": eur_usd,
            "recommendations": [
                {"severity": r.severity, "category": r.category,
                 "title": r.title, "detail": r.detail,
                 "supporting_data": r.supporting_data}
                for r in recommendations
            ] if recommendations else [],
        })

        # 11. Shadow book: track this run's discards (SEPARATE journal —
        # research rows the go-live gate can never see). Deliberately LAST:
        # its chain fetches must never sit between a staged ticket and the
        # 🎯 alert email, or between the run and its screens row (the CI
        # verify gate) inside the job timeout. Never fatal.
        try:
            from csp_screener import shadow_book
            for c in live_shadow_pool + live_earnings_rejects:
                c["tier"] = "live"
            for c in sandbox_shadow_pool + sandbox_earnings_rejects:
                c["tier"] = "sandbox"
            shadow_summary = shadow_book.record_run_shadows(
                screen_id=screen_id,
                discarded=live_shadow_pool + sandbox_shadow_pool,
                earnings_blocked=live_earnings_rejects + sandbox_earnings_rejects,
                production_open_tickers={
                    t.ticker for t in virtual_tracker.get_open_virtual_trades()},
                production_cooldown_tickers=virtual_tracker.recently_closed_tickers(
                    REOPEN_COOLDOWN_DAYS),
                vix=vix,
            )
            journal.append("system_events", {
                "event": "shadow_run",
                "at": datetime.now().isoformat(),
                "screen_id": screen_id,
                "opened": len(shadow_summary["opened"]),
                "skipped": shadow_summary["skipped"],
                "no_setup": shadow_summary["no_setup"],
                "not_spanning": shadow_summary.get("not_spanning", 0),
                "dropped_by_cap": shadow_summary.get("dropped_by_cap", 0),
                "inert": shadow_summary.get("inert", False),
                "marked": (mark_summary.get("shadow") or {}).get("updated", 0),
                "closed": (mark_summary.get("shadow") or {}).get("closed", 0),
                "closed_pnl": (mark_summary.get("shadow") or {}).get(
                    "closed_pnl_total", 0.0),
            })
        except Exception as e:
            logger.warning(f"Shadow book step skipped (non-fatal): {e}")

        # 12. Deadman success ping
        ping_msg = (
            f"OK candidates={len(candidates)} opened={len(opened)} "
            f"closed={mark_summary['closed']} "
            f"deferred={mark_summary.get('deferred', 0)} VIX={vix or 'na'}"
        )
        deadman.ping_success(ping_msg)
        logger.info(f"Run complete: {ping_msg}")
        if alert_failed:
            logger.error(
                "A live ticket STAGED but the alert email FAILED to send — "
                "returning non-zero so the workflow goes red. The ticket "
                "content is in the journal and the uploaded email preview."
            )
            return 1
        return 0
    except Exception as e:
        logger.exception(f"Screener run failed: {e}")
        deadman.ping_failure(f"Screener crashed: {e}")
        journal.append("system_events", {
            "event": "screener_failure",
            "error": str(e),
            "at": datetime.now().isoformat(),
        })
        # Best-effort failure email — the deadman switch fires too, but the
        # traceback in your inbox is what lets you fix it without digging.
        try:
            import traceback
            from html import escape as _esc
            notify.send_email(
                f"[CSP Screener] RUN FAILED {now.strftime('%Y-%m-%d')}",
                f"<html><body style='font-family:sans-serif'>"
                f"<h2 style='color:#cf222e'>Screener crashed</h2>"
                f"<pre style='background:#f6f8fa;padding:12px;border-radius:6px;"
                f"overflow-x:auto'>{_esc(traceback.format_exc())}</pre>"
                f"</body></html>",
            )
        except Exception:
            pass
        return 1


def kill_email(now: datetime, vix: float) -> None:
    """Send a 'no trades this week' email when VIX kill switch hits."""
    subject = f"[CSP Screener] VIX kill switch — no candidates"
    html = f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:auto;">
      <h2>VIX kill switch triggered</h2>
      <p>Current VIX is <b>{vix:.1f}</b>, above kill-switch threshold of
      <b>{config.VIX_KILL_SWITCH}</b>.</p>
      <p>Selling premium into a vol spike is the classic retail blow-up.
      Screener returned NO candidates this week. Sit on your hands.</p>
      <p>Run at {now.strftime('%Y-%m-%d %H:%M')}.</p>
    </body></html>"""
    notify.write_preview(subject, html)
    notify.send_email(subject, html)


def _try_connect_ibkr():
    """Best-effort IBKR connection. Returns None if TWS is off."""
    import os
    try:
        from ib_insync import IB
    except ImportError:
        logger.info("ib_insync not installed; skipping IBKR")
        return None
    port = int(os.environ.get("IBKR_PORT", "7497"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", "20"))  # avoid collision with main daemon
    ib = IB()
    try:
        ib.connect("127.0.0.1", port, clientId=client_id, timeout=8, readonly=True)
        ib.reqMarketDataType(3)  # delayed
        logger.info(f"Connected to IBKR on port {port}")
        return ib
    except Exception as e:
        logger.info(f"IBKR not available ({e}); using yfinance fallback for options")
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CSP Screener — weekly/daily run")
    parser.add_argument("--no-ibkr", action="store_true", help="Skip IBKR, use yfinance only")
    parser.add_argument("--force-send", action="store_true", help="Force email even if empty")
    parser.add_argument("--daily", action="store_true",
                        help="Daily indications run: full screen + virtual opens, "
                             "no email (dashboard-only)")
    args = parser.parse_args()
    exit_code = run_weekly_screen(
        force_send=args.force_send,
        use_ibkr=not args.no_ibkr,
        run_type="daily" if args.daily else "weekly",
    )
    sys.exit(exit_code)
