#!/usr/bin/env python3
"""Gold Ichimoku + RSI signal engine (shared by watcher, listener, backtest).
  signal.py            -> analyze + send now
  signal.py --watch    -> cron watcher: news alerts, trade tracking, new-signal send (dedup)
  signal.py --weekly   -> weekly performance summary
  add --dry to print only."""
import os, sys
# file is named signal.py: drop own dir from path so stdlib "signal" is not shadowed
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path = [x for x in sys.path if os.path.abspath(x or ".") != HERE]
import json, time, pickle, importlib.util, datetime as dt, requests, pandas as pd, numpy as np, yfinance as yf
from zoneinfo import ZoneInfo
import matplotlib; matplotlib.use("Agg")
import mplfinance as mpf, matplotlib.pyplot as plt

_ns = importlib.util.spec_from_file_location("gold_news", os.path.join(HERE, "news.py"))
news = importlib.util.module_from_spec(_ns); _ns.loader.exec_module(news)

TZ = ZoneInfo("Europe/Istanbul")
CHART = os.path.join(HERE, "chart.png")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or open("/workspace/tg_chat_id").read().strip()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
STATE = os.path.join(HERE, "state.json")
TRADES = os.path.join(HERE, "trades.json")
CACHE = os.path.join(HERE, "cache")
CROSS_LOOKBACK = 3
SWING_BARS, ATR_BUF, MIN_ATR, MAX_ATR = 20, 0.5, 1.0, 4.0
# quality filters (strict mode)
CLOUD_DIST_ATR = 0.25      # 4H close must be >= this many 4H-ATRs beyond the cloud edge
RSI_MAX_BUY, RSI_MIN_SELL = 75, 25
MIN_ATR_RATIO = 0.7        # 15m ATR vs its 200-bar median
SESSION = (0, 24)         # Istanbul hours for watcher signals
STRICT = True
FILTERS = {"chikou", "rsi_ext", "atr"}  # dist & fut disabled: hurt in backtest # active strict filters

# ---------------- data ----------------
def _td_get(interval, size):
    """Twelve Data XAU/USD with file cache + simple rate guard (free tier 8/min, 800/day)."""
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key: return None
    os.makedirs(CACHE, exist_ok=True)
    cf = os.path.join(CACHE, f"td_{interval}.pkl")
    ttl = 90 if interval == "15min" else 300
    if os.path.exists(cf) and time.time() - os.path.getmtime(cf) < ttl:
        return pickle.load(open(cf, "rb"))
    log = os.path.join(CACHE, "td_calls.json")
    calls = [t for t in (json.load(open(log)) if os.path.exists(log) else []) if time.time() - t < 86400]
    if sum(time.time() - t < 60 for t in calls) >= 7 or len(calls) >= 780:
        return pickle.load(open(cf, "rb")) if os.path.exists(cf) else None
    try:
        j = requests.get("https://api.twelvedata.com/time_series", timeout=30, params=dict(
            symbol="XAU/USD", interval=interval, outputsize=size, timezone="UTC", apikey=key)).json()
        calls.append(time.time()); json.dump(calls, open(log, "w"))
        if j.get("status") != "ok": raise RuntimeError(j.get("message"))
        d = pd.DataFrame(j["values"])
        d.index = pd.to_datetime(d.pop("datetime")).dt.tz_localize("UTC")
        d = d.rename(columns=str.capitalize).astype(float).sort_index()
        if "Volume" not in d: d["Volume"] = 0.0
        d = d[["Open","High","Low","Close","Volume"]]
        pickle.dump(d, open(cf, "wb")); return d
    except Exception as e:
        print("twelvedata failed:", e, file=sys.stderr)
        return pickle.load(open(cf, "rb")) if os.path.exists(cf) else None

def _yf(interval, period):
    d = yf.download("GC=F", interval=interval, period=period, progress=False, auto_adjust=False)
    if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
    return d[["Open","High","Low","Close","Volume"]].dropna()

def get_data():
    """-> (d4 raw, d15 raw, source label)"""
    h4, m15 = _td_get("4h", 600), _td_get("15min", 1500)
    if h4 is not None and m15 is not None and len(h4) > 120 and len(m15) > 300:
        return h4, m15, "XAU/USD · Twelve Data"
    h1 = _yf("1h", "730d")
    h4 = h1.resample("4h", origin="epoch").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    return h4, _yf("15m", "60d"), "GC=F · Yahoo (تأخیر ~۱۵ دقیقه)"

