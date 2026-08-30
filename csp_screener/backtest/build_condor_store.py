"""
Day store for AMENDMENT 5 — puts AND calls in one partitioned store.

The existing store carries puts only (the schema has no `right` column
because nothing before this needed one). This builds daystore_condor with
both sides, each row tagged P or C, IV inverted against the matching
Black-Scholes pricer and delta signed per side:

    put  delta = -N(-d1)      (production's estimator)
    call delta = +N(d1)

Vectorized, same bisection contract as day_store.invert_iv — NaN where the
mid is at or below intrinsic, or unreachable at the 300% hygiene ceiling.

    python csp_screener/backtest/build_condor_store.py
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
from csp_screener.backtest.day_store import IV_HI, IV_LO, IV_ITERS, bs_put

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
SRC = DATA / "options_index"
STORE = DATA / "daystore_condor"
COLUMNS = ["quote_date", "ticker", "expiration", "right", "strike",
           "bid", "ask", "last", "volume", "open_interest", "iv", "delta",
           "underlying_price"]


def bs_call(S, K, T, sigma):
    """Black-Scholes call, rate 0 (put-call symmetry of the same model)."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    out = np.maximum(S - K, 0.0)
    ok = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    if ok.any():
        s, k, t, sig = S[ok], K[ok], T[ok], sigma[ok]
        sq = sig * np.sqrt(t)
        d1 = (np.log(s / k) + 0.5 * sig * sig * t) / sq
        out[ok] = np.maximum(s * ndtr(d1) - k * ndtr(d1 - sq), 0.0)
    return out


def invert(mid, S, K, T, pricer, intrinsic):
    mid, S, K, T = (np.asarray(x, dtype=float) for x in (mid, S, K, T))
    iv = np.full(mid.shape[0], np.nan)
    live = (mid > 0) & (S > 0) & (K > 0) & (T > 0) & (mid > intrinsic + 1e-6)
    if not live.any():
        return iv
    top = pricer(S[live], K[live], T[live], np.full(int(live.sum()), IV_HI))
    idx = np.flatnonzero(live)[top >= mid[live]]
    if idx.size == 0:
        return iv
    s, k, t, tgt = S[idx], K[idx], T[idx], mid[idx]
    lo = np.full(idx.size, IV_LO)
    hi = np.full(idx.size, IV_HI)
    for _ in range(IV_ITERS):
        m = 0.5 * (lo + hi)
        below = pricer(s, k, t, m) < tgt
        lo = np.where(below, m, lo)
        hi = np.where(below, hi, m)      # the bisection fix, verified
    iv[idx] = np.round(0.5 * (lo + hi), 4)
    return iv


def read_side(tdir: Path, prefix: str, right: str, spots: dict):
    out = []
    for f in sorted(tdir.glob(f"{prefix}_*.csv")):
        if f.stat().st_size < 50:
            continue
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        if df.empty or "created" not in df.columns:
            continue
        exp = date.fromisoformat(f.stem.replace(f"{prefix}_", ""))
        qd = pd.to_datetime(df["created"]).dt.date
        frame = pd.DataFrame({
            "quote_date": qd, "ticker": tdir.name, "expiration": exp,
            "right": right,
            "strike": pd.to_numeric(df["strike"], errors="coerce"),
            "bid": pd.to_numeric(df["bid"], errors="coerce").fillna(0.0),
            "ask": pd.to_numeric(df["ask"], errors="coerce").fillna(0.0),
            "last": pd.to_numeric(df["close"], errors="coerce").fillna(0.0),
            "volume": pd.to_numeric(df["volume"], errors="coerce")
                .fillna(0).astype(int),
            "open_interest": 0,
            "underlying_price": [spots.get(d, np.nan) for d in qd],
        })
        oi_f = tdir / f"oi_{'calls' if right == 'C' else ''}{exp.isoformat()}.csv"
        alt = tdir / (f"oi_calls_{exp.isoformat()}.csv" if right == "C"
                      else f"oi_{exp.isoformat()}.csv")
        for cand in (alt, oi_f):
            if cand.exists() and cand.stat().st_size > 50:
                try:
                    oi = pd.read_csv(cand)
                    m = {(pd.Timestamp(t).date(), round(float(k), 4)): int(v)
                         for t, k, v in zip(oi["timestamp"], oi["strike"],
                                            oi["open_interest"])
                         if pd.notna(t) and pd.notna(k) and pd.notna(v)}
                    frame["open_interest"] = [
                        m.get((d, round(float(k), 4)), 0)
                        for d, k in zip(frame["quote_date"], frame["strike"])]
                except Exception:
                    pass
                break
        out.append(frame.dropna(subset=["quote_date", "strike",
                                        "underlying_price"]))
    return out


