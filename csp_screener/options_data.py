"""
Options chain + IV + greeks fetcher.

Priority:
  1. IBKR (TWS, port 7497) — best data, accurate greeks, near-real-time IV
  2. yfinance .option_chain — fallback when TWS is off; we LABEL the
     data quality so the email surfaces the degradation

We do NOT manufacture greeks. If we can't get them, we report None and the
email tells the user to check in barchart / IBKR.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from csp_screener import config

logger = logging.getLogger(__name__)


@dataclass
class OptionContract:
    ticker: str
    expiration: datetime
    strike: float
    right: str               # "P" or "C"
    bid: float
    ask: float
    last: float
    mid: float
    open_interest: int
    volume: int
    iv: Optional[float]      # implied vol (annualized, e.g. 0.45 = 45%)
    delta: Optional[float]   # signed (puts are negative)
    source: str = "unknown"  # "ibkr" or "yfinance"

    @property
    def bid_ask_spread_pct(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return abs(self.ask - self.bid) / self.mid

    @property
    def dte(self) -> int:
        return max(0, (self.expiration.date() - datetime.now().date()).days)


@dataclass
class OptionsChain:
    ticker: str
    spot: float
    expirations: list[datetime] = field(default_factory=list)
    puts: list[OptionContract] = field(default_factory=list)
    calls: list[OptionContract] = field(default_factory=list)
    source: str = "unknown"
    fetched_at: datetime = field(default_factory=datetime.now)

    def puts_for_expiration(self, exp: datetime) -> list[OptionContract]:
        return [p for p in self.puts if p.expiration.date() == exp.date()]


def fetch_chain(ticker: str, spot_price: float, ibkr_client=None) -> Optional[OptionsChain]:
    """
    Try IBKR first if a connected client is provided, else yfinance fallback.
    Returns None on total failure.
    """
    if ibkr_client is not None:
        try:
            chain = _fetch_ibkr(ticker, spot_price, ibkr_client)
            if chain and (chain.puts or chain.calls):
                return chain
        except Exception as e:
            logger.warning(f"IBKR options fetch failed for {ticker}: {e}")

    try:
        return _fetch_yfinance(ticker, spot_price)
    except Exception as e:
        logger.warning(f"yfinance options fetch failed for {ticker}: {e}")
        return None


def _fetch_yfinance(ticker: str, spot_price: float) -> Optional[OptionsChain]:
    """Fetch option chain from yfinance. Greeks/IV may be missing or stale."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    expirations_str = t.options or []
    if not expirations_str:
        return None

    # Filter to expirations within our DTE window
    now = datetime.now().date()
    candidate_exps = []
    for exp_str in expirations_str:
        try:
            exp = datetime.strptime(exp_str, "%Y-%m-%d")
            dte = (exp.date() - now).days
            if config.DTE_MIN <= dte <= config.DTE_MAX:
                candidate_exps.append(exp)
        except ValueError:
            continue

    if not candidate_exps:
        return None

    chain = OptionsChain(
        ticker=ticker,
        spot=spot_price,
        expirations=candidate_exps,
        source="yfinance",
    )

    for exp in candidate_exps:
        try:
            oc = t.option_chain(exp.strftime("%Y-%m-%d"))
            for _, row in oc.puts.iterrows():
                # NaN-safe extraction. NOTE: `NaN or 0` does NOT work — NaN is
                # truthy, so int(NaN) raised and silently dropped the whole
                # expiration. Use _num() everywhere.
                bid = _num(row.get("bid"))
                ask = _num(row.get("ask"))
                last = _num(row.get("lastPrice"))
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
                iv = row.get("impliedVolatility")
                iv = float(iv) if (pd.notna(iv) and iv) else None

                chain.puts.append(OptionContract(
                    ticker=ticker,
                    expiration=exp,
                    strike=float(row["strike"]),
                    right="P",
                    bid=bid,
                    ask=ask,
                    last=last,
                    mid=mid,
                    open_interest=int(_num(row.get("openInterest"))),
                    volume=int(_num(row.get("volume"))),
                    iv=iv,
                    delta=_estimate_put_delta(spot_price, float(row["strike"]), exp, iv),
                    source="yfinance",
                ))
        except Exception as e:
            logger.debug(f"yfinance chain parse failed for {ticker} {exp}: {e}")

    return chain


