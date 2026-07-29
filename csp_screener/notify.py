"""
Email composition + SMTP delivery via Gmail.

Renders a clean HTML email with:
  1. This week's top candidates (with virtual setup attached)
  2. Self-evaluation summary (screener's track record)
  3. Open virtual positions and their current P&L
  4. System health indicators (data quality, missing earnings, etc.)

Requires env vars:
  SMTP_USER       — Gmail address
  SMTP_PASSWORD   — Gmail App Password (16 chars; not your normal password)
  SMTP_TO         — recipient (defaults to SMTP_USER)
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import asdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Optional

from csp_screener import config
from csp_screener.evaluator import PerformanceSummary

logger = logging.getLogger(__name__)


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'background:{color};color:#fff;font-size:11px;font-weight:600;">'
        f'{escape(text)}</span>'
    )


def _quality_badge(quality: str) -> str:
    unverified = quality.endswith("_unverified_liquidity")
    base = quality.replace("_unverified_liquidity", "")
    badge = {
        "ibkr_greeks": _badge("IBKR LIVE", "#2da44e"),
        "yfinance_iv_estimated_delta": _badge("yfinance + est", "#bf8700"),
        "premium_only_no_greeks": _badge("PREMIUM ONLY", "#cf222e"),
    }.get(base, _badge(base, "#6e7781"))
    if unverified:
        badge += " " + _badge("LIQ?", "#cf222e")
    return badge


def _pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "#2da44e"
    if pnl < 0:
        return "#cf222e"
    return "#6e7781"


def _fmt_expiry(iso_date: str) -> tuple[str, int]:
    """('Fri, Jul 31 2026', days_from_now) from a YYYY-MM-DD string."""
    try:
        d = datetime.fromisoformat(iso_date)
        days = max(0, (d.date() - datetime.now().date()).days)
        return d.strftime("%a, %b %d %Y"), days
    except (ValueError, TypeError):
        return iso_date, 0


def _odds_phrase(p: float) -> tuple[str, str]:
    """('9 times in 10', '1 time in 10') phrasing for a win probability."""
    best_d, best_k, best_err = 10, round(p * 10), 999.0
    for d in (4, 5, 10, 20):
        k = round(p * d)
        err = abs(p - k / d)
        if 0 < k < d and err < best_err:
            best_d, best_k, best_err = d, k, err
    lose = best_d - best_k
    return (
        f"{best_k} time{'s' if best_k != 1 else ''} in {best_d}",
        f"{lose} time{'s' if lose != 1 else ''} in {best_d}",
    )


def _plain_action_html(
    setup: dict,
    ticker: str,
    last_price: float,
    tier: str = "sandbox",
    max_risk_cap: Optional[float] = None,
) -> str:
    """
    The 'what exactly to trade, in one glance' block. Pure translation of
    the setup's existing numbers into sentences — no new math except
    display-only ratios and the odds read off the delta.
    """
    pretty, days = _fmt_expiry(setup["expiration"])
    is_spread = (setup.get("structure") == "put_credit_spread"
                 and setup.get("long_strike") is not None)
    max_loss = setup["max_loss_per_contract"]
    strike = setup["strike"]
    is_paper = tier != "live"
    # Lead with what the ATTACHED exit plan delivers, not the
    # expire-worthless number — this line sits directly above "buy back at
    # 50% of the credit", so the two must agree.
    from csp_screener.setup_generator import net_at_tp_exit
    gross_credit = setup["estimated_credit_per_contract"]
    credit = net_at_tp_exit(
        gross_credit, "put_credit_spread" if is_spread else "csp")
    hold_to_expiry = setup.get("net_credit_after_friction") or gross_credit

    if is_spread:
        headline = (
            f"SELL the ${strike:.2f} put &nbsp;+&nbsp; "
            f"BUY the ${setup['long_strike']:.2f} put"
        )
        cap_note = ""
        if max_risk_cap:
            cap_note = f" ({max_loss / max_risk_cap:.0%} of your ${max_risk_cap:.0f} per-trade cap)"
        worst = (
            f"<b style='color:#cf222e;'>Worst case &minus;${max_loss:.0f}</b> "
            f"— capped by the bought put, no matter how far it falls{cap_note}."
        )
        collect_note = " (net, after costs)"
    else:
        headline = f"SELL 1 &times; {escape(ticker)} ${strike:.2f} PUT"
        worst = (
            f"<b style='color:#cf222e;'>Worst case &minus;${max_loss:.0f}</b> "
            f"if {escape(ticker)} went to zero. "
            f"You start losing below ${setup['breakeven']:.2f}."
        )
        collect_note = ""

    # Odds: P(expires worthless) ≈ 1 - |delta| — the market's own estimate
    delta = setup.get("delta")
    if delta is not None:
        win_p = 1.0 - min(1.0, abs(float(delta)))
        keep_ph, lose_ph = _odds_phrase(win_p)
        est_note = ("; delta is estimated here — treat as rough"
                    if not str(setup.get("data_quality", "")).startswith("ibkr") else "")
        odds_html = (
            f"<b>≈ {win_p*100:.0f}% odds</b> this expires worthless "
            f"(read off the option's delta — the market's own estimate, not a "
            f"promise{est_note}). At those odds: keep ≈ ${credit:.0f} about "
            f"{keep_ph}; lose up to ${max_loss:.0f} about {lose_ph}.<br>"
        )
    else:
        odds_html = (
            "<b style='color:#bf8700;'>Odds unknown</b> — the data source gave "
            "no Greeks. Don't act on this one without checking the chain in IBKR.<br>"
        )

    if is_paper:
        box_style = "background:#f6f8fa;border:1px solid #d0d7de;"
        title = "The paper trade — research only, not for your account"
        title_color = "#6e7781"
        collect_html = (
            f"<span style='color:#57606a;font-weight:600;'>The model collects "
            f"≈ ${credit:.0f} (virtual)</span> per contract{collect_note}."
        )
        paper_note = (
            "<br><span style='color:#bf8700;font-size:12px;'>Real CSPs at this "
            "account size are a playbook hard-no until ~$10K+. This exists to "
            "build the track record.</span>"
        )
    else:
        box_style = "background:#f0f6ff;border:1px solid #a5c9ff;"
        title = "The trade"
        title_color = "#0969da"
        collect_html = (
            f"<span style='color:#2da44e;font-weight:600;'>You net ≈ "
            f"${credit:.0f}</span> per contract at the 50% take-profit this "
            f"ticket attaches{collect_note}. "
            f"<span style='color:#6e7781;'>(${gross_credit:.0f} gross credit "
            f"up front; ${hold_to_expiry:.0f} only if held to expiry, which "
            f"the plan does not do.)</span>"
        )
        paper_note = ""

    return f"""
    <div style="margin-top:8px;padding:12px 14px;{box_style}border-radius:8px;">
      <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:{title_color};
                  text-transform:uppercase;margin-bottom:4px;">{title}</div>
      <div style="font-size:15px;font-weight:700;font-family:monospace;">
        {headline}
        <span style="font-weight:400;font-family:sans-serif;color:#57606a;font-size:13px;">
          — expires <b>{pretty}</b> ({days} days from now)
        </span>
      </div>
      <div style="font-size:13px;margin-top:8px;line-height:1.7;">
        {collect_html}<br>
        {odds_html}
        <b>You win</b> if {escape(ticker)} stays above <b>${strike:.2f}</b> through {pretty}
        — the strike is {setup['pct_otm']*100:.0f}% below today's ${last_price:.2f}.<br>
        {worst}<br>
        <span style="color:#57606a;">Exit plan: buy back at 50% of the credit,
        or close when 21 days remain — whichever comes first.</span>{paper_note}
      </div>
    </div>"""


def render_candidates_section(candidates: list[dict]) -> str:
    """candidates is a list of dicts each with merged ranked + setup data."""
    if not candidates:
        return (
            "<div style='padding:16px;background:#fff8c5;border:1px solid #d4a72c;"
            "border-radius:6px;'><b>No candidates this week.</b> All names failed "
            "filters (price band / volume / earnings / VIX kill switch).</div>"
        )

    cards = []
    for c in candidates:
        setup = c.get("setup")
        rv_pct = c.get("rv_percentile", 0)
        next_earn = c.get("next_earnings_days")
        earn_str = (
            f"earnings in {next_earn}d" if (next_earn is not None and next_earn >= 0)
            else "earnings date unknown"
        )
        header = f"""
        <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;">
          <span style="font-size:17px;font-weight:700;font-family:monospace;">
            {escape(c['ticker'])}
            <span style="font-weight:400;font-family:sans-serif;font-size:12px;color:#6e7781;">
              ${c['last_price']:.2f} · vol rank {rv_pct:.0f}/100 · {earn_str}
            </span>
          </span>
          {_quality_badge(setup.get('data_quality', 'unknown')) if setup else ''}
        </div>"""

        if not setup:
            body = (
                "<div style='margin-top:6px;font-size:13px;color:#6e7781;font-style:italic;'>"
                "No liquid put found in the expiry window. Underlying still ranked — "
                "check the chain manually in IBKR.</div>"
            )
        else:
            credit = setup["estimated_credit_per_contract"]
            max_loss = setup["max_loss_per_contract"]
            roc = (credit / max_loss * 100) if max_loss > 0 else 0
            annualized = roc * (365 / max(1, setup["dte"]))
            delta_str = f"{setup['delta']:+.2f}" if setup.get("delta") is not None else "?"
            iv_str = f"{setup['iv']*100:.0f}%" if setup.get("iv") is not None else "?"
            body = _plain_action_html(setup, c["ticker"], c["last_price"]) + f"""
            <div style="margin-top:6px;font-size:11px;color:#6e7781;">
              For the detail-inclined: {roc:.1f}% return on risk ({annualized:.0f}% annualized)
              · Δ {delta_str} · IV {iv_str} · open interest {setup.get('open_interest', '?')}
            </div>"""

        cards.append(f"""
        <div style="border:1px solid #d0d7de;border-radius:8px;padding:14px;margin:10px 0;">
          {header}
          {body}
        </div>""")

    return "".join(cards)


def render_performance_section(summaries: dict[str, PerformanceSummary]) -> str:
    """Render the virtual track record."""
    cards = []
    for key in ["30d", "90d", "all"]:
        s = summaries.get(key)
        if not s:
            continue
        if s.closed_count == 0:
            body = (
                f"<div style='color:#6e7781;font-style:italic;'>"
                f"No closed virtual trades yet in this period.</div>"
            )
        else:
            pnl_color = _pnl_color(s.total_pnl)
            pess = getattr(s, "total_pnl_pessimistic", s.total_pnl)
            body = f"""
            <div style="font-size:22px;font-weight:600;color:{pnl_color};">
              ${pess:+.2f} <span style="font-size:14px;color:#6e7781;">to</span> ${s.total_pnl:+.2f}
            </div>
            <div style="font-size:10px;color:#6e7781;">
              pessimistic ↔ base fills — the truth is inside this band
            </div>
            <div style="margin-top:6px;font-size:13px;">
              <b>{s.closed_count}</b> trades • <b>{s.win_rate*100:.0f}%</b> win
              <br>Avg win ${s.avg_win:+.2f} vs loss ${s.avg_loss:+.2f}
              <br>Best ${s.best_trade:+.2f} • Worst ${s.worst_trade:+.2f}
            </div>"""
        cards.append(f"""
        <div style="flex:1;padding:14px;background:#f6f8fa;border-radius:6px;
                    border:1px solid #d0d7de;margin:0 4px;">
          <div style="font-size:11px;color:#6e7781;font-weight:600;letter-spacing:0.5px;
                      text-transform:uppercase;margin-bottom:8px;">
            {escape(s.period_label)}
          </div>
          {body}
        </div>""")

    return f"""
    <div style="display:flex;margin:0 -4px;font-family:sans-serif;">
      {''.join(cards)}
    </div>"""


def render_open_positions_section(open_positions_data: list[dict]) -> str:
    """Render open virtual positions with current P&L."""
    if not open_positions_data:
        return (
            "<div style='padding:12px;color:#6e7781;font-style:italic;'>"
            "No open virtual positions.</div>"
        )
    rows = []
    for p in open_positions_data:
        pnl_color = _pnl_color(p.get("pnl_now", 0))
        row = f"""
        <tr style="border-bottom:1px solid #d0d7de;">
          <td style="padding:8px;font-weight:600;">{escape(p['ticker'])}</td>
          <td style="padding:8px;">${p['strike']:.2f}P {p['expiration']}</td>
          <td style="padding:8px;text-align:right;">{p['dte_remaining']}</td>
          <td style="padding:8px;text-align:right;">${p['credit_received']:.2f}</td>
          <td style="padding:8px;text-align:right;color:{pnl_color};font-weight:600;">
            ${p.get('pnl_now', 0):+.2f}
          </td>
          <td style="padding:8px;text-align:right;color:{pnl_color};">
            {p.get('pnl_pct_now', 0)*100:+.1f}%
          </td>
        </tr>
        """
        rows.append(row)

    return f"""
    <table style="width:100%;border-collapse:collapse;font-family:sans-serif;font-size:13px;">
      <thead>
        <tr style="background:#f6f8fa;text-align:left;">
          <th style="padding:8px;">Ticker</th>
          <th style="padding:8px;">Contract</th>
          <th style="padding:8px;text-align:right;">DTE</th>
          <th style="padding:8px;text-align:right;">Credit</th>
          <th style="padding:8px;text-align:right;">P&L</th>
          <th style="padding:8px;text-align:right;">% of credit</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def render_insights_section(recommendations: list) -> str:
    """Render the learning-layer insights section."""
    if not recommendations:
        return ""
    badge_colors = {"warn": "#bf8700", "alert": "#cf222e", "info": "#0969da"}
    rows = []
    for r in recommendations[:8]:  # cap at 8 to keep email scannable
        color = badge_colors.get(r.severity, "#6e7781")
        rows.append(f"""
        <div style="border-left:3px solid {color};padding:8px 12px;margin:8px 0;
                    background:#f6f8fa;">
          <div style="font-weight:600;font-size:13px;color:{color};text-transform:uppercase;
                      letter-spacing:0.5px;margin-bottom:4px;">
            {escape(r.severity)} · {escape(r.category)}
          </div>
          <div style="font-weight:600;font-size:14px;">{escape(r.title)}</div>
          <div style="font-size:13px;color:#6e7781;margin-top:4px;">{escape(r.detail)}</div>
        </div>""")
    return "".join(rows)