# ---------------- indicators / rules ----------------
def indicators(d):
    d = d.copy(); h, l, c = d.High, d.Low, d.Close
    mid = lambda n: (h.rolling(n).max() + l.rolling(n).min()) / 2
    d["tenkan"], d["kijun"] = mid(9), mid(26)
    d["futA"], d["futB"] = (d.tenkan + d.kijun) / 2, mid(52)      # cloud 26 bars ahead
    d["spanA"], d["spanB"] = d.futA.shift(26), d.futB.shift(26)    # cloud under current bar
    delta = c.diff(); up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn)
    d["rsi_ma"] = d.rsi.rolling(9).mean()  # SMA(9) of RSI = MT "MA on previous indicator data"
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    d["atr_ratio"] = d.atr / d.atr.rolling(200, min_periods=50).median()
    top, bot = d[["spanA","spanB"]].max(axis=1), d[["spanA","spanB"]].min(axis=1)
    d["tk"] = np.sign(d.tenkan - d.kijun)
    d["cloud"] = np.where(c > top, 1, np.where(c < bot, -1, 0))
    d["cloud_dist"] = np.where(c > top, (c - top) / d.atr, np.where(c < bot, (bot - c) / d.atr, 0.0))
    d["fut"] = np.sign(d.futA - d.futB)
    d["chikou"] = np.where(c > h.shift(26), 1, np.where(c < l.shift(26), -1, 0))  # lagging span vs price 26 bars ago
    d["mapos"] = np.sign(d.rsi - d.rsi_ma)
    d["cross_id"] = (d.tk != d.tk.shift()).cumsum()
    return d

def checks(r, tf, side, strict=STRICT):
    """Ordered list of (Farsi label, passed) for a timeframe ('4h'/'15m') and side (+1/-1)."""
    b = side > 0
    c = [("تنکن " + ("بالای" if b else "زیر") + " کیجون", r.tk == side),
         ("قیمت " + ("بالای" if b else "زیر") + " ابر", r.cloud == side),
         (f"RSI {'بالای ۵۵' if b else 'زیر ۴۵'}", r.rsi > 55 if b else r.rsi < 45)]
    if tf == "15m":
        c.append((f"RSI {'بالای' if b else 'زیر'} مووینگ ۹", r.mapos == side))
    if strict and tf == "4h":
        c += [x for f, x in [("dist", (f"فاصلهٔ کافی از ابر (≥{CLOUD_DIST_ATR} ATR)", r.cloud == side and r.cloud_dist >= CLOUD_DIST_ATR)),
              ("chikou", ("چیکو (خط تأخیری) تأیید می‌کنه", r.chikou == side)),
              ("fut", ("ابر آینده هم‌جهت", r.fut == side))] if f in FILTERS]
    if strict and tf == "15m":
        c += [x for f, x in [("rsi_ext", ("RSI اشباع نیست" + (" (&lt;۷۵)" if b else " (&gt;۲۵)"), r.rsi < RSI_MAX_BUY if b else r.rsi > RSI_MIN_SELL)),
              ("atr", ("بازار نوسان کافی داره (ATR)", r.atr_ratio >= MIN_ATR_RATIO))] if f in FILTERS]
    return c

def direction(r, tf, strict=STRICT):
    for side in (1, -1):
        if all(ok for _, ok in checks(r, tf, side, strict)): return side
    return 0

def recent(series, side, n=CROSS_LOOKBACK):
    s = series.dropna()
    return any(s.iloc[-i] == side and s.iloc[-i-1] != side for i in range(1, n + 1) if len(s) > i)

# ---------------- text ----------------
FA_DIR = {1: "صعودی 📈", -1: "نزولی 📉", 0: "خنثی ⏸"}
def ic(ok): return "✅" if ok else "❌"

def block(title, d, tf, dirn, lean=None):
    r = d.iloc[-1]
    side = dirn or lean or (int(r.tk) or 1)  # show checks for the direction it leans to
    lines = [f"<b>{title}</b>"]
    for lab, ok in checks(r, tf, side): lines.append(f"• {lab} {ic(ok)}")
    lines.append(f"• RSI: {r.rsi:.1f} | مووینگ ۹: {r.rsi_ma:.1f}")
    if recent(d.tk, side): lines.append("• کراس تازهٔ تنکن/کیجون ⚡")
    if tf == "15m" and recent(d.mapos, side): lines.append("• کراس تازهٔ RSI با مووینگ ۹ ⚡")
    lines.append(f"• وضعیت: {FA_DIR[dirn]}")
    return "\n".join(lines)

