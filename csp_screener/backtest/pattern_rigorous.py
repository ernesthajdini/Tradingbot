import sys, json
from datetime import date
from pathlib import Path
sys.path.insert(0,'.')
import numpy as np, pandas as pd
from scipy import stats
from csp_screener.backtest.pattern_study import load_ohlc, patterns, MIN_PRICE, MIN_VOL, TRAIN, VALID
DATA=Path("csp_screener/backtest/data/thetadata_full")
FOCUS=["gap_up_3pct","high_52w"]; HZ=21
files=sorted((DATA/"stocks").glob("*.csv"))
# NON-OVERLAPPING: keep at most one observation per stock per 21 sessions,
# and record the date so we can also cluster by day.
rec={w:{k:[] for k in FOCUS+["__BASE__"]} for w in ("train","valid")}
for i,p in enumerate(files,1):
    try: df=load_ohlc(p)
    except Exception: continue
    if len(df)<300: continue
    elig=(df["c"]>=MIN_PRICE)&(df["v"].shift(1).rolling(20).mean()>=MIN_VOL)
    pats=patterns(df); fwd=df["c"].shift(-HZ)/df["c"]-1.0
    d=df.index.date
    for wn,(a,b) in (("train",TRAIN),("valid",VALID)):
        win=elig&(d>=a)&(d<=b)
        if not win.any(): continue
        for name in FOCUS+["__BASE__"]:
            m=win if name=="__BASE__" else (win & pats[name].fillna(False))
            idx=np.flatnonzero(m.to_numpy())
            keep=[]; last=-99
            for j in idx:                      # enforce 21-session spacing
                if j-last>=HZ: keep.append(j); last=j
            for j in keep:
                r=fwd.iloc[j]
                if np.isfinite(r): rec[wn][name].append((d[j], float(r)))
    if i%3000==0: print(f"  {i}/{len(files)}", flush=True)

def clustered(pairs):
    """Mean with standard error clustered by DATE — all stocks share the
    market factor on a given day, so day is the unit of independence."""
    df=pd.DataFrame(pairs, columns=["d","r"])
    g=df.groupby("d")["r"].mean()
    return float(df["r"].mean()), float(g.std(ddof=1)/np.sqrt(len(g))), len(df), len(g)

print()
for wn in ("train","valid"):
    bm,bse,bn,bd=clustered(rec[wn]["__BASE__"])
    print(f"=== {wn.upper()}: base rate {1e4*bm:+.0f}bp over {HZ}d "
          f"({bn:,} non-overlapping obs on {bd:,} days) ===")
    for name in FOCUS:
        m,se,n,nd=clustered(rec[wn][name])
        edge=m-bm
        t=edge/np.sqrt(se**2+bse**2)
        p=2*(1-stats.norm.cdf(abs(t)))
        print(f"  {name:14} n={n:6,} on {nd:4,} days | absolute {1e4*m:+8.0f}bp "
              f"| edge {1e4*edge:+7.0f}bp | t={t:5.2f} p={p:.4f} "
              f"| net of 83bp costs: {1e4*m-83:+.0f}bp")
