#!/usr/bin/env python3
"""Backtest old vs new rules on yfinance GC=F (60d 15m, 4H from 1h). 4H uses only CLOSED bars (no lookahead)."""
import os, sys, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != HERE]
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
sp = importlib.util.spec_from_file_location("gs", os.path.join(HERE, "signal.py")); gs = importlib.util.module_from_spec(sp); sp.loader.exec_module(gs)
import pandas as pd, numpy as np

h1 = gs._yf("1h", "730d")
h4 = gs.indicators(h1.resample("4h", origin="epoch").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna())
d15 = gs.indicators(gs._yf("15m", "60d"))

for _d in (h4, d15): _d.index = _d.index.tz_convert("UTC").as_unit("ns")

def dirs(d, tf, strict):
    return np.array([gs.direction(r, tf, strict) for r in d.itertuples()])

def run(strict, session):
    d4 = h4.copy(); d4["dir"] = dirs(d4, "4h", strict); d4.index = d4.index + pd.Timedelta(hours=4)  # available after close
    m = d15.copy(); m["dir15"] = dirs(m, "15m", strict)
    m = pd.merge_asof(m.sort_index(), d4[["dir"]].sort_index(), left_index=True, right_index=True)
    m["side"] = np.where((m.dir == m.dir15) & (m.dir != 0), m.dir, 0)
    if session:
        h = m.index.tz_convert(gs.TZ).hour
        m.loc[(h < gs.SESSION[0]), "side"] = 0
    res = []; prev = None
    rows = list(m.itertuples())
    for i, r in enumerate(rows):
        key = (r.side, r.cross_id) if r.side else None
        if i < 200 or key == prev: prev = key; continue
        prev = key
        if not key: continue
        lv = gs.levels(m.iloc[: i + 1], r.side); s = r.side; tp1 = False; out = None
        for b in rows[i + 1:]:
            stop = lv["entry"] if tp1 else lv["sl"]
            if (s > 0 and b.Low <= stop) or (s < 0 and b.High >= stop): out = 0.5 if tp1 else -1.0; break
            if not tp1 and ((s > 0 and b.High >= lv["tp1"]) or (s < 0 and b.Low <= lv["tp1"])): tp1 = True
            if tp1 and ((s > 0 and b.High >= lv["tp2"]) or (s < 0 and b.Low <= lv["tp2"])): out = 1.5; break
        # simple fixed outcomes too
        def first(target):
            for b in rows[i + 1:]:
                if (s > 0 and b.Low <= lv["sl"]) or (s < 0 and b.High >= lv["sl"]): return -1
                if (s > 0 and b.High >= target) or (s < 0 and b.Low <= target): return 1
            return 0
        res.append(dict(t=r.Index, side=s, mgmt=out, tp1=first(lv["tp1"]), tp2=first(lv["tp2"])))
    return pd.DataFrame(res)

ALL = set(gs.FILTERS)
if "--ablate" in sys.argv:
    for f in sorted(ALL) + ["NONE"]:
        for only in (False, True):
            gs.FILTERS = ALL - {f} if not only else ({f} if f != "NONE" else set())
            for sess in (False, True):
                r = run(True, sess); c = r[r.mgmt.notna()]
                print(f"{'only' if only else 'without'} {f:8s} sess={sess}: n={len(r)} TP1%={(r.tp1>0).mean()*100:.0f} totR={c.mgmt.sum():+.1f} avgR={c.mgmt.mean():+.2f}")
    sys.exit()
for name, strict, sess in [("OLD (live rules before today)", False, False), ("NEW strict, no session", True, False), ("NEW strict + session 10-24", True, True)]:
    r = run(strict, sess)
    if r.empty: print(f"{name}: 0 signals"); continue
    c = r[r.mgmt.notna()]
    t1, t2 = r[r.tp1 != 0], r[r.tp2 != 0]
    print(f"{name}: signals={len(r)} (buy {sum(r.side>0)}/sell {sum(r.side<0)})\n"
          f"   TP1-before-SL: {(t1.tp1>0).mean()*100:.0f}% of {len(t1)}   avgR(exit TP1)={t1.tp1.mean():+.2f}\n"
          f"   TP2-before-SL: {(t2.tp2>0).mean()*100:.0f}% of {len(t2)}   avgR(exit TP2)={(t2.tp2*np.where(t2.tp2>0,2,1)).mean():+.2f}\n"
          f"   managed (½@TP1, BE, ½@TP2): closed={len(c)} totalR={c.mgmt.sum():+.1f} avgR={c.mgmt.mean():+.2f} wins={sum(c.mgmt>0)} losses={sum(c.mgmt<0)}")
print("period:", d15.index[0], "->", d15.index[-1])
