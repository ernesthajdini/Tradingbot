"""
PHASE 5 DATA — liquid index/sector ETFs, 2017-2026.

WHY (evidence from the completed study, not a hunch): every catastrophic
loss in the 119-trade production cell was SINGLE-NAME IDIOSYNCRATIC — FRC's
bank failure (-$714), MNMD (-$330), TEVA (-$272), AMC (-$253). The six ETF
trades that did occur returned +$4.72/trade with a worst loss of -$87.
An index cannot go bankrupt, cannot be defrauded, cannot have a bank run.
Measured here: SPY/QQQ/IWM quote 4.9-5.7% wide vs a single-name universe
where ~90% of chains failed the same 5% gate — friction was the other killer.

These names sit far outside the $5-25 / $20-60 price bands, so the existing
pull never touched them. Data only; no strategy conclusion is drawn until a
pre-registered study runs (MANIFEST amendment required first).
"""
from __future__ import annotations
import sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
import requests

BASE="http://127.0.0.1:25503"
OUT=Path(__file__).resolve().parent/"data"/"thetadata_full"/"options_index"
TICKERS=["SPY","QQQ","IWM","DIA","XSP","EEM","EFA","XLF","XLE","XLU","XBI",
         "SMH","KRE","GDX","GLD","SLV","TLT","HYG","USO","UNG","FXI","EWZ",
         "ARKK","XOP","IYR","XLK","XLV","XLI","XLP","SOXX"]
START=date(2017,1,1); END=date(2026,8,28)
# 50 days covers a 45-DTE entry held to expiry, with margin.
ACTIVE=50; WORKERS=4; RETRIES=[0,5,20,60]

def is_monthly(d: date) -> bool:
    """Third-Friday standard expiration. SPY/QQQ/IWM list ~150 expirations a
    year once weeklies are counted, which makes a 10-year pull days long for
    no gain: a 25-45 DTE strategy takes the expiry nearest 35 days, and the
    monthlies are where the open interest and the tightest markets are. This
    restriction is a declared data-coverage choice, stamped in the study —
    it narrows WHICH expiries exist, never which trades are allowed."""
    return d.weekday() == 4 and 15 <= d.day <= 21

def _get(path, params, sym):
    for b in RETRIES:
        if b: time.sleep(b)
        try: r=requests.get(f"{BASE}{path}", params=params, timeout=300)
        except requests.RequestException: continue
        t=r.text; low=t[:160].lower()
        if "subscription" in low: raise RuntimeError(f"TIER WALL {sym}: {t[:120]}")
        if r.status_code==200 and (low.startswith("symbol") or low.startswith("created")): return t
        if r.status_code in (429,500,502,503,504): continue
        return ""
    return None

def pull(sym):
    d=OUT/sym; d.mkdir(parents=True, exist_ok=True)
    c={"puts":0,"oi":0,"skip":0,"empty":0,"fail":0}
    txt=_get("/v3/option/list/expirations",{"symbol":sym},sym)
    if not txt: return c
    exps=[]
    for line in txt.splitlines()[1:]:
        p=line.replace('"','').split(",")
        if len(p)>=2:
            try:
                e=date.fromisoformat(p[1])
                if START<=e<=END+timedelta(days=ACTIVE) and is_monthly(e):
                    exps.append(e)
            except ValueError: pass
    st=_get("/v3/stock/history/eod",{"symbol":sym,"start_date":START.isoformat(),
                                     "end_date":min(END,START+timedelta(days=364)).isoformat()},sym)
    parts=[]
    y=START.year
    while y<=END.year:
        t=_get("/v3/stock/history/eod",{"symbol":sym,
              "start_date":max(date(y,1,1),START).isoformat(),
              "end_date":min(date(y,12,31),END).isoformat()},sym)
        if t: parts.append("\n".join(t.splitlines() if not parts else t.splitlines()[1:]))
        y+=1
    if parts: (d/"stock_eod.csv").write_text("\n".join(parts)+"\n",encoding="utf-8")
    for e in sorted(set(exps)):
        lo=max(e-timedelta(days=ACTIVE),START); hi=min(e,END)
        if lo>hi: continue
        for kind,path in (("puts","/v3/option/history/eod"),
                          ("oi","/v3/option/history/open_interest")):
            f=d/f"{kind}_{e.isoformat()}.csv"
            if f.exists(): c["skip"]+=1; continue
            t=_get(path,{"symbol":sym,"expiration":e.isoformat(),"right":"put",
                         "start_date":lo.isoformat(),"end_date":hi.isoformat()},sym)
            if t is None: c["fail"]+=1
            elif t and len(t.splitlines())>1: f.write_text(t,encoding="utf-8"); c[kind]+=1
            else: f.write_text("",encoding="utf-8"); c["empty"]+=1
    return c

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    log=open(OUT.parent/"index_pull.log","a",encoding="utf-8",buffering=1)
    print(f"{len(TICKERS)} index/ETF tickers",file=log)
    tot={"puts":0,"oi":0,"skip":0,"empty":0,"fail":0}; t0=time.time(); done=0
    with ThreadPoolExecutor(WORKERS) as ex:
        fs={ex.submit(pull,s):s for s in TICKERS}
        for fu in as_completed(fs):
            c=fu.result(); done+=1
            for k in tot: tot[k]+=c.get(k,0)
            print(f"{done}/{len(TICKERS)} {fs[fu]} ({(time.time()-t0)/3600:.1f}h) {tot}",file=log)
    print(f"INDEX PULL DONE {tot}",file=log); print("INDEX PULL DONE")
    return 0
if __name__=="__main__": sys.exit(main())
