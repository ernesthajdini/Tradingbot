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


def render_candidates_section(candidates: list[dict]) -> str:
    """candidates is a list of dicts each with merged ranked + setup data."""
    if not candidates:
        return (
            "<div style='padding:16px;background:#fff8c5;border:1px solid #d4a72c;"
            "border-radius:6px;'><b>No candidates this week.</b> All names failed "
            "filters (price band / volume / earnings / VIX kill switch).</div>"
        )

    rows = []
    for c in candidates:
        setup = c.get("setup")
        rv_pct = c.get("rv_percentile", 0)
        next_earn = c.get("next_earnings_days")
        earn_str = (
            f"{next_earn}d" if (next_earn is not None and next_earn >= 0)
            else "unknown"
        )

        if not setup:
            row = f"""
            <tr style="border-bottom:1px solid #d0d7de;">
              <td style="padding:10px;font-weight:600;font-size:15px;">{escape(c['ticker'])}</td>
              <td style="padding:10px;color:#6e7781;font-style:italic;" colspan="6">
                No liquid put found in DTE window. Underlying still on the list — check it manually.
              </td>
            </tr>"""
            rows.append(row)
            continue

        credit = setup["estimated_credit_per_contract"]
        max_loss = setup["max_loss_per_contract"]
        breakeven = setup["breakeven"]
        roc = (credit / max_loss * 100) if max_loss > 0 else 0  # return on collateral
        annualized = roc * (365 / max(1, setup["dte"]))
        delta_str = f"{setup['delta']:+.3f}" if setup.get("delta") is not None else "?"
        iv_str = f"{setup['iv']*100:.1f}%" if setup.get("iv") is not None else "?"

        row = f"""
        <tr style="border-bottom:1px solid #d0d7de;">
          <td style="padding:10px;font-weight:600;font-size:15px;">
            {escape(c['ticker'])}
            <div style="font-size:11px;color:#6e7781;font-weight:400;margin-top:2px;">
              ${c['last_price']:.2f} | RV pct {rv_pct:.0f} | earn {earn_str}
            </div>
          </td>
          <td style="padding:10px;">
            <div><b>${setup['strike']:.2f}P</b> {setup['expiration']}</div>
            <div style="font-size:11px;color:#6e7781;">
              {setup['dte']} DTE • {setup['pct_otm']*100:.1f}% OTM • Δ {delta_str} • IV {iv_str}
            </div>
          </td>
          <td style="padding:10px;text-align:right;">
            <b>${credit:.2f}</b>
            <div style="font-size:11px;color:#6e7781;">credit</div>
          </td>
          <td style="padding:10px;text-align:right;">
            ${max_loss:.0f}
            <div style="font-size:11px;color:#6e7781;">max loss</div>
          </td>
          <td style="padding:10px;text-align:right;">
            <b>{roc:.1f}%</b>
            <div style="font-size:11px;color:#6e7781;">ROC ({annualized:.0f}% ann)</div>
          </td>
          <td style="padding:10px;text-align:right;">
            ${breakeven:.2f}
            <div style="font-size:11px;color:#6e7781;">breakeven</div>
          </td>
          <td style="padding:10px;text-align:center;">
            {_quality_badge(setup.get('data_quality', 'unknown'))}
          </td>
        </tr>
        """
        rows.append(row)

    table = f"""
    <table style="width:100%;border-collapse:collapse;font-family:sans-serif;font-size:13px;">
      <thead>
        <tr style="background:#f6f8fa;text-align:left;">
          <th style="padding:10px;">Ticker</th>
          <th style="padding:10px;">Suggested Put</th>
          <th style="padding:10px;text-align:right;">Credit</th>
          <th style="padding:10px;text-align:right;">Max Loss</th>
          <th style="padding:10px;text-align:right;">Return</th>
          <th style="padding:10px;text-align:right;">Breakeven</th>
          <th style="padding:10px;text-align:center;">Data</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """
    return table


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
            body = f"""
            <div style="font-size:24px;font-weight:600;color:{pnl_color};">
              ${s.total_pnl:+.2f}
            </div>
            <div style="margin-top:6px;font-size:13px;">
              <b>{s.closed_count}</b> trades • <b>{s.win_rate*100:.0f}%</b> win
              <br>Avg ${s.avg_pnl:+.2f}/trade • PF {s.profit_factor:.2f}
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


def render_full_email(
    week_label: str,
    candidates: list[dict],
    summaries: dict[str, PerformanceSummary],
    open_positions: list[dict],
    health: dict,
    recommendations: list | None = None,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    subject = f"[CSP Screener] {week_label} — {len(candidates)} candidates"

    health_html = ""
    if health.get("warnings"):
        warns = "<br>".join(escape(w) for w in health["warnings"])
        health_html = (
            f"<div style='padding:10px;margin:16px 0;background:#fff8c5;"
            f"border:1px solid #d4a72c;border-radius:6px;'>"
            f"<b>System notes:</b><br>{warns}</div>"
        )

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

    html_body = f"""
    <html><body style="font-family:sans-serif;max-width:900px;margin:auto;color:#1f2328;
                       background:#fff;padding:20px;">

      <h1 style="margin-bottom:0;font-size:22px;">CSP Screener — {escape(week_label)}</h1>
      <p style="color:#6e7781;margin-top:4px;font-size:13px;">
        Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} • Underlying candidates only.
        You pick the actual contract.
      </p>

      {health_html}

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        This week's candidates
      </h2>
      {candidates_html}
      <p style="font-size:12px;color:#6e7781;margin-top:8px;">
        Before placing a real trade: verify IV rank on
        <a href="https://www.barchart.com" style="color:#0969da;">barchart.com</a>,
        check chain in IBKR, set a hard exit plan (50% TP / 21 DTE / -2x credit SL).
        Read once, decide once, then journal the decision.
      </p>

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        Screener track record (virtual)
      </h2>
      {perf_html}
      <p style="font-size:12px;color:#6e7781;margin-top:8px;">
        Virtual = "what would have happened if you'd taken every weekly suggestion."
        P&amp;L is NET of ${config.COMMISSION_PER_CONTRACT:.2f}/contract commission each way
        plus {config.SLIPPAGE_PCT_OF_PREMIUM:.0%} slippage on entry and exit premium.
        PF &gt; 1.0 = positive expectancy after friction.
      </p>

      {insights_section}

      <h2 style="margin-top:30px;font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;">
        Open virtual positions
      </h2>
      {open_html}

      <hr style="margin-top:30px;border:none;border-top:1px solid #d0d7de;">
      <p style="font-size:11px;color:#6e7781;">
        Sent by csp_screener (local cron, your Windows machine).
        Hard rules locked in config.py. Edit deliberately.
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