def _render_near_misses(live_candidates: list[dict]) -> str:
    """The near-miss ledger — WHY each live candidate voided, with numbers."""
    rows = []
    for c in live_candidates:
        reasons = c.get("void_reasons") or []
        if c.get("setup") or not reasons:
            continue
        near = next((r for r in reasons if "NEAR MISS" in r), reasons[0])
        rows.append(
            f"<div style='font-size:12px;color:#57606a;padding:3px 0;'>"
            f"<b>{escape(c['ticker'])}</b>: {escape(near)}</div>"
        )
    if not rows:
        return ""
    return (
        "<div style='margin-top:10px;padding:10px 12px;background:#f6f8fa;"
        "border-radius:6px;'>"
        "<div style='font-size:11px;font-weight:700;color:#57606a;"
        "text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;'>"
        "Why nothing qualified (near-miss ledger)</div>"
        + "".join(rows) +
        "</div>"
    )


def render_live_section(
    live_candidates: list[dict],
    no_trade_week: bool,
    flags: Optional[dict] = None,
) -> str:
    """LIVE tier: put credit spreads with staged order tickets."""
    flags = flags or {}
    if no_trade_week or not any(c.get("setup") for c in live_candidates):
        # Honest labeling: 'no trade' is a market verdict only when the
        # market could actually speak. Outside market hours yfinance zeroes
        # every bid/ask, so nothing can EVER stage — say so instead of
        # implying the gates rejected real prices.
        if flags.get("market_open") is False:
            banner = (
                "<div style='padding:16px;background:#f6f8fa;border:1px solid #d0d7de;"
                "border-radius:6px;font-weight:600;'>"
                "⏸ QUOTES UNAVAILABLE (market closed) — live tickets can only "
                "stage during US market hours, when real two-sided quotes exist. "
                "The weekday market-hours run is the acting signal; this digest "
                "is planning-only.</div>"
            )
        else:
            banner = (
                "<div style='padding:16px;background:#ddf4ff;border:1px solid #54aeff;"
                "border-radius:6px;font-weight:600;'>"
                "🚫 NO TRADE — no live-tier spread passed the gates "
                "(net credit ≥ $25 after friction, friction ≤ 20% of credit, "
                "live quotes, tight spreads). Sitting out is the designed outcome, "
                "not a failure.</div>"
            )
        return banner + _render_near_misses(live_candidates)
    blocks = []
    viable_seen = 0
    for c in live_candidates:
        s = c.get("setup")
        if not s:
            reason = c.get("skip_reason", "no viable spread")
            blocks.append(
                f"<div style='padding:8px 12px;color:#6e7781;font-size:13px;'>"
                f"{escape(c['ticker'])}: <i>{escape(reason)}</i></div>"
            )
            continue
        viable_seen += 1
        if viable_seen == 1:
            pick_badge = _badge("★ BEST PICK", "#bf8700")
            backup_note = ""
        else:
            pick_badge = _badge(f"BACKUP #{viable_seen}", "#6e7781")
            backup_note = (
                "<div style='font-size:12px;color:#6e7781;margin-top:4px;'>"
                "Take only if the best pick won't fill at its limit — never "
                "both; the budget allows one.</div>"
            )
        ticket = escape(s.get("ticket") or "")
        blocks.append(f"""
        <div style="border:2px solid #2da44e;border-radius:8px;padding:14px;margin:10px 0;">
          <div style="display:flex;justify-content:space-between;align-items:baseline;">
            <span style="font-size:16px;font-weight:700;">{escape(c['ticker'])}
              <span style="font-weight:400;color:#6e7781;font-size:12px;">
                ${c['last_price']:.2f} · vol rank {c.get('rv_percentile', 0):.0f}/100
              </span>
            </span>
            <span>{pick_badge} {_badge('PUT CREDIT SPREAD', '#2da44e')} {_quality_badge(s.get('data_quality', ''))}</span>
          </div>
          {backup_note}
          {_plain_action_html(s, c['ticker'], c['last_price'], tier='live',
                              max_risk_cap=flags.get('max_risk_per_spread'))}
          <div style="font-size:12px;margin-top:10px;padding:8px 10px;background:#fff8c5;
                      border-radius:6px;">
            <b>WHEN:</b> work it within ~90 minutes of this email — never inside the
            first or last 30 minutes of the US session (spreads are widest there).
            Check the live mid in IBKR first: if it's dropped more than a couple of
            cents below the staged credit, the edge is gone. Chase at most 1 tick;
            walking away unfilled is the plan working, not a miss. The GTC exits go
            in with the same order.
          </div>
          <div style="font-size:11px;color:#6e7781;margin-top:8px;">
            The exact staged order (approve or reject in IBKR — never retype it):
          </div>
          <pre style="background:#f6f8fa;border-radius:6px;padding:10px;margin:4px 0;
                      font-size:12px;line-height:1.6;overflow-x:auto;">{ticket}</pre>
          <div style="font-size:11px;color:#6e7781;">
            Approve or reject — never modify by hand. A typed order is a tripwire event.
          </div>
        </div>""")
    return "".join(blocks)


