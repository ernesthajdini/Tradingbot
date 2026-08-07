"""
Runs Monday-Friday after market close to update virtual positions.

This is the engine that gives the screener a TRUE performance record:
every weekday the open virtual positions get marked to current spot, and any
that hit exit rules get closed in the journal.

Quiet by default — only emits an email if a virtual position closed today.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csp_screener import (
    config,
    data_pipeline,
    deadman,
    evaluator,
    journal,
    notify,
    virtual_tracker,
)


def _setup_logging():
    log_file = config.LOGS_DIR / f"daily_check_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


logger = logging.getLogger("csp_screener.daily")


def run() -> int:
    _setup_logging()
    logger.info(f"Daily check starting at {datetime.now()}")

    try:
        # Hydrate from Supabase first — on GitHub Actions the local journal
        # starts empty, and open positions only exist in the cloud.
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

        # Get list of unique tickers in open virtual positions — plus the
        # shadow book's tickers, which mark on the same pass. A shadow-book
        # failure must never stop production marking.
        open_trades = virtual_tracker.get_open_virtual_trades()
        shadow_tickers: set = set()
        try:
            from csp_screener import shadow_book
            shadow_tickers = {t.ticker for t in shadow_book.get_open_shadow_trades()}
        except Exception as e:
            logger.warning(f"Shadow book unavailable this run (non-fatal): {e}")
        if not open_trades and not shadow_tickers:
            logger.info("No open virtual positions; nothing to do")
            deadman.ping_success("daily: no open positions")
            return 0

        tickers = sorted({t.ticker for t in open_trades} | shadow_tickers)
        logger.info(f"Updating {len(open_trades)} positions "
                    f"(+{len(shadow_tickers)} shadow tickers) across "
                    f"{len(tickers)} tickers")

        # Fetch prices for those tickers
        price_data = data_pipeline.fetch_prices(tickers, lookback_days=60)

        def spot_resolver(ticker):
            df = price_data.get(ticker)
            return data_pipeline.last_price(df) if df is not None else None

        def iv_resolver(ticker):
            df = price_data.get(ticker)
            if df is None:
                return None
            return data_pipeline.recent_realized_vol(df, window=30) or 0.30

        eur_usd = data_pipeline.get_eurusd_rate() if config.TRACK_EUR else None
        from csp_screener.main import us_market_likely_open
        quote_resolver = (virtual_tracker.market_quote_resolver
                          if us_market_likely_open() else None)
        summary = virtual_tracker.update_all_open_positions(
            spot_resolver, iv_resolver, eur_usd_rate=eur_usd,
            quote_resolver=quote_resolver)
        logger.info(
            f"Daily update: {summary['updated']} marked, "
            f"{summary['closed']} closed (PnL ${summary['closed_pnl_total']:+.2f})"
        )

        # Shadow book marks AFTER production — free-rides the warm quote
        # cache, spends its own capped fetch budget, never fatal.
        shadow_summary = None
        try:
            from csp_screener import shadow_book
            shadow_summary = shadow_book.mark_open_shadows(
                spot_resolver, iv_resolver, eur_usd_rate=eur_usd,
                market_open=us_market_likely_open())
        except Exception as e:
            logger.warning(f"Shadow marking skipped (non-fatal): {e}")

        # Log the daily run
        journal.append("system_events", {
            "event": "daily_check",
            "at": datetime.now().isoformat(),
            "open_at_start": len(open_trades),
            "updated": summary["updated"],
            "closed": summary["closed"],
            "closed_pnl": summary["closed_pnl_total"],
            "details": summary["details"],
            "shadow": ({
                "updated": shadow_summary["updated"],
                "closed": shadow_summary["closed"],
                "closed_pnl": shadow_summary["closed_pnl_total"],
                "quotes_spent": shadow_summary["quotes_spent"],
            } if shadow_summary else None),
        })

        # If something closed today, send a brief notification
        if summary["closed"] > 0:
            send_close_notification(summary, price_data)

        deadman.ping_success(
            f"daily marked={summary['updated']} closed={summary['closed']}"
        )
        return 0
    except Exception as e:
        logger.exception(f"Daily check failed: {e}")
        deadman.ping_failure(f"Daily check crashed: {e}")
        return 1


def send_close_notification(summary: dict, price_data: dict) -> None:
    """Send a brief email when virtual positions close mid-week."""
    closed = [d for d in summary["details"] if d.get("action") == "closed"]
    if not closed:
        return

    rows = []
    for c in closed:
        pnl = c.get("pnl", 0)
        color = "#2da44e" if pnl > 0 else "#cf222e"
        reason = c.get("reason", "?")
        rows.append(f"""
        <tr style="border-bottom:1px solid #d0d7de;">
          <td style="padding:8px;"><code>{c['trade_id']}</code></td>
          <td style="padding:8px;color:{color};font-weight:600;">${pnl:+.2f}</td>
          <td style="padding:8px;">{reason}</td>
        </tr>""")

    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"[CSP Screener] {len(closed)} virtual position(s) closed {today}"
    html = f"""
    <html><body style="font-family:sans-serif;max-width:700px;margin:auto;">
      <h2>Virtual positions closed today</h2>
      <p>The screener's virtual portfolio had {len(closed)} closure(s).
      This is paper P&amp;L only — no real trade was made.</p>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f6f8fa;text-align:left;">
            <th style="padding:8px;">Trade</th>
            <th style="padding:8px;">PnL</th>
            <th style="padding:8px;">Reason</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <p style="font-size:12px;color:#6e7781;margin-top:16px;">
        Total closed PnL today: ${summary['closed_pnl_total']:+.2f}<br>
        Full Sunday digest still arrives this weekend.
      </p>
    </body></html>"""
    notify.write_preview(subject, html)
    notify.send_email(subject, html)


if __name__ == "__main__":
    sys.exit(run())
