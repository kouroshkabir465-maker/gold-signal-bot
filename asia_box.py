#!/usr/bin/env python3
"""Asian-session box (03:00-10:00 Istanbul) + M15 breakout / sweep alerts.  ISOLATED add-on:
it never changes signal.py logic or state.json; its own state lives in asia.json / asia_prefs.json.
  asia_box.py --watch   -> called by watcher.yml after the main watcher (box at 10:00, then breakout/sweep alerts)
  asia_box.py --reply   -> called by reply.yml for the two Asian-session keyboard buttons (exits fast otherwise)
  add --dry to print instead of sending."""
import os, sys, json, datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path = [x for x in sys.path if os.path.abspath(x or ".") != HERE]   # don't shadow stdlib "signal"
TZ, MT = ZoneInfo("Europe/Istanbul"), ZoneInfo("Europe/Athens")
BOX_START_H, BOX_END_H = 3, 10        # MT5 chart (broker server, Europe/Athens EET/EEST) hours [03:00, 10:00) = 04-11 Istanbul in winter, 03-10 in summer
WIDE_USD = 60.0                       # box wider than this -> warning (backtest Sep 25-Nov 25 2026: median 44.5$, p75 63.1$)
MIN_BOX_BARS = 24                     # of 28 M15 bars; fewer = holiday / data gap -> no box
MIN_BOX_USD = 5.0                     # flatter = synthetic holiday bars (e.g. Dec 25 / Jan 1 / Good Friday) -> no box
MAX_ALERTS_DAY = 4                    # breakout up/down + sweep high/low, at most one of each
STOP_BUF = 1.0                        # $ beyond the sweep wick
MAX_RISK_USD = 15.0                   # user's rule: 0.01 lot risk <= 15$
BRK_RISK_FRAC, BRK_RISK_MIN, BRK_RISK_MAX = 0.2, 4.0, 12.0   # breakout retest stop = 20% of box, clamped 4..12$
STALE_MIN = 60                        # don't alert on a bar that closed more than this many minutes ago
STATE = os.path.join(HERE, "asia.json")
PREFS = os.path.join(HERE, "asia_prefs.json")
BTN_TOGGLE, BTN_BOX = "🌏 سشن آسیا روشن/خاموش", "📦 باکس آسیا امروز"
DRY = "--dry" in sys.argv

def is_asia_text(t):
    t = (t or "").strip()
    return t in (BTN_TOGGLE, BTN_BOX) or t.startswith("/asia") or t in ("سشن آسیا", "باکس آسیا", "باکس")

def load(p, d):
    try: return json.load(open(p)) if os.path.exists(p) else d
    except Exception: return d
def save(p, v):
    if not DRY: json.dump(v, open(p, "w"), ensure_ascii=False, indent=1, default=str)

# ---------------- pure logic (shared with backtest) ----------------
def box_window(day):
    """day: MT5 server date -> (start, end) aware UTC timestamps of the box window (03:00-10:00 chart time)."""
    import pandas as pd
    s = pd.Timestamp(dt.datetime(day.year, day.month, day.day, BOX_START_H, tzinfo=MT)).tz_convert("UTC")
    e = pd.Timestamp(dt.datetime(day.year, day.month, day.day, BOX_END_H, tzinfo=MT)).tz_convert("UTC")
    return s, e

def compute_box(m15, day):
    """Highest high wick / lowest low wick of M15 bars opening in [03:00, 10:00) Istanbul. None if incomplete."""
    s, e = box_window(day)
    w = m15[(m15.index >= s) & (m15.index < e)]
    if len(w) < MIN_BOX_BARS: return None
    hi, lo = float(w.High.max()), float(w.Low.min())
    if hi - lo < MIN_BOX_USD: return None
    return dict(day=str(day), top=hi, bot=lo, size=hi - lo, mid=(hi + lo) / 2, bars=len(w),
                t_top=str(w.High.idxmax()), t_bot=str(w.Low.idxmin()))