def main() -> int:
    t0 = time.time()
    STORE.mkdir(parents=True, exist_ok=True)
    tickers = sorted(p for p in SRC.iterdir() if p.is_dir())
    total = 0
    for i, tdir in enumerate(tickers, 1):
        sf = tdir / "stock_eod.csv"
        if not sf.exists():
            continue
        sdf = data_loader.load_thetadata_stock(sf)
        spots = dict(zip(sdf.index.date, sdf["Close"].astype(float)))
        frames = (read_side(tdir, "puts", "P", spots)
                  + read_side(tdir, "calls", "C", spots))
        if not frames:
            continue
        frame = pd.concat(frames, ignore_index=True)
        frame = frame.drop_duplicates(
            subset=["quote_date", "ticker", "expiration", "right", "strike"],
            keep="last")
        two = (frame["bid"] > 0) & (frame["ask"] > 0)
        mid = np.where(two, 0.5 * (frame["bid"] + frame["ask"]), np.nan)
        S = frame["underlying_price"].to_numpy(float)
        K = frame["strike"].to_numpy(float)
        T = ((pd.to_datetime(frame["expiration"])
              - pd.to_datetime(frame["quote_date"])).dt.days.to_numpy() / 365.0)
        isP = (frame["right"] == "P").to_numpy()
        iv = np.full(len(frame), np.nan)
        if isP.any():
            iv[isP] = invert(np.nan_to_num(mid)[isP], S[isP], K[isP], T[isP],
                             bs_put, np.maximum(K[isP] - S[isP], 0.0))
        if (~isP).any():
            iv[~isP] = invert(np.nan_to_num(mid)[~isP], S[~isP], K[~isP],
                              T[~isP], bs_call,
                              np.maximum(S[~isP] - K[~isP], 0.0))
        iv[~two] = np.nan
        frame["iv"] = iv
        d1 = np.full(len(frame), np.nan)
        ok = (T > 0) & (iv > 0) & (S > 0) & (K > 0) & np.isfinite(iv)
        if ok.any():
            d1[ok] = ((np.log(S[ok] / K[ok]) + 0.5 * iv[ok] ** 2 * T[ok])
                      / (iv[ok] * np.sqrt(T[ok])))
        delta = np.full(len(frame), np.nan)
        delta[ok & isP] = -ndtr(-d1[ok & isP])
        delta[ok & ~isP] = ndtr(d1[ok & ~isP])
        frame["delta"] = delta

        pads.write_dataset(
            pa.Table.from_pandas(frame[COLUMNS], preserve_index=False),
            STORE, format="parquet",
            partitioning=pads.partitioning(
                pa.schema([("quote_date", pa.date32())]), flavor="hive"),
            basename_template=f"t{i}-{{i}}.parquet",
            existing_data_behavior="overwrite_or_ignore",
            max_partitions=8192, max_open_files=8192)
        total += len(frame)
        if i % 5 == 0 or i == len(tickers):
            print(f"  {i}/{len(tickers)} tickers, {total:,} rows "
                  f"({(time.time()-t0)/60:.1f}min)", flush=True)
    print(f"CONDOR STORE BUILT: {total:,} rows in {(time.time()-t0)/60:.1f}min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
