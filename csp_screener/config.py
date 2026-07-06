"""
ALL configuration in one file. Git-tracked. Edit deliberately.

These thresholds were chosen from research + adversarial review. Do not change
them based on a few weeks of results — that's the overfitting trap.

The THRESHOLD_CHANGE_COOLDOWN_DAYS rule is enforced by a pre-commit hook
(install with: scripts/install_cooldown_hook.bat). It rejects commits that
modify this file if the last commit to it was less than 14 days ago.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
JOURNAL_DIR = OUTPUT_DIR / "journal"
LOGS_DIR = OUTPUT_DIR / "logs"
CACHE_DIR = OUTPUT_DIR / "cache"

for d in (OUTPUT_DIR, JOURNAL_DIR, LOGS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Universe filter thresholds — TWO TIERS (per OPTIONS_PROFIT_PLAYBOOK.md)
# ---------------------------------------------------------------------------
# LIVE tier: liquid, penny-quoted $20-60 names traded as PUT CREDIT SPREADS.
# These are the candidates you'd actually put money on. Strict gates:
# live quotes only, min net credit after friction, tight spreads.
LIVE_PRICE_MIN: float = 20.0
LIVE_PRICE_MAX: float = 60.0

# SANDBOX tier: the original $5-25 CSP universe. Virtual-only research —
# it must independently prove itself on 100+ paper trades under the
# pessimistic friction band before any promotion. (Playbook change #5.)
PRICE_MIN: float = 5.0
PRICE_MAX: float = 25.0

# Put credit spread construction (LIVE tier)
SPREAD_WIDTH_NARROW: float = 1.0   # $1 wide for underlyings <= $30
SPREAD_WIDTH_WIDE: float = 2.0     # $2 wide above $30
MAX_RISK_PER_SPREAD: float = 130.0  # ~5% of a $2.6K account; playbook M7 sizing

# LIVE-tier viability gates (playbook change #7)
MIN_NET_CREDIT_AFTER_FRICTION: float = 25.0   # dollars, else VOID
MAX_FRICTION_PCT_OF_CREDIT: float = 0.20      # friction >20% of credit = VOID

# Liquidity floor — 1M+ shares average daily volume.
# Lower = wider option bid/ask spreads = retail-killer slippage.
MIN_DAILY_VOLUME: int = 1_000_000

# Days of price history needed for a clean realized-vol percentile.
RV_HISTORY_DAYS: int = 252

# Realized vol percentile lookback window (the "now" period to rank).
RV_WINDOW_DAYS: int = 20

# ---------------------------------------------------------------------------
# Options data filters
# ---------------------------------------------------------------------------
# Minimum open interest on the contract we'd suggest.
# 500 is the floor — below this you'll eat spread.
MIN_OPEN_INTEREST: int = 500

# Maximum bid-ask spread as % of mid. Above this = unfillable at decent price.
MAX_BID_ASK_PCT_OF_MID: float = 0.05  # 5%

# Target delta for CSPs (we suggest strikes near this delta).
TARGET_DELTA: float = 0.30

# Days-to-expiration window for CSP suggestions.
DTE_MIN: int = 25
DTE_MAX: int = 45

# ---------------------------------------------------------------------------
# Earnings exclusion (HARD)
# ---------------------------------------------------------------------------
# Never suggest a contract that would still be open at earnings.
# 14 calendar days = covers a 45 DTE window with 30-day buffer plus our
# 21 DTE exit rule.
NEVER_HOLD_THROUGH_EARNINGS: bool = True
EARNINGS_EXCLUSION_DAYS: int = 14

# ---------------------------------------------------------------------------
# Position / risk limits (would-be live limits — also applied to virtual)
# ---------------------------------------------------------------------------
MAX_OPEN_POSITIONS: int = 2
MAX_TOTAL_PREMIUM_AT_RISK: float = 200.0  # dollars

# Account-level kill switches
VIX_KILL_SWITCH: float = 35.0          # if VIX > this, screen returns empty
ACCOUNT_DRAWDOWN_KILL_SWITCH: float = 0.20  # 20% account drawdown halts new

# ---------------------------------------------------------------------------
# Output limits
# ---------------------------------------------------------------------------
MAX_CANDIDATES_IN_EMAIL: int = 5    # ranked top-5 PER TIER
# Unthrottled paper engine (playbook change #8): the paper pool needs
# 200+ trades by month 6, which weekly 4-5 opens can't reach at cap 10.
MAX_VIRTUAL_OPEN: int = 24

# ---------------------------------------------------------------------------
# Vol-spike playbook (playbook change #11)
# ---------------------------------------------------------------------------
# VIX > KILL blocks NEW entries only; open defined-risk positions ride to
# their normal exits. POST-SPIKE window: VIX was above KILL within the
# lookback and has now fallen back through RECOVERY — historically the
# richest premium-selling regime a patient 1-lot trader can access.
VIX_POST_SPIKE_RECOVERY: float = 30.0
VIX_SPIKE_LOOKBACK_DAYS: int = 10

# ---------------------------------------------------------------------------
# Anti-tinker
# ---------------------------------------------------------------------------
# Pre-commit hook enforces no edits within this many days.
# This is the cheapest defense against the week-8 over-fitting urge.
THRESHOLD_CHANGE_COOLDOWN_DAYS: int = 14

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
# Set via env vars at runtime — never commit credentials.
#   SMTP_USER     (e.g., your.email@gmail.com)
#   SMTP_PASSWORD (Gmail App Password — 16 chars, not your login password)
#   SMTP_TO       (where to send alerts; defaults to SMTP_USER)
#   HEALTHCHECKS_PING_URL  (free at healthchecks.io)
#   FINNHUB_API_KEY        (free tier at finnhub.io for earnings data)
#   IBKR_PORT              (default 7497 for TWS paper)
SMTP_HOST: str = "smtp.gmail.com"
SMTP_PORT: int = 587

# ---------------------------------------------------------------------------
# Trading friction (applied to VIRTUAL PnL so the track record is honest)
# ---------------------------------------------------------------------------
# IBKR Fixed pricing has a $1.00 ORDER MINIMUM which always binds at 1-lot.
# $0.65 was too kind. (Playbook change #2.)
COMMISSION_PER_CONTRACT: float = 1.00
# Slippage as a fraction of premium, each way.
# BASE = plausible fill quality in liquid penny-quoted names.
# PESSIMISTIC = wide-spread reality; paper P&L is always reported as a
# [pessimistic, base] band because paper fills contain zero slippage
# information. (Playbook change #3.)
SLIPPAGE_PCT_OF_PREMIUM: float = 0.05
SLIPPAGE_PCT_PESSIMISTIC: float = 0.10

# ---------------------------------------------------------------------------
# Currency — the person eats in EUR (playbook change #12)
# ---------------------------------------------------------------------------
# Every virtual close also records the EURUSD rate and PnL in EUR.
TRACK_EUR: bool = True

# ---------------------------------------------------------------------------
# Virtual tracker exit rules (mirrors what a disciplined trader would do)
# ---------------------------------------------------------------------------
# Take profit at 50% of max profit (industry-standard CSP exit).
VIRTUAL_TP_PCT: float = 0.50

# Force exit at 21 DTE remaining regardless (avoids gamma).
VIRTUAL_FORCE_EXIT_DTE: int = 21

# Stop loss: close if loss reaches 2x credit received.
VIRTUAL_SL_MULTIPLE: float = 2.0