def classify(bar, box):
    """One closed M15 bar after 10:00 -> 'brk_up' | 'brk_dn' | 'sw_hi' | 'sw_lo' | None."""
    o, h, l, c, top, bot = bar.Open, bar.High, bar.Low, bar.Close, box["top"], box["bot"]
    if c > top: return "brk_up"                     # closed (body) above the box
    if c < bot: return "brk_dn"
    up, dn = h > top and bot <= o <= top, l < bot and bot <= o <= top   # wick out, opened & closed inside
    if up and dn: return None                       # wick both sides: ambiguous
    return "sw_hi" if up else "sw_lo" if dn else None

def h4_trend_at(d4, t):
    """Ichimoku cloud position of the last CLOSED MT5 H4 bar before time t: +1 above, -1 below, 0 inside."""
    import pandas as pd
    closed = d4[d4.index + pd.Timedelta(hours=4) <= t]
    return int(closed.cloud.iloc[-1]) if len(closed) else 0

def sweep_levels(kind, bar, box):
    e = float(bar.Close)
    if kind == "sw_lo":
        sl = float(bar.Low) - STOP_BUF
        return dict(side=1, entry=e, sl=sl, tp1=box["mid"], tp2=box["top"], risk=e - sl)
    sl = float(bar.High) + STOP_BUF
    return dict(side=-1, entry=e, sl=sl, tp1=box["mid"], tp2=box["bot"], risk=sl - e)

def scan_day(m15, d4, day, now=None):
    """Backtest helper: box + list of (bar_time, kind, info) that would fire for an Istanbul date."""
    import pandas as pd
    box = compute_box(m15, day)
    if not box: return None, []
    _, e = box_window(day)
    end = pd.Timestamp(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), MT)).tz_convert("UTC")
    seen, out = set(), []
    for t, b in m15[(m15.index >= e) & (m15.index < end)].iterrows():
        k = classify(b, box)
        if not k or k in seen or len(seen) >= MAX_ALERTS_DAY: continue
        seen.add(k); out.append((t, k, sweep_levels(k, b, box) if k.startswith("sw") else breakout_levels(k, box), h4_trend_at(d4, t + pd.Timedelta(minutes=15))))
    return box, out

# ---------------- text ----------------
def ist(t): return t.tz_convert(MT).strftime("%H:%M")   # shown in MT5 chart time
def mt_hours(day):
    s, e = box_window(day); return f"{s.tz_convert(TZ):%H:%M} تا {e.tz_convert(TZ):%H:%M}"
TREND = {1: "صعودی (بالای ابر)", -1: "نزولی (زیر ابر)", 0: "خنثی (داخل ابر)"}

def box_text(box, day, late=False):
    import pandas as pd
    m = (f"🌏 <b>باکس سشن آسیا</b> ({day:%m-%d})\n"
         f"⏰ ۰۳:۰۰ تا ۱۰:۰۰ ساعت چارت MT5 (= {mt_hours(day)} استانبول)\n🕒 ساعت‌های این پیام به وقت چارت MT5 هستن.\n\n"
         f"🔺 سقف: <b>{box['top']:.2f}</b> ({ist(pd.Timestamp(box['t_top']))})\n"
         f"🔻 کف: <b>{box['bot']:.2f}</b> ({ist(pd.Timestamp(box['t_bot']))})\n"
         f"➖ وسط: {box['mid']:.2f}\n📏 اندازه: <b>{box['size']:.2f} دلار</b>")
    if box["size"] > WIDE_USD:
        m += f"\n\n⚠️ <b>باکس پهنه</b> (بیشتر از {WIDE_USD:.0f} دلار): استاپ‌ها بزرگ‌ترن؛ ریسک ۰.۰۱ لات رو زیر {MAX_RISK_USD:.0f} دلار نگه دار."
    m += ("\n\n📤 بسته شدن کندل ۱۵ دقیقه بیرون باکس → سیگنال شکست با ورود روی پولبک\n"
          "🔄 شدو بیرون باکس و بسته شدن داخلش → سیگنال سویپ")
    if late: m += "\n(این پیام با تأخیر اومد چون داده دیر رسید.)"
    return m