def levels(d15, side):
    r = d15.iloc[-1]; atr = r.atr; e = r.Close; w = d15.tail(SWING_BARS)
    if side > 0:
        sl = min(w.Low.min(), r.kijun) - ATR_BUF * atr
        risk = min(max(e - sl, MIN_ATR * atr), MAX_ATR * atr)
        return dict(entry=e, sl=e - risk, tp1=e + risk, tp2=e + 2 * risk, atr=atr)
    sl = max(w.High.max(), r.kijun) + ATR_BUF * atr
    risk = min(max(sl - e, MIN_ATR * atr), MAX_ATR * atr)
    return dict(entry=e, sl=e + risk, tp1=e - risk, tp2=e - 2 * risk, atr=atr)

def in_session(now=None):
    h = (now or dt.datetime.now(TZ)).hour
    return SESSION[0] <= h < SESSION[1]

def analyze():
    h4, m15, src = get_data()
    d4, d15 = indicators(h4), indicators(m15)
    dir4, dir15 = direction(d4.iloc[-1], "4h"), direction(d15.iloc[-1], "15m")
    side = dir4 if dir4 and dir15 == dir4 else 0
    lv = levels(d15, side) if side else None
    now = dt.datetime.now(TZ)
    bo = news.blackout(now)
    if side:
        v = ("🟢 <b>خرید (BUY)</b>" if side > 0 else "🔴 <b>فروش (SELL)</b>") + " — همهٔ فیلترها تأیید شد."
        if recent(d15.tk, side): v += " ⚡ کراس تازه روی ۱۵ دقیقه."
        if recent(d15.mapos, side): v += " ⚡ کراس تازهٔ RSI/MA9."
        v += (f"\n\n🎯 <b>ورود:</b> {lv['entry']:.2f}\n🛑 <b>حد ضرر:</b> {lv['sl']:.2f}\n"
              f"✅ <b>تارگت ۱ (1R):</b> {lv['tp1']:.2f}\n✅ <b>تارگت ۲ (2R):</b> {lv['tp2']:.2f}\n"
              f"📏 ریسک: {abs(lv['entry'] - lv['sl']):.2f} دلار")
    elif not dir4:
        lean = int(d4.iloc[-1].tk) or 1
        bad = [lab for lab, ok in checks(d4.iloc[-1], "4h", lean) if not ok]
        v = "⏳ <b>صبر کن</b> — روند ۴ ساعته تأیید نشده: " + "، ".join(bad[:3]) + "."
    else:
        bad = [lab for lab, ok in checks(d15.iloc[-1], "15m", dir4) if not ok]
        v = f"⏳ <b>صبر کن</b> — روند ۴ ساعته {FA_DIR[dir4]}ه، ولی ۱۵ دقیقه هنوز آماده نیست: " + "، ".join(bad[:3]) + "."
    if bo:
        t, e, _ = bo[0]
        v += f"\n\n📰⚠️ خبر مهم {e['title']} ساعت {t:%H:%M} — از ۳۰ دقیقه قبل تا ۱۵ دقیقه بعدش سیگنال جدید نمی‌دم."
    msg = (f"🪙 <b>تحلیل طلا</b> ({src})\n💰 قیمت: <b>{d15.Close.iloc[-1]:.2f}</b>\n\n"
           f"{block('📊 تایم ۴ ساعته (جهت)', d4, '4h', dir4)}\n\n{block('⏱ تایم ۱۵ دقیقه (ورود)', d15, '15m', dir15, dir4 or int(d4.iloc[-1].tk) or 1)}\n\n"
           f"<b>نتیجه:</b> {v}\n\n⚠️ این پیام توصیه مالی نیست؛ فقط تحلیل خودکار اندیکاتورهاست.")
    age = (pd.Timestamp.now(tz="UTC") - d15.index[-1].tz_convert("UTC")).total_seconds()
    return dict(msg=msg, side=side, lv=lv, d4=d4, d15=d15, age=age, src=src, blackout=bo,
                session=in_session(now), key=f"{side}|{d15.cross_id.iloc[-1]}|{d15.index[d15.cross_id == d15.cross_id.iloc[-1]][0]}")

