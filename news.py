#!/usr/bin/env python3
"""Daily Farsi Telegram digest of today's high-impact USD events (ForexFactory feed)."""
import os, sys, html, datetime as dt, requests
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Europe/Istanbul")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or open("/workspace/tg_chat_id").read().strip()
URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TOP = ("Non-Farm", "CPI", "FOMC", "Federal Funds", "Powell", "GDP", "PCE", "Unemployment Rate", "Retail Sales", "PPI", "ISM")

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "ff_week.json")
import json, time

def events():
    """Weekly feed, cached 1h (feed is rate-limited). Falls back to stale cache on error."""
    try:
        if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < 3600:
            return json.load(open(CACHE))
        ev = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).json()
        os.makedirs(os.path.dirname(CACHE), exist_ok=True); json.dump(ev, open(CACHE, "w"))
        return ev
    except Exception:
        return json.load(open(CACHE)) if os.path.exists(CACHE) else []

def high_usd():
    """[(datetime Istanbul, event dict, is_top)] for high-impact USD events this week."""
    out = []
    for e in events():
        if e.get("country") != "USD" or e.get("impact") != "High": continue
        t = dt.datetime.fromisoformat(e["date"]).astimezone(TZ)
        out.append((t, e, any(k.lower() in e["title"].lower() for k in TOP)))
    return sorted(out, key=lambda x: x[0])

def blackout(now=None, before=30, after=15):
    """Events whose window [t-before, t+after] contains now."""
    now = now or dt.datetime.now(TZ)
    return [(t, e, top) for t, e, top in high_usd() if t - dt.timedelta(minutes=before) <= now <= t + dt.timedelta(minutes=after)]

INVERTED = ("unemployment", "jobless", "claims")
SPEECH = ("speaks", "speech", "fomc", "press conference", "minutes", "testifies", "statement")
CAVEAT = "ℹ️ این‌ها سناریوی معمول‌ان؛ بازار گاهی برعکس واکنش نشون می‌ده."

def kind(title):
    t = title.lower()
    if any(k in t for k in ("federal funds", "rate decision")): return "normal"
    if any(k in t for k in SPEECH): return "speech"
    return "inverted" if any(k in t for k in INVERTED) else "normal"

def scenario(title):
    k = kind(title)
    if k == "speech": return "🦅 لحن تند (هاوکیش) ← دلار قوی ← طلا احتمالاً نزولی 📉 | 🕊 لحن نرم (داویش) ← طلا احتمالاً صعودی 📈"
    if k == "inverted": return "📊 واقعی بیشتر از پیش‌بینی ← بد برای دلار ← طلا احتمالاً صعودی 📈 | کمتر ← طلا احتمالاً نزولی 📉"
    return "📊 واقعی بیشتر از پیش‌بینی ← دلار قوی‌تر ← طلا احتمالاً نزولی 📉 | کمتر ← طلا احتمالاً صعودی 📈"

def _num(x):
    try: return float(re.sub(r"[^0-9.\-]", "", x)) if x else None
    except ValueError: return None

def impact(title, actual, forecast):
    """+1 gold bullish, -1 bearish, 0 in line/unknown."""
    if actual is None or forecast is None or kind(title) == "speech" or actual == forecast: return 0
    usd = 1 if actual > forecast else -1
    if kind(title) == "inverted": usd = -usd
    return -usd