def align(side, tr):
    if tr == side: return f"✅ هم‌جهت با روند H4: {TREND[tr]}"
    if tr == 0: return f"⚪️ روند H4: {TREND[tr]}"
    return f"❌ خلاف روند H4: {TREND[tr]}"

def breakout_levels(kind, box):
    """Retest plan: limit entry on the broken edge, stop back inside the box, 1R / 2R targets."""
    risk = min(max(BRK_RISK_FRAC * box["size"], BRK_RISK_MIN), BRK_RISK_MAX)
    if kind == "brk_up":
        e = box["top"]; return dict(side=1, entry=e, sl=e - risk, tp1=e + risk, tp2=e + 2 * risk, risk=risk)
    e = box["bot"]; return dict(side=-1, entry=e, sl=e + risk, tp1=e - risk, tp2=e - 2 * risk, risk=risk)

def _lv_lines(lv, t1, t2):
    return (f"🎯 <b>ورود:</b> {lv['entry']:.2f}\n🛑 <b>حد ضرر:</b> {lv['sl']:.2f}\n"
            f"✅ <b>تارگت ۱{t1}:</b> {lv['tp1']:.2f}\n✅ <b>تارگت ۲{t2}:</b> {lv['tp2']:.2f}\n"
            f"📏 ریسک: {lv['risk']:.2f} دلار (۰.۰۱ لات ≈ {lv['risk']:.0f} دلار)")

def alert_text(kind, t, bar, box, lv, tr):
    import pandas as pd
    when = ist(t + pd.Timedelta(minutes=15))
    if kind.startswith("brk"):
        lv = lv or breakout_levels(kind, box); up = lv["side"] > 0
        edge = "سقف" if up else "کف"
        return ("\u200f" + ("🟢 <b>BUY</b>" if up else "🔴 <b>SELL</b>") + f" — شکست {edge} باکس آسیا\n"
                f"کندل ۱۵ دقیقه ({ist(t)}–{when}) {'بالای' if up else 'زیر'} {edge} {lv['entry']:.2f} بسته شد: <b>{bar.Close:.2f}</b>\n"
                f"{align(lv['side'], tr)}\n\n"
                f"📌 <b>نوع سفارش:</b> {'Buy Limit' if up else 'Sell Limit'} روی پولبک به {edge} باکس\n"
                + _lv_lines(lv, " (1R)", " (2R)") +
                f"\n\n❌ ابطال: اگه قبل از پولبک کندل ۱۵ دقیقه دوباره داخل باکس بسته شد، یا تا آخر امروز پر نشد، سفارش رو پاک کن.\n"
                f"باکس: {box['bot']:.2f} – {box['top']:.2f} ({box['size']:.0f} دلار)")
    b = lv["side"] > 0
    m = ("\u200f" + ("🟢 <b>BUY</b>" if b else "🔴 <b>SELL</b>") + f" — سویپ {'کف' if b else 'سقف'} باکس آسیا\n"
         f"کندل ۱۵ دقیقه ({ist(t)}–{when}) شدو زد {'زیر کف' if b else 'بالای سقف'} {box['bot' if b else 'top']:.2f} "
         f"(تا {bar.Low if b else bar.High:.2f}) و داخل باکس بسته شد.\n{align(lv['side'], tr)}\n\n"
         f"📌 <b>نوع سفارش:</b> {'Buy' if b else 'Sell'} Market (قیمت بسته شدن کندل)\n"
         + _lv_lines(lv, " (وسط باکس)", f" ({'سقف' if b else 'کف'} باکس)") +
         f"\n⚖️ ریسک به ریوارد: ۱:{abs(lv['tp1'] - lv['entry']) / lv['risk']:.1f} تا ۱:{abs(lv['tp2'] - lv['entry']) / lv['risk']:.1f}")
    if lv["risk"] > MAX_RISK_USD: m += f"\n⚠️ ریسک بیشتر از {MAX_RISK_USD:.0f} دلاره؛ طبق قانونت حجم رو کم کن یا رد کن."
    if (lv["tp1"] - lv["entry"]) * lv["side"] <= 0: m += "\n⚠️ قیمت از وسط باکس رد شده؛ فقط تارگت ۲ معتبره."
    return m