# ---------------- chart ----------------
def plot(d4, d15, lv=None, path=None):
    fig = plt.figure(figsize=(13, 11))
    g = fig.add_gridspec(4, 1, height_ratios=[3, 1, 3, 1], hspace=0.35)
    for k, (d, title) in enumerate([(d4.tail(120), "XAU 4H"), (d15.tail(160), "XAU 15m")]):
        ax, axr = fig.add_subplot(g[2*k]), fig.add_subplot(g[2*k+1])
        aps = [mpf.make_addplot(d.tenkan, ax=ax, color="blue", width=1),
               mpf.make_addplot(d.kijun, ax=ax, color="red", width=1),
               mpf.make_addplot(d.spanA, ax=ax, color="green", width=0.7),
               mpf.make_addplot(d.spanB, ax=ax, color="orange", width=0.7),
               mpf.make_addplot(d.rsi, ax=axr, color="purple", ylabel="RSI"),
               mpf.make_addplot(d.rsi_ma, ax=axr, color="goldenrod", width=1)]
        mpf.plot(d, type="candle", ax=ax, addplot=aps, style="yahoo", xrotation=0)
        x = np.arange(len(d)); a, b = d.spanA.values, d.spanB.values
        ax.fill_between(x, a, b, where=a >= b, color="green", alpha=0.15)
        ax.fill_between(x, a, b, where=a < b, color="red", alpha=0.15)
        axr.axhline(55, color="green", ls="--", lw=0.8); axr.axhline(45, color="red", ls="--", lw=0.8)
        if lv:
            for key, col in [("entry", "black"), ("sl", "red"), ("tp1", "green"), ("tp2", "darkgreen")]:
                ax.axhline(lv[key], color=col, ls=":", lw=1.2)
                ax.text(len(d) - 1, lv[key], f" {key.upper()} {lv[key]:.1f}", color=col, fontsize=8, va="bottom", ha="right")
        axr.set_ylim(0, 100); ax.set_title(title + "  (Tenkan blue, Kijun red, cloud; RSI purple, MA9 gold)")
    fig.savefig(path or CHART, dpi=110, bbox_inches="tight"); plt.close(fig)

# ---------------- telegram ----------------
DRY = "--dry" in sys.argv
def tg(method, **kw):
    if DRY: print(f"[dry] {method}: {str(kw.get("data", {}).get("text", ""))}"); return
    j = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", timeout=60, **kw).json()
    if not j.get("ok"): raise RuntimeError(f"Telegram {method} failed: {j.get('description')}")

def say(text): tg("sendMessage", data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})

def send_analysis(a, path=None):
    plot(a["d4"], a["d15"], a["lv"], path)
    if not DRY:
        with open(path or CHART, "rb") as f:
            tg("sendPhoto", data={"chat_id": CHAT_ID, "caption": "چارت طلا: ۴ ساعته و ۱۵ دقیقه"}, files={"photo": f})
    say(a["msg"])

# ---------------- state helpers ----------------
def load(p, default): return json.load(open(p)) if os.path.exists(p) else default
def save(p, v):
    if not DRY: json.dump(v, open(p, "w"), ensure_ascii=False, indent=1, default=str)

# ---------------- watcher parts ----------------
from itertools import groupby
def _groups():
    """[(time, [(event, is_top), ...])] grouped by release time."""
    return [(t, [(e, top) for _, e, top in g]) for t, g in groupby(news.high_usd(), key=lambda x: x[0])]

def _fp(e):
    return " | ".join(x for x in [f"پیش‌بینی: {e['forecast']}" if e.get("forecast") else "",
                                  f"قبلی: {e['previous']}" if e.get("previous") else ""] if x)

