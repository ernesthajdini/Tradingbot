"""
Alert and notification system.
Supports: log files, email (SMTP), and desktop notifications (Windows toast).
"""

import logging
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Sends alerts via multiple channels.

    Email setup (optional):
      Set environment variables:
        ALERT_EMAIL_FROM=your_email@gmail.com
        ALERT_EMAIL_TO=your_email@gmail.com
        ALERT_EMAIL_PASSWORD=your_app_password
        ALERT_SMTP_HOST=smtp.gmail.com
        ALERT_SMTP_PORT=587

      For Gmail, use an App Password (not your regular password):
      https://myaccount.google.com/apppasswords
    """

    def __init__(self, log_dir: str | Path | None = None):
        self.log_dir = Path(log_dir) if log_dir else Path("trading_system/output/logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Email config from env vars
        self.email_from = os.environ.get("ALERT_EMAIL_FROM", "")
        self.email_to = os.environ.get("ALERT_EMAIL_TO", "")
        self.email_password = os.environ.get("ALERT_EMAIL_PASSWORD", "")
        self.smtp_host = os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("ALERT_SMTP_PORT", "587"))
        self.email_enabled = bool(self.email_from and self.email_to and self.email_password)

    def send(self, subject: str, body: str, level: str = "info"):
        """Send alert through all configured channels."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_message = f"[{timestamp}] [{level.upper()}] {subject}\n{body}"

        # Always log to file
        self._log_to_file(subject, body, level)

        # Console
        log_func = getattr(logger, level, logger.info)
        log_func(f"ALERT: {subject}")

        # Email for important alerts
        if self.email_enabled and level in ("warning", "error", "critical", "signal"):
            self._send_email(subject, body)

        # Windows desktop notification
        self._toast_notification(subject, body[:200])

    def signal_alert(self, signals: list):
        """Send alert about new trading signals."""
        if not signals:
            return

        lines = [f"Found {len(signals)} trading signal(s):\n"]
        for s in signals:
            lines.append(
                f"  {s.direction:4s} {s.ticker:6s} "
                f"conf={s.confidence:.2f} "
                f"entry=${s.entry_price:.2f} "
                f"SL=${s.stop_loss:.2f} TP=${s.target_price:.2f}"
            )
            if s.reasoning:
                lines.append(f"    Reason: {' | '.join(s.reasoning[:3])}")
            lines.append("")

        body = "\n".join(lines)
        self.send("New Trading Signals", body, level="signal")

    def execution_alert(self, results: list, dry_run: bool = True):
        """Send alert about order execution."""
        mode = "DRY RUN" if dry_run else "LIVE"
        lines = [f"[{mode}] {len(results)} orders processed:\n"]
        for r in results:
            lines.append(f"  {r['side']:4s} {r.get('shares', '?')} {r['ticker']:6s} -- {r['status']}")
        body = "\n".join(lines)
        self.send(f"Order Execution [{mode}]", body, level="signal")

    def risk_alert(self, message: str):
        """Send urgent risk management alert."""
        self.send("RISK ALERT", message, level="critical")

    def daily_summary(self, summary: dict):
        """Send daily portfolio summary."""
        lines = [
            f"Portfolio Value:  ${summary.get('equity', 0):>12,.2f}",
            f"Daily PnL:       ${summary.get('daily_pnl', 0):>12,.2f} ({summary.get('daily_pnl_pct', 0):+.2%})",
            f"Open Positions:  {summary.get('num_positions', 0)}",
            f"Open Orders:     {summary.get('num_orders', 0)}",
            f"Cash Available:  ${summary.get('cash', 0):>12,.2f}",
        ]
        if summary.get("positions"):
            lines.append("\nPositions:")
            for p in summary["positions"]:
                lines.append(
                    f"  {p['ticker']:6s} {p['shares']:>5d} shares  "
                    f"PnL: ${p['unrealized_pnl']:>8.2f} ({p['unrealized_pnl_pct']:+.2%})"
                )
        body = "\n".join(lines)
        self.send("Daily Portfolio Summary", body, level="info")

    def _log_to_file(self, subject: str, body: str, level: str):
        """Append alert to daily log file."""
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = self.log_dir / f"alerts_{today}.log"
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{timestamp}] [{level.upper()}] {subject}\n")
            f.write(f"{'='*60}\n")
            f.write(body + "\n")

    def _send_email(self, subject: str, body: str):
        """Send email notification."""
        try:
            msg = MIMEMultipart()
            msg["From"] = self.email_from
            msg["To"] = self.email_to
            msg["Subject"] = f"[Trading System] {subject}"

            # Plain text body
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_from, self.email_password)
                server.send_message(msg)

            logger.info(f"Email sent: {subject}")
        except Exception as e:
            logger.warning(f"Email failed: {e}")

    def _toast_notification(self, title: str, message: str):
        """Show Windows desktop notification."""
        try:
            from subprocess import Popen
            # Use PowerShell to show a toast notification on Windows
            ps_script = (
                f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                f"ContentType = WindowsRuntime] | Out-Null; "
                f"$template = [Windows.UI.Notifications.ToastNotificationManager]"
                f"::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                f"$textNodes = $template.GetElementsByTagName('text'); "
                f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{title[:50]}')) | Out-Null; "
                f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{message[:100]}')) | Out-Null; "
                f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
                f"[Windows.UI.Notifications.ToastNotificationManager]"
                f"::CreateToastNotifier('Trading System').Show($toast)"
            )
            Popen(["powershell", "-Command", ps_script],
                   creationflags=0x08000000)  # CREATE_NO_WINDOW
        except Exception:
            pass  # silently fail if toast not available