# ---------------- live ----------------
def _gs():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gold_signal_asia", os.path.join(HERE, "signal.py"))
    g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g); return g

def _data(gs):
    h4, m15, src = gs.get_data()
    if "Twelve Data" not in src: return None, None, src   # Yahoo GC=F futures != broker spot -> never box on it
    return gs.indicators(h4), m15, src

def _post(gs, chat, text):
    if DRY: print(f"[dry] -> {chat}:\n{text}\n"); return True
    import requests
    j = requests.post(f"https://api.telegram.org/bot{gs.TOKEN}/sendMessage", timeout=60, data={
        "chat_id": chat, "text": text, "parse_mode": "HTML", "reply_markup": gs.KB}).json()
    if not j.get("ok"): print(f"asia send failed for a chat: {j.get('description')}")
    return bool(j.get("ok"))

def broadcast(gs, text):
    prefs = load(PREFS, {})
    for c in gs.CHAT_IDS:
        if prefs.get(str(c), True): _post(gs, c, text)
        else: print("asia alerts off for one chat, skipped")

def watch():
    import pandas as pd
    gs = _gs()
    now = pd.Timestamp.now(tz="UTC"); today = now.tz_convert(MT).date()
    if gs.market_closed(now.to_pydatetime()): print("asia: market closed, skip"); return
    _, e = box_window(today)
    if now < e: print("asia: before 10:00 Istanbul, nothing to do"); return
    d4, m15, src = _data(gs)
    if m15 is None: print(f"asia: no Twelve Data ({src}), skip"); return
    st = load(STATE, {})
    if st.get("day") != str(today): st = {"day": str(today), "alerts": [], "last_bar": None}
    closed = m15[m15.index + pd.Timedelta(minutes=15) <= now]
    if not st.get("box"):
        if not len(closed) or closed.index[-1] < e - pd.Timedelta(minutes=15):
            print("asia: 09:45 bar not in data yet, retry next run"); return
        box = compute_box(closed, today)
        if not box: print("asia: box incomplete (holiday / data gap), no box today"); st["box"] = {"none": True}; save(STATE, st); return
        st["box"] = box; save(STATE, st)            # save before send: never double-send
        broadcast(gs, box_text(box, today, late=(now - e).total_seconds() > 3600)); print(f"asia: BOX SENT {box['bot']:.2f}-{box['top']:.2f}")
    box = st["box"]
    if box.get("none"): return
    end = pd.Timestamp(dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), MT)).tz_convert("UTC")
    bars = closed[(closed.index >= e) & (closed.index < end)]
    if st.get("last_bar"): bars = bars[bars.index > pd.Timestamp(st["last_bar"])]
    for t, b in bars.iterrows():
        st["last_bar"] = str(t)
        k = classify(b, box)
        if not k or k in st["alerts"] or len(st["alerts"]) >= MAX_ALERTS_DAY: continue
        st["alerts"].append(k); save(STATE, st)
        if (now - (t + pd.Timedelta(minutes=15))).total_seconds() > STALE_MIN * 60:
            print(f"asia: {k} at {t} too old, recorded without sending"); continue
        tr = h4_trend_at(d4, t + pd.Timedelta(minutes=15))
        broadcast(gs, alert_text(k, t, b, box, sweep_levels(k, b, box) if k.startswith("sw") else breakout_levels(k, box), tr))
        print(f"asia: ALERT {k} bar {t}")
    save(STATE, st)