def news_alerts(st):
    now = dt.datetime.now(TZ); sent = set(st.get("news_alerted", []))
    for t, evs in _groups():
        mins = (t - now).total_seconds() / 60
        new = [(e, top) for e, top in evs if f"{e['title']}|{e['date']}" not in sent]
        if not (0 < mins <= 35) or not new: continue
        lines = [f"📰⚠️ <b>{'🔥 ' if any(tp for _, tp in evs) else ''}{'خبر' if len(evs) == 1 else f'{len(evs)} خبر'} مهم دلار تا {int(mins)} دقیقه دیگه</b> (⏰ {t:%H:%M})"]
        for e, top in evs:
            lines.append(f"\n{'🔥 ' if top else '• '}<b>{e['title']}</b>" + (f"\n   {_fp(e)}" if _fp(e) else ""))
        lines.append(f"\nطلا ممکنه نوسان شدید داشته باشه. از ۳۰ دقیقه قبل تا ۱۵ دقیقه بعد سیگنال جدید نمی‌دم؛ اگه معامله باز داری، حد ضررت رو چک کن.")
        say("\n".join(lines))
        sent.update(f"{e['title']}|{e['date']}" for e, _ in evs)
    st["news_alerted"] = sorted(sent)[-80:]

POST_MAX_H = float(os.environ.get("NEWS_POST_MAX_H", "3"))
def post_news(st, d15):
    """After each release time: actual vs forecast per event (TradingView calendar) + one shared observed 15m gold move."""
    now = dt.datetime.now(TZ); done = set(st.get("post_alerted", []))
    for t, evs in _groups():
        keys = [f"{e['title']}|{e['date']}" for e, _ in evs]
        age_h = (now - t).total_seconds() / 3600
        if all(k in done for k in keys) or age_h < 16 / 60 or age_h > POST_MAX_H: continue
        tu = pd.Timestamp(t).tz_convert("UTC")
        bar = d15[(d15.index >= tu) & (d15.index < tu + pd.Timedelta(minutes=15))]
        if bar.empty or d15.index[-1] <= bar.index[0]: continue
        acts = [(e, top, news.find_actual(e["title"], t.astimezone(dt.timezone.utc))) for e, top in evs]
        if any(a is None and news.kind(e["title"]) != "speech" for e, _, a in acts) and age_h < 1: continue
        lines = [f"📰✅ <b>{'🔥 ' if any(tp for _, tp in evs) else ''}نتیجهٔ {'خبر' if len(evs) == 1 else f'{len(evs)} خبر'} ساعت {t:%H:%M}</b>"]
        bias = 0
        for e, top, act in acts:
            lines.append(f"\n{'🔥 ' if top else '• '}<b>{e['title']}</b>")
            if act:
                u = "%" if act["unit"] == "%" else ""
                fc = act["forecast"] if act["forecast"] is not None else news._num(e.get("forecast"))
                lines.append(f"   واقعی: <b>{act['actual']}{u}</b> | پیش‌بینی: {fc if fc is not None else '—'}{u if fc is not None else ''} | قبلی: {e.get('previous') or '—'}")
            elif news.kind(e["title"]) == "speech":
                lines.append("   سخنرانی/بیانیه؛ عدد نداره.")
            else:
                lines.append("   عدد واقعی پیدا نشد.")
        mv = bar.Close.iloc[0] - bar.Open.iloc[0]
        lines.append(f"\n<b>واکنش واقعی طلا در ۱۵ دقیقهٔ اول:</b> {'📈 صعودی' if mv > 0 else '📉 نزولی' if mv < 0 else 'بی‌تغییر'} {mv:+.2f} دلار "
                     f"({bar.Open.iloc[0]:.2f} ← {bar.Close.iloc[0]:.2f}) | سقف/کف: {bar.High.iloc[0]:.2f} / {bar.Low.iloc[0]:.2f}")
        say("\n".join(lines)); done.update(keys)
    st["post_alerted"] = sorted(done)[-80:]

