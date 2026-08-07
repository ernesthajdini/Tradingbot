"""
Historical EOD option-chain loaders → the production OptionsChain shape.

The engine consumes {date: {ticker: OptionsChain}}, built from a NORMALIZED
long-format DataFrame with one row per (quote_date, ticker, expiration,
strike) put:

    quote_date   date        the EOD snapshot date
    ticker       str
    expiration   date
    strike       float
    bid, ask     float       EOD quotes (0.0 when absent)
    last         float       last trade price (0.0 when absent)
    volume       int
    open_interest int
    iv           float|NaN   annualized implied vol
    delta        float|NaN   signed put delta
    underlying_price float   EOD spot

Adapters:
  * load_normalized_csv — the schema above, verbatim (write-your-own).
  * load_optionsdx_csv  — optionsDX free/paid flat files (wide C_/P_ format).
  * load_thetadata_dir  — the CSVs written by thetadata_pull.py (v3 EOD
    format, verified against live pulls 2026-08-07). Notes: (a) the EOD
    endpoint carries NO iv/delta — IV is BS-INVERTED from the two-sided
    close mid using the production pricing model (self-consistent with how
    marks are computed), delta estimated from that IV; (b) open interest is
    VALUE-gated — on FREE-tier pulls OI=0 throughout, which the production
    gates treat as OI-UNKNOWN (accept with warning; that is their designed
    degradation); (c) prices are AS-TRADED, same basis as the strikes —
    the stock_eod.csv per ticker is the aligned spot source, use
    load_thetadata_stock for the prices dict, not yfinance.

Survivorship metadata: every loader returns (frame, meta) where meta says
whether the source includes delisted underlyings. The engine stamps this
into every run log — MANIFEST.md forbids unlabeled survivorship.

SPLIT/DIVIDEND ALIGNMENT (pilot checklist item): the engine mixes the price
frames' Close (spot for entries, RV, model-mark fallback) with the chain
frame's strikes. Both MUST be on the same adjustment basis — AS-TRADED
prices against as-traded strikes. Feeding retroactively split-adjusted
closes (yfinance default) against as-traded vendor chains puts every
post-split name's spot on the wrong scale: never-sell-ITM voids one side of
the split and model marks fabricate stop-losses. Sanity-check per ticker:
frame Close vs chain underlying_price should agree within ~1% on overlapping
dates — investigate any name where they don't.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from csp_screener.options_data import OptionContract, OptionsChain

logger = logging.getLogger(__name__)

NORMALIZED_COLUMNS = [
    "quote_date", "ticker", "expiration", "strike", "bid", "ask", "last",
    "volume", "open_interest", "iv", "delta", "underlying_price",
]


def load_normalized_csv(path, ticker: Optional[str] = None) -> tuple[pd.DataFrame, dict]:
    """Load a CSV already in the normalized schema."""
    df = pd.read_csv(path)
    missing = [c for c in NORMALIZED_COLUMNS if c not in df.columns and c != "ticker"]
    if missing:
        raise ValueError(f"{path}: missing normalized columns {missing}")
    if "ticker" not in df.columns:
        if not ticker:
            raise ValueError(f"{path}: no ticker column and none supplied")
        df["ticker"] = ticker
    df["quote_date"] = pd.to_datetime(df["quote_date"]).dt.date
    df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
    meta = {"source": str(path), "includes_delisted": None}  # caller must say
    return df[NORMALIZED_COLUMNS], meta


def load_optionsdx_csv(path, ticker: str) -> tuple[pd.DataFrame, dict]:
    """
    Parse an optionsDX EOD flat file into normalized PUT rows.

    optionsDX ships wide rows (one row per strike/expiry with C_* and P_*
    columns) and brackets its headers like ' [QUOTE_DATE]'. Header cleanup
    is tolerant: brackets and whitespace stripped, case ignored.
    """
    df = pd.read_csv(path)
    df.columns = [str(c).strip().strip("[]").strip().upper() for c in df.columns]

    def col(name: str, default=None):
        if name in df.columns:
            return df[name]
        return pd.Series([default] * len(df))

    def num(series, default=0.0):
        return pd.to_numeric(series, errors="coerce").fillna(default)

    out = pd.DataFrame({
        "quote_date": pd.to_datetime(col("QUOTE_DATE"), errors="coerce").dt.date,
        "ticker": ticker,
        "expiration": pd.to_datetime(col("EXPIRE_DATE"), errors="coerce").dt.date,
        "strike": num(col("STRIKE")),
        "bid": num(col("P_BID")),
        "ask": num(col("P_ASK")),
        "last": num(col("P_LAST")),
        "volume": num(col("P_VOLUME")).astype(int),
        "open_interest": num(col("P_OI")).astype(int),
        "iv": pd.to_numeric(col("P_IV"), errors="coerce"),
        "delta": pd.to_numeric(col("P_DELTA"), errors="coerce"),
        "underlying_price": num(col("UNDERLYING_LAST")),
    })
    out = out.dropna(subset=["quote_date", "expiration"])
    out = out[out["strike"] > 0]
    meta = {"source": str(path), "includes_delisted": False,
            "note": "optionsDX samples are per-ticker files of live names — "
                    "treat as survivorship-biased unless proven otherwise"}
    return out[NORMALIZED_COLUMNS], meta


def _invert_bs_iv(mid: float, spot: float, strike: float, dte: int) -> Optional[float]:
    """Implied vol by bisection on the production BS put pricer — the same
    model the marks use, so replay pricing is self-consistent."""
    from csp_screener.virtual_tracker import black_scholes_put_price
    if mid <= 0 or spot <= 0 or strike <= 0 or dte <= 0:
        return None
    intrinsic = max(strike - spot, 0.0)
    if mid <= intrinsic + 1e-6:
        return None
    lo, hi = 0.01, 3.0
    if black_scholes_put_price(spot, strike, dte, hi) < mid:
        return None  # priced beyond 300% IV — leave iv unset (hygiene cap)
    for _ in range(40):
        sigma = (lo + hi) / 2
        if black_scholes_put_price(spot, strike, dte, sigma) < mid:
            lo = sigma
        else:
            hi = sigma
    return round((lo + hi) / 2, 4)


def load_thetadata_dir(data_dir) -> tuple[pd.DataFrame, dict]:
    """
    Load every ticker directory written by thetadata_pull.py into one
    normalized frame. Layout: data_dir/<TICKER>/puts_<EXP>.csv + stock_eod.csv.
    v3 EOD columns: symbol,expiration,strike,right,created,last_trade,open,
    high,low,close,volume,count,bid_size,bid_exchange,bid,bid_condition,
    ask_size,ask_exchange,ask,ask_condition — quote_date derives from
    `created` (the EOD snapshot timestamp), OI is absent on FREE pulls (0).
    """
    data_dir = Path(data_dir)
    frames = []
    for tdir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        ticker = tdir.name
        for f in sorted(tdir.glob("puts_*.csv")):
            try:
                df = pd.read_csv(f)
            except Exception as e:
                logger.warning(f"{f}: unreadable ({e})")
                continue
            if df.empty or "created" not in df.columns:
                continue
            exp = date.fromisoformat(f.stem.replace("puts_", ""))
            out = pd.DataFrame({
                "quote_date": pd.to_datetime(df["created"]).dt.date,
                "ticker": ticker,
                "expiration": exp,
                "strike": pd.to_numeric(df["strike"], errors="coerce"),
                "bid": pd.to_numeric(df["bid"], errors="coerce").fillna(0.0),
                "ask": pd.to_numeric(df["ask"], errors="coerce").fillna(0.0),
                "last": pd.to_numeric(df["close"], errors="coerce").fillna(0.0),
                "volume": pd.to_numeric(df["volume"], errors="coerce")
                    .fillna(0).astype(int),
                "open_interest": (
                    pd.to_numeric(df["open_interest"], errors="coerce")
                      .fillna(0).astype(int)
                    if "open_interest" in df.columns
                    else pd.Series(0, index=df.index)),
                "iv": float("nan"),
                "delta": float("nan"),
                "underlying_price": float("nan"),
            })
            frames.append(out.dropna(subset=["quote_date", "strike"]))
    if not frames:
        raise FileNotFoundError(f"no puts_*.csv under {data_dir}")
    frame = pd.concat(frames, ignore_index=True)

    # Stamp underlying_price from the aligned as-traded stock files, then
    # BS-invert IV from two-sided mids (delta follows in chains_for_date).
    spots = {}
    for tdir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        sf = tdir / "stock_eod.csv"
        if not sf.exists():
            continue
        sdf = load_thetadata_stock(sf)
        spots[tdir.name] = dict(
            zip(sdf.index.date, sdf["Close"].astype(float)))
    frame["underlying_price"] = [
        spots.get(t, {}).get(d, float("nan"))
        for t, d in zip(frame["ticker"], frame["quote_date"])
    ]
    frame = frame.dropna(subset=["underlying_price"])

    ivs = []
    for row in frame.itertuples(index=False):
        if row.bid > 0 and row.ask > 0:
            mid = (row.bid + row.ask) / 2
            dte = (row.expiration - row.quote_date).days
            ivs.append(_invert_bs_iv(mid, row.underlying_price,
                                     row.strike, dte))
        else:
            ivs.append(None)
    frame["iv"] = pd.array(ivs, dtype="float64")

    meta = {"source": str(data_dir), "includes_delisted": True,
            "note": "ThetaData serves delisted underlyings (FSR verified "
                    "2026-08-07); OI=0 on FREE-tier pulls (VALUE-gated) — "
                    "production gates read that as OI-UNKNOWN; iv is "
                    "BS-inverted from two-sided close mids"}
    return frame[NORMALIZED_COLUMNS], meta


def load_thetadata_stock(path) -> pd.DataFrame:
    """One stock_eod.csv -> OHLCV DataFrame indexed by date (as-traded)."""
    df = pd.read_csv(path)
    idx = pd.DatetimeIndex(pd.to_datetime(df["created"]).dt.normalize())
    # .to_numpy() strips the source RangeIndex — passing the Series directly
    # would silently reindex them against idx into all-NaN (alignment).
    return pd.DataFrame({
        "Close": pd.to_numeric(df["close"], errors="coerce").to_numpy(),
        "Volume": pd.to_numeric(df["volume"], errors="coerce")
            .fillna(0.0).to_numpy(),
    }, index=idx).dropna(subset=["Close"])


def load_thetadata_prices(data_dir) -> dict[str, pd.DataFrame]:
    """{ticker: OHLCV frame} from every stock_eod.csv under data_dir."""
    data_dir = Path(data_dir)
    out = {}
    for tdir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        sf = tdir / "stock_eod.csv"
        if sf.exists():
            try:
                out[tdir.name] = load_thetadata_stock(sf)
            except Exception as e:
                logger.warning(f"{sf}: unreadable ({e})")
    return out


# ---------------------------------------------------------------------------
# Normalized frame -> production OptionsChain objects
# ---------------------------------------------------------------------------

def _estimate_put_delta_asof(
    spot: float, strike: float, expiration: date, iv: Optional[float],
    asof: date,
) -> Optional[float]:
    """BS put delta with an injected as-of date (options_data's estimator
    reads the wall clock, which a replay must never do)."""
    import math
    if not iv or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    days = (expiration - asof).days
    if days <= 0:
        return None
    t = days / 365.0
    try:
        d1 = (math.log(spot / strike) + (0.5 * iv * iv) * t) / (iv * math.sqrt(t))
        return -0.5 * (1 + math.erf(-d1 / math.sqrt(2)))
    except Exception:
        return None


def chains_for_date(
    frame: pd.DataFrame,
    asof: date,
    dte_min: int,
    dte_max: int,
) -> dict[str, OptionsChain]:
    """
    Build {ticker: OptionsChain} for one historical date, restricted to the
    declared DTE window (this is how the DTE knob is wired — the generators
    only ever see expirations inside the window under study).

    last_trade_date is set to the snapshot date so the zombie-staleness
    filter — which compares against the WALL CLOCK — is not run here at all;
    EOD vendor rows are point-in-time by construction. The monotonicity and
    IV-hygiene defenses still apply via the setup path's sanity gates.
    """
    day = frame[frame["quote_date"] == asof]
    chains: dict[str, OptionsChain] = {}
    for ticker, rows in day.groupby("ticker"):
        spot = float(rows["underlying_price"].iloc[-1])
        if not np.isfinite(spot) or spot <= 0:
            continue
        contracts: list[OptionContract] = []
        expirations: set = set()
        for _, r in rows.iterrows():
            exp = r["expiration"]
            dte = (exp - asof).days
            if not (dte_min <= dte <= dte_max):
                continue
            bid, ask = float(r["bid"]), float(r["ask"])
            last = float(r["last"])
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
            iv = r["iv"]
            iv = float(iv) if pd.notna(iv) and iv else None
            delta = r["delta"]
            delta = (float(delta) if pd.notna(delta) and delta
                     else _estimate_put_delta_asof(spot, float(r["strike"]),
                                                   exp, iv, asof))
            exp_dt = datetime.combine(exp, datetime.min.time())
            expirations.add(exp_dt)
            contracts.append(OptionContract(
                ticker=ticker,
                expiration=exp_dt,
                strike=float(r["strike"]),
                right="P",
                bid=bid, ask=ask, last=last, mid=mid,
                open_interest=int(r["open_interest"]),
                volume=int(r["volume"]),
                iv=iv, delta=delta,
                source="backtest",
                last_trade_date=datetime.combine(asof, datetime.min.time()),
            ))
        if contracts:
            chains[ticker] = OptionsChain(
                ticker=ticker, spot=spot,
                expirations=sorted(expirations),
                puts=contracts, source="backtest",
                fetched_at=datetime.combine(asof, datetime.min.time()),
            )
    return chains


def universe_asof(
    prices: dict[str, pd.DataFrame],
    asof: date,
    price_min: float,
    price_max: float,
    min_volume: float,
) -> list[str]:
    """
    As-of-date universe reconstruction: which tickers would the production
    price-band + volume filters have admitted ON THIS DATE? `prices` must
    include names that later died — a universe of today's survivors replayed
    over history is survivorship fiction (MANIFEST rail).
    """
    out = []
    for ticker, df in prices.items():
        if not hasattr(df.index, "date"):
            # A non-datetime index cannot be as-of sliced; silently using
            # the full frame would be a lookahead leak (the last price
            # deciding a historical date's universe). Refuse loudly.
            raise ValueError(
                f"universe_asof: {ticker} price frame has a non-datetime "
                f"index — load CSVs with parse_dates/index_col so the as-of "
                f"cut can apply")
        hist = df[df.index.date <= asof]
        if hist.empty:
            continue
        px = float(hist["Close"].iloc[-1])
        vol = float(hist["Volume"].tail(20).mean()) if "Volume" in hist.columns else 0.0
        if price_min <= px <= price_max and vol >= min_volume:
            out.append(ticker)
    return sorted(out)