TV_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "tv_cal.json")
def tv_events():
    """Actuals from TradingView's public economic-calendar endpoint (unofficial; cached 4 min)."""
    try:
        if os.path.exists(TV_CACHE) and time.time() - os.path.getmtime(TV_CACHE) < 240: return json.load(open(TV_CACHE))
        now = dt.datetime.now(dt.timezone.utc)
        r = requests.get("https://economic-calendar.tradingview.com/events", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com"},
                         params={"from": (now - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                 "to": (now + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "countries": "US"}).json()["result"]
        json.dump(r, open(TV_CACHE, "w")); return r
    except Exception as ex:
        print("tradingview calendar failed:", ex, file=sys.stderr)
        return json.load(open(TV_CACHE)) if os.path.exists(TV_CACHE) else []

import re
def _norm(t):
    t = t.lower().replace("m/m", "mom").replace("y/y", "yoy").replace("q/q", "qoq")
    return set(re.findall(r"[a-z0-9]+", t)) - {"us", "the", "of", "s"}

ALIAS = {"core retail sales": "retail sales ex autos", "unemployment claims": "initial jobless claims",
         "non-farm employment change": "nonfarm payrolls", "adp non-farm employment change": "adp nonfarm employment change",
         "prelim gdp": "gdp growth rate", "advance gdp": "gdp growth rate", "final gdp": "gdp growth rate",
         "ism manufacturing pmi": "ism manufacturing pmi", "ism services pmi": "ism non manufacturing pmi",
         "jolts job openings": "jolts job openings", "cb consumer confidence": "cb consumer confidence"}

def find_actual(title, when):
    """Match FF event to TradingView event at same time; -> dict(actual, forecast, unit) or None."""
    tl = title.lower()
    for k, v in ALIAS.items():
        if tl.startswith(k): tl = tl.replace(k, v, 1); break
    a = _norm(tl); best, score = None, 0
    for e in tv_events():
        try: t = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        except Exception: continue
        if abs((t - when).total_seconds()) > 120 or e.get("actual") is None: continue
        b = _norm(e["title"])
        j = (2 - len(b - a) / 10) if a <= b else len(a & b) / max(1, len(a | b))  # all FF words present -> prefer fewest extras
        if ("core" in a) != ("core" in b): j -= 0.5
        if j > score: best, score = e, j
    if best and score >= 0.5:
        return dict(actual=best["actual"], forecast=best.get("forecast"), unit=best.get("unit") or "", tv_title=best["title"])
    return None

def build():
    ev = events()
    today = dt.datetime.now(TZ).date()
    rows = []
    for e in ev:
        if e.get("country") != "USD" or e.get("impact") != "High": continue
        t = dt.datetime.fromisoformat(e["date"]).astimezone(TZ)
        if t.date() == today: rows.append((t, e))
    rows.sort(key=lambda x: x[0])
    head = f"📰 <b>اخبار مهم اقتصادی امروز ({today:%Y-%m-%d})</b>\nفقط اخبار پراهمیت دلار آمریکا که روی طلا اثر دارن (ساعت‌ها به وقت استانبول)\n\n"
    if not rows:
        body = "✅ امروز خبر پراهمیتی برای دلار نداریم. بازار احتمالاً آروم‌تره، ولی همیشه مراقب باش."
    else:
        lines = []
        for t, e in rows:
            star = "🔥🔥 " if any(k.lower() in e["title"].lower() for k in TOP) else "🔴 "
            fp = []
            if e.get("forecast"): fp.append(f"پیش‌بینی: {html.escape(e['forecast'])}")
            if e.get("previous"): fp.append(f"قبلی: {html.escape(e['previous'])}")
            lines.append(f"{star}<b>{t:%H:%M}</b> — {html.escape(e['title'])}" + (f"\n      {' | '.join(fp)}" if fp else ""))
        body = "\n".join(lines) + "\n\n🔥🔥 = خیلی مهم (معمولاً نوسان شدید طلا)\n" + "⚠️ حوالی زمان این اخبار با احتیاط معامله کن."
    return head + body

FA_DAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]

def _line(t, e, day=False):
    star = "🔥🔥 " if any(k.lower() in e["title"].lower() for k in TOP) else "🔴 "
    when = (f"{FA_DAYS[t.weekday()]} " if day else "") + f"{t:%H:%M}"
    fp = []
    if e.get("forecast"): fp.append(f"پیش‌بینی: {html.escape(e['forecast'])}")
    if e.get("previous"): fp.append(f"قبلی: {html.escape(e['previous'])}")
    return f"{star}<b>{when}</b> — {html.escape(e['title'])}" + (f"\n      {' | '.join(fp)}" if fp else "")

def build_calendar():
    """On-demand calendar: today's high-impact USD news + rest of this week (next trading day if today empty)."""
    now = dt.datetime.now(TZ); today = now.date()
    rows = high_usd()
    todays = [(t, e) for t, e, _ in rows if t.date() == today]
    later = [(t, e) for t, e, _ in rows if t.date() > today]
    out = [f"🗓 <b>تقویم اقتصادی — امروز {FA_DAYS[today.weekday()]} {today:%Y-%m-%d}</b>",
           "فقط اخبار پراهمیت دلار که روی طلا اثر دارن (ساعت استانبول)", ""]
    if todays:
        out += [_line(t, e) for t, e in todays]
    else:
        out.append("✅ امروز" + (" (آخر هفته‌ست)" if today.weekday() >= 5 else "") + " خبر پراهمیتی نداریم.")
        if later:
            d = later[0][0].date()
            out += ["", f"📌 <b>روز معاملاتی بعدی با خبر مهم: {FA_DAYS[d.weekday()]} {d:%m-%d}</b>"]
            out += [_line(t, e) for t, e in later if t.date() == d]
            later = [(t, e) for t, e in later if t.date() != d]
    if later:
        out += ["", "📅 <b>بقیه‌ی هفته:</b>"] + [_line(t, e, day=True) for t, e in later]
    if not later and today.weekday() >= 4:
        out += ["", "ℹ️ تقویم هفته‌ی بعد معمولاً آخر هفته منتشر میشه؛ دوشنبه دوباره «تقویم» بزن."]
    out += ["", "🔥🔥 = خیلی مهم | ⚠️ حوالی این ساعت‌ها با احتیاط معامله کن."]
    return "\n".join(out)

def main():
    msg = build()
    print(msg)
    if "--dry" in sys.argv: return
    j = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=30,
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}).json()
    if not j.get("ok"): raise SystemExit(f"Telegram failed: {j.get('description')}")
    print("SENT OK")

if __name__ == "__main__": main()
