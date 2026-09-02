"""
AMENDMENT 12 DATA — the single-name CALL day store.

Mirrors day_store.build() for calls_<EXP>.csv files: IV is inverted from a
Black-Scholes CALL pricer against the two-sided close mid, delta is N(d1).
Same bisection as the put store — with the interval-collapse bug that cost
the first study run kept fixed (both bounds move, never the same one).

Open interest: merged from oi_calls_<EXP>.csv when present, else 0 (the
OI-UNKNOWN path the put loader also has). The study gates on it when known.

    python csp_screener/backtest/build_call_store.py
Read: DayStore(CALL_STORE).day(date)
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
from csp_screener.backtest.day_store import (BATCH_TICKERS, DATA, IV_HI,
                                             IV_ITERS, IV_LO)

CALL_STORE = DATA / "daystore_calls"


def bs_call(S, K, T, sigma):
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    out = np.maximum(S - K, 0.0)
    ok = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    if ok.any():
        s, k, t, sig = S[ok], K[ok], T[ok], sigma[ok]
        sq = sig * np.sqrt(t)
        d1 = (np.log(s / k) + 0.5 * sig * sig * t) / sq
        d2 = d1 - sq
        out[ok] = np.maximum(s * ndtr(d1) - k * ndtr(d2), 0.0)
    return out


def invert_call_iv(mid, S, K, T):
    mid, S, K, T = (np.asarray(x, dtype=float) for x in (mid, S, K, T))
    iv = np.full(mid.shape[0], np.nan)
    intrinsic = np.maximum(S - K, 0.0)
    live = (mid > 0) & (S > 0) & (K > 0) & (T > 0) & (mid > intrinsic + 1e-6)
    if not live.any():
        return iv
    top = bs_call(S[live], K[live], T[live], np.full(live.sum(), IV_HI))
    idx = np.flatnonzero(live)[top >= mid[live]]
    if idx.size == 0:
        return iv
    s, k, t, target = S[idx], K[idx], T[idx], mid[idx]
    lo = np.full(idx.size, IV_LO)
    hi = np.full(idx.size, IV_HI)
    for _ in range(IV_ITERS):
        m = 0.5 * (lo + hi)
        below = bs_call(s, k, t, m) < target
        lo = np.where(below, m, lo)     # both bounds move — see day_store
        hi = np.where(below, hi, m)
    iv[idx] = np.round(0.5 * (lo + hi), 4)
    return iv


def call_delta(S, K, T, sigma):
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    out = np.full(S.shape[0], np.nan)
    ok = (T > 0) & (sigma > 0) & (S > 0) & (K > 0) & np.isfinite(sigma)
    if ok.any():
        s, k, t, sig = S[ok], K[ok], T[ok], sigma[ok]
        d1 = (np.log(s / k) + 0.5 * sig * sig * t) / (sig * np.sqrt(t))
        out[ok] = ndtr(d1)
    return out


def load_calls(tickers, options_subdir="options"):
    """Same normalized frame as the put loader, from calls_<EXP>.csv."""
    frames = []
    for ticker in tickers:
        tdir = DATA / options_subdir / ticker
        stock_csv = DATA / "stocks" / f"{ticker}.csv"
        if not tdir.is_dir() or not stock_csv.exists():
            continue
        try:
            sdf = data_loader.load_thetadata_stock(stock_csv)
        except Exception:
            continue
        spots = dict(zip(sdf.index.date, sdf["Close"].astype(float)))
        for f in sorted(tdir.glob("calls_*.csv")):
            if f.stat().st_size < 50:
                continue
            try:
                df = pd.read_csv(f)
            except Exception:
                continue
            if df.empty or "created" not in df.columns:
                continue
            exp = date.fromisoformat(f.stem.replace("calls_", ""))
            qd = pd.to_datetime(df["created"]).dt.date
            out = pd.DataFrame({
                "quote_date": qd, "ticker": ticker, "expiration": exp,
                "strike": pd.to_numeric(df["strike"], errors="coerce"),
                "bid": pd.to_numeric(df["bid"], errors="coerce").fillna(0.0),
                "ask": pd.to_numeric(df["ask"], errors="coerce").fillna(0.0),
                "last": pd.to_numeric(df["close"], errors="coerce").fillna(0.0),
                "volume": pd.to_numeric(df["volume"], errors="coerce")
                    .fillna(0).astype(int),
                "open_interest": 0, "iv": np.nan, "delta": np.nan,
                "underlying_price": [spots.get(d, np.nan) for d in qd],
            }).dropna(subset=["quote_date", "strike", "underlying_price"])
            if out.empty:
                continue
            oi_f = tdir / f"oi_calls_{exp.isoformat()}.csv"
            if oi_f.exists() and oi_f.stat().st_size > 50:
                try:
                    oi = pd.read_csv(oi_f)
                    oi_map = {(pd.Timestamp(t).date(), round(float(k), 4)): int(v)
                              for t, k, v in zip(oi["timestamp"], oi["strike"],
                                                 oi["open_interest"])
                              if pd.notna(t) and pd.notna(k) and pd.notna(v)}
                    out["open_interest"] = [
                        oi_map.get((d, round(float(k), 4)), 0)
                        for d, k in zip(out["quote_date"], out["strike"])]
                except Exception:
                    pass
            frames.append(out)
    if not frames:
        return pd.DataFrame(columns=data_loader.NORMALIZED_COLUMNS)
    return pd.concat(frames, ignore_index=True)[data_loader.NORMALIZED_COLUMNS]


def build(store: Path = CALL_STORE, options_subdir: str = "options") -> dict:
    opt_root = DATA / options_subdir
    tickers = sorted(p.name for p in opt_root.iterdir()
                     if p.is_dir() and any(p.glob("calls_*.csv")))
    store.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    stats = {"tickers": 0, "rows": 0, "batches": 0}
    for start in range(0, len(tickers), BATCH_TICKERS):
        batch = tickers[start:start + BATCH_TICKERS]
        frame = load_calls(batch, options_subdir)
        if frame.empty:
            continue
        qd = pd.to_datetime(frame["quote_date"])
        exp = pd.to_datetime(frame["expiration"])
        T = (exp - qd).dt.days.to_numpy() / 365.0
        S = frame["underlying_price"].to_numpy(dtype=float)
        K = frame["strike"].to_numpy(dtype=float)
        bid = frame["bid"].to_numpy(dtype=float)
        ask = frame["ask"].to_numpy(dtype=float)
        two_sided = (bid > 0) & (ask > 0)
        mid = np.where(two_sided, 0.5 * (bid + ask), np.nan)
        iv = invert_call_iv(np.nan_to_num(mid), S, K, T)
        iv[~two_sided] = np.nan
        frame["iv"] = iv
        frame["delta"] = call_delta(S, K, T, iv)
        frame = frame.drop_duplicates(
            subset=["quote_date", "ticker", "expiration", "strike"], keep="last")
        pads.write_dataset(
            pa.Table.from_pandas(frame, preserve_index=False), store,
            format="parquet",
            partitioning=pads.partitioning(
                pa.schema([("quote_date", pa.date32())]), flavor="hive"),
            basename_template=f"c{start}-{{i}}.parquet",
            existing_data_behavior="overwrite_or_ignore",
            max_partitions=8192, max_open_files=8192)
        stats["tickers"] += len(batch)
        stats["rows"] += len(frame)
        stats["batches"] += 1
        print(f"  {stats['tickers']}/{len(tickers)} tickers, "
              f"{stats['rows']:,} rows ({(time.time()-t0)/60:.1f}min)", flush=True)
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    return stats


if __name__ == "__main__":
    import argparse
    from csp_screener.backtest.day_store import DayStore
    ap = argparse.ArgumentParser()
    ap.add_argument("--options-subdir", default="options")
    ap.add_argument("--store", default=str(CALL_STORE))
    a = ap.parse_args()
    print(f"Building CALL day store from {a.options_subdir}...")
    s = build(Path(a.store), a.options_subdir)
    ds = DayStore(Path(a.store))
    print(f"CALL STORE BUILT: {s}")
    if ds.dates:
        print(f"{len(ds.dates)} trading days ({ds.dates[0]} -> {ds.dates[-1]})")
        d = ds.day(ds.dates[len(ds.dates) // 2])
        print(f"sample day: {len(d)} rows, delta range "
              f"{d['delta'].min():.3f}..{d['delta'].max():.3f}, "
              f"iv median {d['iv'].median():.3f}")
