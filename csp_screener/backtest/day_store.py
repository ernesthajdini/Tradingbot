"""
FULL-STUDY PHASE 4 — the day store.

14.9M option-quote rows do not belong in one DataFrame: the engine's access
pattern is strictly per-day (chains_for_date filters to `asof`, and
_position_mark looks up one contract on `asof`), so the data is stored
partitioned by quote_date and streamed one day at a time. Memory stays flat
at ~one day of quotes regardless of study length.

Also computed ONCE here, vectorized, and reused by all 6 pre-registered
config runs:
  * iv    — bisection inversion of the PRODUCTION Black-Scholes put pricer
            against the two-sided close mid (same model the marks use, so
            replay pricing is self-consistent). Semantics mirror
            data_loader._invert_bs_iv exactly: NaN when the mid is at or
            below intrinsic, or when even 300% vol cannot reach the mid.
  * delta — production's estimator (-N(-d1)) from that iv. Strike selection
            depends on it, so it must match, not approximate.

Build:  python csp_screener/backtest/day_store.py
Read:   DayStore(path).day(date) -> DataFrame ; .dates -> [date, ...]
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
from scipy.special import ndtr

from csp_screener.backtest import data_loader

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
STORE = DATA / "daystore"
BATCH_TICKERS = 250
IV_LO, IV_HI, IV_ITERS = 0.01, 3.0, 40


# ---------------------------------------------------------------------------
# Vectorized production pricing (mirrors virtual_tracker.black_scholes_put_price
# and setup_generator/options_data._estimate_put_delta at rate = 0)
# ---------------------------------------------------------------------------

def bs_put(S, K, T, sigma):
    """Black-Scholes put, rate 0, elementwise. T in years."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    out = np.maximum(K - S, 0.0)
    ok = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    if ok.any():
        s, k, t, sig = S[ok], K[ok], T[ok], sigma[ok]
        sq = sig * np.sqrt(t)
        d1 = (np.log(s / k) + 0.5 * sig * sig * t) / sq
        d2 = d1 - sq
        out[ok] = np.maximum(k * ndtr(-d2) - s * ndtr(-d1), 0.0)
    return out


def invert_iv(mid, S, K, T):
    """Bisection IV, vectorized. NaN where production returns None."""
    mid, S, K, T = (np.asarray(x, dtype=float) for x in (mid, S, K, T))
    n = mid.shape[0]
    iv = np.full(n, np.nan)
    intrinsic = np.maximum(K - S, 0.0)
    live = (mid > 0) & (S > 0) & (K > 0) & (T > 0) & (mid > intrinsic + 1e-6)
    if not live.any():
        return iv
    # Unreachable even at the IV hygiene ceiling -> leave NaN (production
    # returns None there; the zombie/IV-cap defenses own that case).
    top = bs_put(S[live], K[live], T[live], np.full(live.sum(), IV_HI))
    reach = top >= mid[live]
    idx = np.flatnonzero(live)[reach]
    if idx.size == 0:
        return iv
    s, k, t, target = S[idx], K[idx], T[idx], mid[idx]
    lo = np.full(idx.size, IV_LO)
    hi = np.full(idx.size, IV_HI)
    for _ in range(IV_ITERS):
        m = 0.5 * (lo + hi)
        below = bs_put(s, k, t, m) < target
        # BUG (found by the study audit, cost: the entire first study run):
        # this line read np.where(below, m, hi), so when `below` was True
        # BOTH bounds became m — the interval collapsed on iteration 1 and
        # every IV in the 21.6M-row store came out as the first midpoint,
        # 0.5*(0.01+3.0) = 1.505. Delta then carried no market information
        # (a pure function of S/K and T), strike selection drifted to ~22%
        # OTM $5 lottery puts instead of ~0.30 delta, and model marks ran
        # 5-25x hot, manufacturing 121 fake stop-losses. Verified against
        # the scalar data_loader._invert_bs_iv on real contracts.
        lo = np.where(below, m, lo)
        hi = np.where(below, hi, m)
    iv[idx] = np.round(0.5 * (lo + hi), 4)
    return iv