def render_bottom_line(flags: dict) -> str:
    """
    One box at the very top that answers 'what do I need to know / do?'
    before any table. Everything below it is supporting detail.
    """
    n_live = flags.get("live_viable_count", 0)
    opened = flags.get("opened_count", 0)
    closed = flags.get("closed_count", 0)
    closed_pnl = flags.get("closed_pnl", 0.0)
    n_open = flags.get("open_positions_count", 0)

    if flags.get("no_trade_week"):
        if flags.get("market_open") is False:
            # Honest labeling: with markets closed nothing COULD stage —
            # this is not a market verdict, and saying so keeps the reader
            # trusting the box on days when it IS a verdict.
            headline = "🗓 Planning digest — markets are closed, so nothing can stage now."
            sub = ("Live tickets stage from real quotes on the weekday "
                   "market-hours run and arrive as separate 🎯 alerts. "
                   "Use this digest to see what's brewing, not to act.")
        else:
            headline = "🚫 Nothing to do — no real-money trade qualified."
            sub = ("No spread passed the safety gates. That's the system working, "
                   "not failing. Skim the paper section if curious; otherwise close this email.")
        bg, border = "#ddf4ff", "#54aeff"
    else:
        headline = f"✅ {n_live} staged ticket(s) waiting for your approve/reject in IBKR."
        sub = ("Review the green boxes below. Approve or reject only — "
               "never modify by hand.")
        bg, border = "#dafbe1", "#2da44e"

    activity = (
        f"Paper activity this run: {opened} opened, {closed} closed"
        + (f" (${closed_pnl:+.2f})" if closed else "")
        + f" · {n_open} position(s) currently open."
    )

    # Risk-budget ledger: what the playbook caps allow the reader to approve
    budget_html = ""
    if flags.get("budget_cap"):
        cap = flags["budget_cap"]
        used = flags.get("live_risk_open", 0.0)
        slots_used = flags.get("slots_used", 0)
        slots_max = flags.get("slots_max", 2)
        per_trade = flags.get("max_risk_per_spread", 130)
        slots_free = max(0, slots_max - slots_used)
        budget_free = max(0.0, cap - used)
        max_approvals = min(slots_free, int(budget_free // per_trade)) if per_trade else 0
        budget_html = f"""
      <div style="font-size:13px;margin-top:10px;padding:8px 12px;background:#ffffffaa;
                  border-radius:6px;">
        <b>RISK BUDGET</b> — ${used:.0f} of ${cap:.0f} in use · {slots_used} of
        {slots_max} position slots filled.
        {'This week you can approve AT MOST ' + str(max_approvals) + ' ticket(s) (~$' + format(per_trade, '.0f') + ' risk each). Approving more breaks the playbook cap.'
         if n_live > 0 else 'Nothing new to approve this week.'}
      </div>"""

    return f"""
    <div style="padding:16px;margin:16px 0;background:{bg};border:2px solid {border};
                border-radius:8px;">
      <div style="font-size:16px;font-weight:700;">{headline}</div>
      <div style="font-size:13px;color:#57606a;margin-top:6px;">{sub}</div>
      {budget_html}
      <div style="font-size:12px;color:#6e7781;margin-top:8px;">{activity}</div>
    </div>"""


def render_quality_legend() -> str:
    """Plain-English key for the data-quality badges."""
    return f"""
    <div style="font-size:11px;color:#6e7781;margin-top:8px;line-height:1.8;">
      <b>Data badges:</b>
      {_badge('IBKR LIVE', '#2da44e')} live broker quotes (trustworthy) ·
      {_badge('yfinance + est', '#bf8700')} free delayed data, Greeks estimated (directional only) ·
      {_badge('PREMIUM ONLY', '#cf222e')} price known but no Greeks ·
      {_badge('LIQ?', '#cf222e')} liquidity NOT verified — always check the chain in IBKR first.
    </div>"""


def render_ticket_alert(live_candidates: list[dict], flags: dict) -> tuple[str, str]:
    from csp_screener.setup_generator import net_at_tp_exit
    """
    Compact intraday alert — sent ONLY when a market-hours run stages a
    live ticket from real two-sided quotes. This is THE acting signal;
    the Sunday digest is planning-only.
    """
    viable = [c for c in live_candidates if c.get("setup")]
    best = viable[0]
    s = best["setup"]
    credit = net_at_tp_exit(
        s["estimated_credit_per_contract"],
        s.get("structure") or "csp")  # what the attached exit plan delivers
    others = f", {len(viable) - 1} backup(s)" if len(viable) > 1 else ""
    subject = (
        f"[CSP Screener] 🎯 TICKET STAGED — TAKE {best['ticker']} "
        f"(${credit:.0f} credit, ${s['max_loss_per_contract']:.0f} risk){others}"
    )
    bottom = render_bottom_line(dict(flags, live_viable_count=len(viable)))
    live_html = render_live_section(live_candidates, False, flags)
    html = f"""
    <html><body style="font-family:sans-serif;max-width:900px;margin:auto;color:#1f2328;
                       background:#fff;padding:20px;">
      <h1 style="margin-bottom:0;font-size:20px;">🎯 Live ticket staged — from real quotes</h1>
      <p style="color:#6e7781;margin-top:4px;font-size:13px;">
        Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC during US market
        hours. Unlike the Sunday digest, these prices are live right now.
      </p>
      {bottom}
      {live_html}
      <p style="font-size:11px;color:#6e7781;margin-top:16px;">
        Reminder: approve or reject only — never retype. If in doubt, rejecting
        is always a valid answer.
      </p>
    </body></html>"""
    return subject, html


def render_full_email(
    week_label: str,
    candidates: list[dict],
    summaries: dict[str, PerformanceSummary],
    open_positions: list[dict],
    health: dict,
    recommendations: list | None = None,
    live_candidates: list[dict] | None = None,
    flags: dict | None = None,
) -> tuple[str, str]:
    """Return (subject, html_body). Two-tier layout: LIVE on top, sandbox below."""
    from csp_screener.setup_generator import net_at_tp_exit
    flags = flags or {}
    live_candidates = live_candidates or []
    viable = [c for c in live_candidates if c.get("setup")]
    n_live = len(viable)
    if flags.get("no_trade_week") or n_live == 0:
        if flags.get("market_open") is False:
            subject = f"[CSP Screener] {week_label} — planning digest (tickets stage weekdays)"
        else:
            subject = f"[CSP Screener] {week_label} — NOTHING TO DO (no trade qualified)"
    else:
        best = viable[0]
        s = best["setup"]
        credit = net_at_tp_exit(
        s["estimated_credit_per_contract"],
        s.get("structure") or "csp")  # what the attached exit plan delivers
        others = f", {n_live - 1} backup(s)" if n_live > 1 else ""
        subject = (
            f"[CSP Screener] {week_label} — TAKE {best['ticker']} "
            f"(${credit:.0f} credit, ${s['max_loss_per_contract']:.0f} risk){others}"
        )

    health_html = ""
    if health.get("warnings"):
        warns = "<br>".join(escape(w) for w in health["warnings"])
        health_html = (
            f"<div style='padding:10px;margin:16px 0;background:#fff8c5;"
            f"border:1px solid #d4a72c;border-radius:6px;'>"
            f"<b>System notes:</b><br>{warns}</div>"
        )

    # Regime banners
    banners = ""
    if flags.get("post_spike_window"):
        banners += (
            "<div style='padding:12px;margin:12px 0;background:#dafbe1;"
            "border:2px solid #2da44e;border-radius:6px;font-weight:600;'>"
            "⚡ POST-SPIKE WINDOW: VIX spiked above the kill switch and has "
            "recovered — historically the richest premium-selling regime. "
            "The pre-staged vol-spike playbook applies.</div>"
        )
    fomc_days = flags.get("fomc_days")
    if fomc_days is not None and fomc_days <= 5:
        banners += (
            f"<div style='padding:12px;margin:12px 0;background:#fff8c5;"
            f"border:1px solid #d4a72c;border-radius:6px;'>"
            f"🏦 FOMC decision in {fomc_days} day(s). Paper-only condor module "
            f"applies; no new live entries the day before/after.</div>"
        )
    if flags.get("eur_usd"):
        banners += (
            f"<div style='padding:8px 12px;margin:8px 0;font-size:12px;color:#6e7781;'>"
            f"EURUSD {flags['eur_usd']:.4f} — P&amp;L tracked in both currencies; "
            f"the scoreboard that matters is EUR.</div>"
        )

    live_html = render_live_section(live_candidates, flags.get("no_trade_week", False), flags)
    candidates_html = render_candidates_section(candidates)
    perf_html = render_performance_section(summaries)
    open_html = render_open_positions_section(open_positions)
    insights_html = render_insights_section(recommendations or [])
    insights_section = ""
    if insights_html:
        insights_section = f"""
      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        System insights (self-learning layer)
      </h2>
      {insights_html}
      <p style="font-size:12px;color:#6e7781;margin-top:8px;">
        These insights come from analyzing your virtual track record. They are
        NEVER applied automatically — surface only. Decide deliberately and
        respect the 14-day cooldown if changing thresholds.
      </p>"""

    bottom_line = render_bottom_line(flags)
    legend_html = render_quality_legend()

    html_body = f"""
    <html><body style="font-family:sans-serif;max-width:900px;margin:auto;color:#1f2328;
                       background:#fff;padding:20px;">

      <h1 style="margin-bottom:0;font-size:22px;">CSP Screener — {escape(week_label)}</h1>
      <p style="color:#6e7781;margin-top:4px;font-size:13px;">
        Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} • <b>Planning digest</b> —
        prices here are the last close, not live. Actionable tickets arrive as
        separate 🎯 alerts from the weekday market-hours run, staged from real
        quotes.
      </p>

      {bottom_line}
      {health_html}
      {banners}

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #2da44e;padding-bottom:6px;">
        1 · Real-money candidates (staged tickets)
      </h2>
      <p style="font-size:12px;color:#6e7781;margin:8px 0;">
        Liquid $20-60 names as defined-risk put credit spreads. Only shown when
        every gate passed: net credit ≥ $25 after friction, friction ≤ 20% of
        credit, live quotes, tight spreads. The machine stages the ticket —
        your only move is approve or reject in IBKR.
      </p>
      {live_html}

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        2 · Paper-only research (no real money)
      </h2>
      <p style="font-size:12px;color:#6e7781;margin:8px 0;">
        The $5-25 research universe. These open as virtual positions to build the
        track record — they are NOT trade suggestions. If one ever tempts you:
        verify IV rank on <a href="https://www.barchart.com" style="color:#0969da;">barchart.com</a>,
        check the chain in IBKR, write the exit plan first (50% TP / 21 DTE / -2x credit SL).
      </p>
      {candidates_html}
      {legend_html}

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        3 · How the screener is doing (paper record)
      </h2>
      <p style="font-size:12px;color:#6e7781;margin:8px 0;">
        "What if I had taken every suggestion?" — P&amp;L shown NET of
        ${config.COMMISSION_PER_CONTRACT:.2f}/contract commission each way plus
        {config.SLIPPAGE_PCT_OF_PREMIUM:.0%} slippage each way. Profit factor
        above 1.0 = making money after friction. Under ~30 closed trades this
        is noise, not signal.
      </p>
      {perf_html}

      {insights_section}

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        4 · Open paper positions
      </h2>
      {open_html}

      <hr style="margin-top:30px;border:none;border-top:1px solid #d0d7de;">
      <p style="font-size:11px;color:#6e7781;">
        Weekly digest from csp_screener. Daily indications live on the dashboard's
        Daily tab. Hard rules locked in config.py — edit deliberately.
      </p>
    </body></html>
    """

    return subject, html_body


def send_email(subject: str, html_body: str, to_address: Optional[str] = None) -> bool:
    """
    Send HTML email. Tries providers in order:
      1. Resend (if RESEND_API_KEY is set) — simplest, one API key
      2. Gmail SMTP (if SMTP_USER + SMTP_PASSWORD are set)
      3. Neither configured → log and return False (dashboard-only mode;
         the run itself always continues)
    """
    if os.environ.get("RESEND_API_KEY", "").strip():
        return _send_via_resend(subject, html_body, to_address)

    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not smtp_user or not smtp_password:
        logger.warning(
            "No email provider configured (RESEND_API_KEY or SMTP_USER/"
            "SMTP_PASSWORD). Running in dashboard-only mode — email skipped."
        )
        return False
    to_addr = to_address or os.environ.get("SMTP_TO", smtp_user).strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.sendmail(smtp_user, [to_addr], msg.as_string())
        logger.info(f"Email sent to {to_addr}")
        return True
    except Exception as e:
        logger.error(f"SMTP send failed: {e}")
        return False


def _send_via_resend(subject: str, html_body: str, to_address: Optional[str] = None) -> bool:
    """
    Send via Resend's HTTP API (https://resend.com).

    Env:
      RESEND_API_KEY  — required
      RESEND_FROM     — optional; defaults to onboarding@resend.dev, which
                        works on the free tier WITHOUT a verified domain
      RESEND_TO       — recipient; falls back to SMTP_TO then SMTP_USER.
                        NOTE: on the free tier without a verified domain,
                        Resend only delivers to the account owner's email.
    """
    import requests

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("RESEND_FROM", "").strip() or "CSP Screener <onboarding@resend.dev>"
    to_addr = (
        to_address
        or os.environ.get("RESEND_TO", "").strip()
        or os.environ.get("SMTP_TO", "").strip()
        or os.environ.get("SMTP_USER", "").strip()
    )
    if not to_addr:
        logger.error("RESEND_API_KEY set but no recipient (set RESEND_TO).")
        return False

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_addr,
                "to": [to_addr],
                "subject": subject,
                "html": html_body,
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            logger.info(f"Email sent via Resend to {to_addr}")
            return True
        logger.error(f"Resend API error {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        logger.error(f"Resend send failed: {e}")
        return False


def write_preview(subject: str, html_body: str) -> str:
    """
    Always write the email preview to disk so you can inspect it even if SMTP fails.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.OUTPUT_DIR / f"email_preview_{ts}.html"
    out.write_text(html_body, encoding="utf-8")
    logger.info(f"Email preview written to {out}")
    return str(out)