def track(d15):
    """Walk 15m bars since each open trade's entry; SL first if a bar touches both (conservative)."""
    trades = load(TRADES, [])
    for t in trades:
        if t["status"] == "closed": continue
        s = 1 if t["side"] > 0 else -1
        bars = d15[d15.index > pd.Timestamp(t["bar"])]
        if t.get("last_checked"): bars = bars[bars.index > pd.Timestamp(t["last_checked"])]
        name = "خرید" if s > 0 else "فروش"
        for ts, b in bars.iterrows():
            lo, hi = b.Low, b.High
            stop = t["entry"] if t["tp1_hit"] else t["sl"]
            if (s > 0 and lo <= stop) or (s < 0 and hi >= stop):
                t.update(status="closed", closed_at=str(ts), R=0.5 if t["tp1_hit"] else -1.0)
                say(f"{'⚪️' if t['tp1_hit'] else '🛑'} سیگنال {name} از {t['entry']:.2f}: "
                    + ("بعد از تارگت ۱ به نقطهٔ ورود برگشت و بسته شد (+0.5R در کل)." if t["tp1_hit"] else f"حد ضرر {t['sl']:.2f} خورد (-1R)."))
                break
            if not t["tp1_hit"] and ((s > 0 and hi >= t["tp1"]) or (s < 0 and lo <= t["tp1"])):
                t["tp1_hit"] = True
                say(f"✅ سیگنال {name} از {t['entry']:.2f}: <b>تارگت ۱ ({t['tp1']:.2f}) خورد</b> (+1R).\n"
                    f"پیشنهاد: نصف حجم رو ببند و حد ضرر رو ببر روی نقطهٔ ورود ({t['entry']:.2f}).")
            if t["tp1_hit"] and ((s > 0 and hi >= t["tp2"]) or (s < 0 and lo <= t["tp2"])):
                t.update(status="closed", closed_at=str(ts), R=1.5)
                say(f"🏆 سیگنال {name} از {t['entry']:.2f}: <b>تارگت ۲ ({t['tp2']:.2f}) خورد</b> (+2R روی نیمهٔ دوم، +1.5R در کل).")
                break
            t["last_checked"] = str(ts)
    save(TRADES, trades)

def watch():
    st = load(STATE, {})
    news_alerts(st); save(STATE, st)
    a = analyze()
    post_news(st, a["d15"]); save(STATE, st)
    track(a["d15"])
    print(f"[{dt.datetime.now(TZ):%F %H:%M}] side={a['side']} key={a['key']} prev={st.get('key')} "
          f"session={a['session']} blackout={bool(a['blackout'])} age={a['age']:.0f}s src={a['src']}")
    if a["age"] > 3600: print("data stale / market closed"); return
    key = a["key"] if a["side"] else "none"
    if key == st.get("key"): return
    if a["side"] and not a["session"]: print("signal held: outside session"); return
    if a["side"] and a["blackout"]: print("signal held: news blackout"); return
    st["key"] = key; save(STATE, st)
    if not a["side"]: return
    send_analysis(a)
    lv = a["lv"]; trades = load(TRADES, [])
    trades.append(dict(id=len(trades) + 1, side=a["side"], time=str(dt.datetime.now(TZ)), bar=str(a["d15"].index[-1]),
                       entry=lv["entry"], sl=lv["sl"], tp1=lv["tp1"], tp2=lv["tp2"], src=a["src"],
                       status="open", tp1_hit=False))
    save(TRADES, trades); print("SIGNAL SENT")

def weekly():
    now = dt.datetime.now(TZ); start = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    ts = [t for t in load(TRADES, []) if dt.datetime.fromisoformat(t["time"]) >= start]
    closed = [t for t in ts if t["status"] == "closed"]
    wins = sum(t["R"] > 0 for t in closed); losses = sum(t["R"] < 0 for t in closed)
    tot = sum(t["R"] for t in closed)
    msg = (f"📊 <b>خلاصهٔ هفتگی سیگنال‌های طلا</b> ({start:%m-%d} تا {now:%m-%d})\n\n"
           f"• تعداد سیگنال: {len(ts)}\n• بسته‌شده: {len(closed)} (برد: {wins} | باخت: {losses} | سربه‌سر: {len(closed)-wins-losses})\n"
           f"• هنوز باز: {len(ts) - len(closed)}\n• مجموع: <b>{tot:+.1f}R</b>\n\n"
           "(محاسبه: نصف در تارگت ۱، بقیه با حد ضرر روی ورود تا تارگت ۲)")
    if not ts: msg += "\n\nاین هفته سیگنالی با همهٔ فیلترها پیدا نشد."
    say(msg)

if __name__ == "__main__":
    if "--watch" in sys.argv: watch()
    elif "--weekly" in sys.argv: weekly()
    elif "--status" in sys.argv:   # 4H status post; skip when market closed / data stale
        a = analyze()
        if a["age"] > 3600: print(f"data stale ({a['age']:.0f}s), skip")
        else: send_analysis(a); print("SENT OK")
    else:
        a = analyze(); print(a["msg"]); send_analysis(a)