def put_delta(S, K, T, sigma):
    """Production's estimated put delta: -N(-d1). NaN where undefined."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    out = np.full(S.shape[0], np.nan)
    ok = (T > 0) & (sigma > 0) & (S > 0) & (K > 0) & np.isfinite(sigma)
    if ok.any():
        s, k, t, sig = S[ok], K[ok], T[ok], sigma[ok]
        d1 = (np.log(s / k) + 0.5 * sig * sig * t) / (sig * np.sqrt(t))
        out[ok] = -ndtr(-d1)
    return out


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(data_root: Path = DATA, store: Path = STORE,
          options_subdir: str = "options") -> dict:
    opt_root = data_root / options_subdir
    tickers = sorted(p.name for p in opt_root.iterdir() if p.is_dir())
    store.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    stats = {"tickers": 0, "rows": 0, "batches": 0}

    for start in range(0, len(tickers), BATCH_TICKERS):
        batch = tickers[start:start + BATCH_TICKERS]
        try:
            frame, _ = data_loader.load_thetadata_full(
                data_root, tickers=batch, compute_iv=False,
                options_subdir=options_subdir)
        except FileNotFoundError:
            continue
        if frame.empty:
            continue
        # Vectorized iv + delta (the loader leaves both NaN by design here)
        qd = pd.to_datetime(frame["quote_date"])
        exp = pd.to_datetime(frame["expiration"])
        dte = (exp - qd).dt.days.to_numpy()
        T = dte / 365.0
        S = frame["underlying_price"].to_numpy(dtype=float)
        K = frame["strike"].to_numpy(dtype=float)
        bid = frame["bid"].to_numpy(dtype=float)
        ask = frame["ask"].to_numpy(dtype=float)
        two_sided = (bid > 0) & (ask > 0)
        mid = np.where(two_sided, 0.5 * (bid + ask), np.nan)
        iv = invert_iv(np.nan_to_num(mid), S, K, T)
        iv[~two_sided] = np.nan
        frame["iv"] = iv
        frame["delta"] = put_delta(S, K, T, iv)

        # The vendor emits more than one EOD snapshot for some
        # (quote_date, contract) pairs — ~12-21% duplicate rows, which
        # inflate coverage counts and let a single contract be marked twice.
        # Keep the last snapshot per contract-day.
        frame = frame.drop_duplicates(
            subset=["quote_date", "ticker", "expiration", "strike"],
            keep="last")
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pads.write_dataset(
            table, store, format="parquet",
            partitioning=pads.partitioning(
                pa.schema([("quote_date", pa.date32())]), flavor="hive"),
            basename_template=f"b{start}-{{i}}.parquet",
            existing_data_behavior="overwrite_or_ignore",
            # One batch spans every date its tickers quote on — ~2,700
            # partitions, well past pyarrow's default cap of 1024.
            max_partitions=8192,
            max_open_files=8192,
        )
        stats["tickers"] += len(batch)
        stats["rows"] += len(frame)
        stats["batches"] += 1
        print(f"  {stats['tickers']}/{len(tickers)} tickers, "
              f"{stats['rows']:,} rows ({(time.time()-t0)/60:.1f}min)",
              flush=True)
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    return stats


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

class DayStore:
    """Per-day quote access. Duck-types the `frame` the engine consumes:
    engine helpers call .day(asof) instead of masking a giant DataFrame."""

    def __init__(self, store: Path = STORE, cache_days: int = 3):
        self.store = Path(store)
        self._dates = sorted(
            date.fromisoformat(p.name.split("=", 1)[1])
            for p in self.store.iterdir()
            if p.is_dir() and p.name.startswith("quote_date=")
        )
        self._cache: dict[date, pd.DataFrame] = {}
        self._cache_days = cache_days

    @property
    def dates(self) -> list[date]:
        return list(self._dates)

    def day(self, asof: date) -> pd.DataFrame:
        hit = self._cache.get(asof)
        if hit is not None:
            return hit
        part = self.store / f"quote_date={asof.isoformat()}"
        if not part.is_dir():
            df = pd.DataFrame(columns=data_loader.NORMALIZED_COLUMNS)
        else:
            df = pads.dataset(part, format="parquet").to_table().to_pandas()
            df["quote_date"] = asof
            df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
        if len(self._cache) >= self._cache_days:
            self._cache.pop(next(iter(self._cache)))
        self._cache[asof] = df
        return df


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--options-subdir", default="options")
    ap.add_argument("--store", default=str(STORE))
    a = ap.parse_args()
    print(f"Building day store from {a.options_subdir}...")
    s = build(DATA, Path(a.store), options_subdir=a.options_subdir)
    ds = DayStore(Path(a.store))
    print(f"DAY STORE BUILT: {s}")
    print(f"{len(ds.dates)} trading days "
          f"({ds.dates[0]} -> {ds.dates[-1]})" if ds.dates else "EMPTY")
