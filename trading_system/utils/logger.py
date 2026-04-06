"""Logging setup for the trading system."""

import logging
import sys


def setup_logging(level: str = "INFO"):
    """Configure logging with a clean format."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger("trading_system")
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for name in ["urllib3", "yfinance", "peewee"]:
        logging.getLogger(name).setLevel(logging.WARNING)

    return root