def reply():
    global _post
    text, chat = (os.environ.get("TG_TEXT") or "").strip(), str(os.environ.get("ASIA_CHAT") or "")
    if not is_asia_text(text): print("asia: not an asia button, skip"); return
    gs = _gs()
    if chat not in [str(c) for c in gs.CHAT_IDS]: chat = str(gs.CHAT_ID)
    if text == BTN_TOGGLE or text.startswith("/asia_o") or text == "سشن آسیا":
        prefs = load(PREFS, {})
        on = (not prefs.get(chat, True)) if not text.startswith("/asia_o") else text.startswith("/asia_on")
        prefs[chat] = on; save(PREFS, prefs)
        _post(gs, chat, "🌏 هشدارهای سشن آسیا (باکس ساعت ۱۰، شکست و سویپ) " + ("<b>روشن</b> شد ✅" if on else "<b>خاموش</b> شد ⛔️\nبرای روشن کردن دوباره همین دکمه رو بزن.")
              + "\nسیگنال‌های اصلی ربات تغییری نمی‌کنن."); return
    import pandas as pd
    now = pd.Timestamp.now(tz="UTC"); today = now.tz_convert(MT).date(); s, e = box_window(today)
    if text == "/asia_test":   # one-off deploy preview, owner only
        _p = _post; _post = lambda g, c, t: _p(g, c, "🧪 <b>پیام تست</b> — قابلیت جدید سشن آسیا نصب شد. این فقط پیش‌نمایشه، سیگنال نیست.\nدکمه‌های جدید پایین صفحه: «📦 باکس آسیا امروز» و «🌏 سشن آسیا روشن/خاموش».\n\n" + t)
    on = load(PREFS, {}).get(chat, True); foot = f"\n\nهشدارهای سشن آسیا برای تو: {'روشن ✅' if on else 'خاموش ⛔️'}"
    if gs.market_closed(now.to_pydatetime()):
        _post(gs, chat, "⛔ بازار طلا الان بسته‌ست؛ باکس آسیا روز معاملاتی بعد ساعت ۱۰:۰۰ چارت MT5 آماده می‌شه." + foot); return
    d4, m15, src = _data(gs)
    if m15 is None: _post(gs, chat, "⚠️ الان دادهٔ دقیق (Twelve Data) در دسترس نیست؛ چند دقیقه دیگه دوباره امتحان کن." + foot); return
    closed = m15[m15.index + pd.Timedelta(minutes=15) <= now]
    if now < e:
        w = closed[closed.index >= s]
        msg = f"⏳ باکس آسیا هنوز آماده نیست؛ ساعت ۱۰:۰۰ چارت MT5 ({e.tz_convert(TZ):%H:%M} استانبول) بسته می‌شه."
        if now >= s and len(w): msg += f"\nتا الان: سقف {w.High.max():.2f} | کف {w.Low.min():.2f} | اندازه {w.High.max() - w.Low.min():.2f} دلار"
        _post(gs, chat, msg + foot); return
    box = compute_box(closed, today)
    if not box: _post(gs, chat, "⚠️ امروز دادهٔ کافی برای ساعت ۰۳ تا ۱۰ نیست (تعطیلی یا قطعی داده)، باکس ساخته نشد." + foot); return
    last = float(closed.Close.iloc[-1])
    pos = "بالای باکس" if last > box["top"] else "زیر باکس" if last < box["bot"] else "داخل باکس"
    st = load(STATE, {}); fired = st.get("alerts", []) if st.get("day") == str(today) else []
    names = {"brk_up": "شکست به بالا", "brk_dn": "شکست به پایین", "sw_hi": "سویپ سقف (SELL)", "sw_lo": "سویپ کف (BUY)"}
    msg = box_text(box, today).rsplit("\n\n📤", 1)[0]
    msg += f"\n\n💰 قیمت الان: <b>{last:.2f}</b> ({pos})\nهشدارهای امروز: " + ("، ".join(names[k] for k in fired) if fired else "هنوز هیچی")
    _post(gs, chat, msg + foot)

if __name__ == "__main__":
    try:
        if "--watch" in sys.argv: watch()
        elif "--reply" in sys.argv: reply()
    except Exception as ex:   # never fail the workflow / never affect main bot
        import traceback; traceback.print_exc(); print(f"asia_box error: {ex}")
