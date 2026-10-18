#!/usr/bin/env python3
"""Telegram long-polling listener: replies with on-demand gold analysis to the owner chat only."""
import os, sys, time, json, traceback, importlib.util, requests
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != HERE]
spec = importlib.util.spec_from_file_location("gold_signal", os.path.join(HERE, "signal.py"))
gs = importlib.util.module_from_spec(spec); spec.loader.exec_module(gs)
nspec = importlib.util.spec_from_file_location("gold_news", os.path.join(HERE, "news.py"))
news = importlib.util.module_from_spec(nspec); nspec.loader.exec_module(news)
API = f"https://api.telegram.org/bot{gs.TOKEN}"
OWNER = int(gs.CHAT_ID)
ALLOWED = {int(c) for c in gs.CHAT_IDS}
OFFSET = os.path.join(HERE, "listener_offset.json")
CHART = os.path.join(HERE, "chart_reply.png")
HELP = ("🤖 <b>راهنمای ربات طلا</b>\n\n"
        "هر پیامی بفرستی (مثلاً «الان چطوره»، «چک» یا /check) همون لحظه تحلیل طلا رو با چارت برات می‌فرستم.\n\n"
        "/check — تحلیل فوری\n/news یا «تقویم» یا «خبر» — تقویم اقتصادی (امروز و بقیه‌ی هفته)\n/help — همین راهنما\n\n"
        "خودکار هم: سیگنال‌های جدید خرید/فروش (هر ۱۵ دقیقه، دوشنبه تا جمعه) و اخبار مهم هر روز ساعت ۸:۴۰.")

DRY = "--dry" in sys.argv
def send(method, **kw):
    if DRY: print("DRY", method, (kw.get("data") or {}).get("text") or (kw.get("data") or {}).get("caption") or ""); return {"ok": True}
    if method == "sendMessage" and "data" in kw: kw["data"] = {"reply_markup": gs.KB, **kw["data"]}
    return requests.post(f"{API}/{method}", timeout=60, **kw).json()

def reply(chat, text):
    if text.startswith("/help"):
        send("sendMessage", data={"chat_id": chat, "text": HELP, "parse_mode": "HTML"}); return
    low = text.lower()
    if any(k in low for k in ("تقویم", "news", "خبر")):   # covers /news, اخبار
        send("sendMessage", data={"chat_id": chat, "text": news.build_calendar(), "parse_mode": "HTML"}); return
    send("sendChatAction", data={"chat_id": chat, "action": "typing"})
    a = gs.analyze()
    msg = gs.with_structure(a)
    if a["age"] > 3600:
        msg = "🌙 <b>الان بازار طلا بسته‌ست</b> (یا داده تازه نیست)؛ این تحلیل مربوط به آخرین قیمت قبل از تعطیلیه.\n\n" + msg
    if a["age"] <= 3600 and not a.get("session", True):
        msg = "🕐 الان خارج از سشن لندن/نیویورک (۱۰:۰۰ تا ۰۰:۰۰ استانبول) هستیم؛ ربات خودکار این ساعت‌ها سیگنال نمی‌فرسته، این فقط تحلیل لحظه‌ایه.\n\n" + msg
    if text.startswith("/start"):
        msg = "سلام کوروش! 👋\n\n" + msg
    gs.plot(a["d4"], a["d15"], a["lv"], path=CHART)
    with open(CHART, "rb") as f:
        send("sendPhoto", data={"chat_id": chat, "caption": "چارت طلا: ۴ ساعته و ۱۵ دقیقه"}, files={"photo": f})
    send("sendMessage", data={"chat_id": chat, "text": msg, "parse_mode": "HTML"})

def main():
    off = json.load(open(OFFSET))["offset"] if os.path.exists(OFFSET) else 0
    once = "--once" in sys.argv   # GitHub Actions: answer pending messages and exit
    print("listener started" + (" (once)" if once else ""), flush=True)
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": off, "timeout": 0 if once else 50,
                             "allowed_updates": json.dumps(["message"])}, timeout=70).json()
            for u in r.get("result", []):
                off = u["update_id"] + 1
                json.dump({"offset": off}, open(OFFSET, "w"))
                m = u.get("message") or {}
                chat = (m.get("chat") or {}).get("id")
                if chat not in ALLOWED: print(f"ignored chat {chat}", flush=True); continue
                text = (m.get("text") or "").strip()
                print(f"{time.strftime('%F %T')} msg: {text[:40]!r}", flush=True)
                try: reply(chat, text)
                except Exception:
                    traceback.print_exc()
                    send("sendMessage", data={"chat_id": chat, "text": "⚠️ الان نتونستم داده بگیرم، چند دقیقه دیگه دوباره امتحان کن."})
        except Exception:
            traceback.print_exc()
            if once: return
            time.sleep(10)
        if once: return

if __name__ == "__main__":
    if "--reply" in sys.argv:   # GitHub Actions reply.yml: message text comes from the dispatch payload (env TG_TEXT)
        c = int(os.environ.get("TG_CHAT") or OWNER)   # reply to whoever asked (must be an allowed chat)
        reply(c if c in ALLOWED else OWNER, (os.environ.get("TG_TEXT") or "").strip())
    else:
        main()