def _num(value, default: float = 0.0) -> float:
    """NaN/None-safe float coercion."""
    if value is None:
        return default
    try:
        f = float(value)
    except (ValueError, TypeError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _fetch_ibkr(ticker: str, spot_price: float, ib) -> Optional[OptionsChain]:
    """Fetch option chain via ib_insync. ib is a connected IB() instance."""
    try:
        from ib_insync import Stock, Option, util
    except ImportError:
        logger.warning("ib_insync not installed; falling back")
        return None

    stock = Stock(ticker, "SMART", "USD")
    try:
        ib.qualifyContracts(stock)
    except Exception as e:
        logger.debug(f"qualifyContracts failed for {ticker}: {e}")
        return None

    try:
        chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
    except Exception as e:
        logger.debug(f"reqSecDefOptParams failed for {ticker}: {e}")
        return None
    if not chains:
        return None
    chain_def = next((c for c in chains if c.exchange == "SMART"), chains[0])

    now = datetime.now().date()
    candidate_exps = []
    for exp_str in sorted(chain_def.expirations):
        try:
            exp = datetime.strptime(exp_str, "%Y%m%d")
            dte = (exp.date() - now).days
            if config.DTE_MIN <= dte <= config.DTE_MAX:
                candidate_exps.append(exp)
        except ValueError:
            continue
    if not candidate_exps:
        return None

    # Pick strikes near spot (±25%) to limit requests
    band_low = spot_price * 0.75
    band_high = spot_price * 1.05  # we mainly want OTM puts (below spot)
    eligible_strikes = sorted(s for s in chain_def.strikes if band_low <= s <= band_high)

    chain = OptionsChain(
        ticker=ticker,
        spot=spot_price,
        expirations=candidate_exps,
        source="ibkr",
    )

    # Build the contracts to request quotes for
    contracts = []
    for exp in candidate_exps:
        for k in eligible_strikes:
            opt = Option(ticker, exp.strftime("%Y%m%d"), k, "P", "SMART")
            contracts.append((opt, exp, k))

    try:
        opts_only = [c[0] for c in contracts]
        ib.qualifyContracts(*opts_only)
        tickers = ib.reqTickers(*opts_only)
        for (opt, exp, k), tdata in zip(contracts, tickers):
            bid = float(tdata.bid or 0)
            ask = float(tdata.ask or 0)
            last = float(tdata.last or 0)
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
            iv = None
            delta = None
            try:
                if tdata.modelGreeks:
                    iv = float(tdata.modelGreeks.impliedVol or 0) or None
                    delta = float(tdata.modelGreeks.delta or 0) or None
            except Exception:
                pass
            chain.puts.append(OptionContract(
                ticker=ticker,
                expiration=exp,
                strike=k,
                right="P",
                bid=bid,
                ask=ask,
                last=last,
                mid=mid,
                open_interest=int(getattr(tdata, "putOpenInterest", 0) or 0),
                volume=int(getattr(tdata, "volume", 0) or 0),
                iv=iv,
                delta=delta,
                source="ibkr",
            ))
    except Exception as e:
        logger.debug(f"ibkr reqTickers failed for {ticker}: {e}")
        return None

    return chain


def _estimate_put_delta(
    spot: float,
    strike: float,
    expiration: datetime,
    iv: Optional[float],
) -> Optional[float]:
    """
    Rough Black-Scholes put delta estimate (no rate, no dividend).
    Used as a fallback when no real greeks are available.

    Returns signed delta in [-1, 0]. Returns None if inputs invalid.
    """
    if not iv or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    days = (expiration.date() - datetime.now().date()).days
    if days <= 0:
        return None
    t = days / 365.0
    try:
        d1 = (math.log(spot / strike) + (0.5 * iv * iv) * t) / (iv * math.sqrt(t))
        # N(-d1) for put delta = -N(-d1)
        from math import erf, sqrt
        n_minus_d1 = 0.5 * (1 + erf(-d1 / sqrt(2)))
        delta = -n_minus_d1
        return float(delta)
    except Exception:
        return None
