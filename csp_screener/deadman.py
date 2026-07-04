"""
Deadman switch via healthchecks.io.

If the screener fails to run for any reason — crashed, computer off, network
down — healthchecks.io will email you. Silence is no longer success.

Setup (one-time):
  1. Sign up free at https://healthchecks.io
  2. Create a check named "csp_screener" with a weekly schedule (Sunday)
  3. Copy the ping URL (looks like https://hc-ping.com/<uuid>)
  4. Set env var HEALTHCHECKS_PING_URL to that URL

Free tier covers 20 checks, more than enough.
"""

from __future__ import annotations

import logging
import os
import requests

logger = logging.getLogger(__name__)


def ping_success(message: str = "") -> bool:
    """
    POST a success ping to healthchecks.io.

    The optional message body shows up in the check's event log so you can see
    what the last successful run found (number of candidates, virtual count, etc.).
    """
    url = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()
    if not url:
        logger.debug("HEALTHCHECKS_PING_URL not set; skipping ping")
        return False
    try:
        resp = requests.post(url, data=message.encode("utf-8"), timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Healthchecks ping failed: {e}")
        return False


def ping_start() -> bool:
    """POST a 'start' ping so we know the screener kicked off."""
    base = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()
    if not base:
        return False
    url = base.rstrip("/") + "/start"
    try:
        requests.post(url, timeout=10)
        return True
    except Exception as e:
        logger.debug(f"Healthchecks start ping failed: {e}")
        return False


def ping_failure(message: str = "") -> bool:
    """POST a failure ping with optional error details."""
    base = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()
    if not base:
        return False
    url = base.rstrip("/") + "/fail"
    try:
        requests.post(url, data=message.encode("utf-8"), timeout=10)
        return True
    except Exception as e:
        logger.debug(f"Healthchecks failure ping failed: {e}")
        return False
