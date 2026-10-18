// Telegram webhook -> instant ack + GitHub repository_dispatch (reply.yml)
const OWNER = 8561650547;
const EXTRA = [];                      // other allowed chats (user's second account), filled after capture
const CAPTURE_USER = "younesi1233";    // unknown chat with this username -> logged via market-scanner chat_seen.yml
const REPO = "kouroshkabir465-maker/gold-signal-bot";

export default {
  async fetch(req, env, ctx) {
    if (req.method !== "POST") return new Response("ok");
    if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET)
      return new Response("forbidden", { status: 403 });
    let u; try { u = await req.json(); } catch { return new Response("bad", { status: 400 }); }
    const m = u.message || u.edited_message;
    if (!m || !m.chat) return new Response("ignored");
    const chat = m.chat.id;
    const tg = (method, body) => fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
      { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (chat !== OWNER && !EXTRA.includes(chat)) {
      if ((m.from?.username || "").toLowerCase() === CAPTURE_USER) ctx.waitUntil(Promise.all([
        fetch("https://api.github.com/repos/kouroshkabir465-maker/market-scanner/dispatches", { method: "POST",
          headers: { authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "gold-bot-worker", "content-type": "application/json" },
          body: JSON.stringify({ event_type: "chat_seen", client_payload: { bot: "gold", chat, username: m.from.username } }) }),
        tg("sendMessage", { chat_id: chat, text: "✅ پیامت رسید؛ به‌زودی این حساب هم فعال میشه." })]));
      return new Response("ignored");
    }
    const text = (m.text || "").slice(0, 500);
    const ack = tg("sendMessage", { chat_id: chat, text: "در حال بررسی... ⏳", reply_parameters: { message_id: m.message_id, allow_sending_without_reply: true } });
    const gh = fetch(`https://api.github.com/repos/${REPO}/dispatches`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json",
                 "user-agent": "gold-bot-worker", "content-type": "application/json" },
      body: JSON.stringify({ event_type: "tg_message", client_payload: { text, chat, update_id: u.update_id } }),
    }).then(async r => {
      if (r.status >= 300) await tg("sendMessage", { chat_id: chat, text: "⚠️ اجرای تحلیل شروع نشد، چند دقیقه دیگه دوباره امتحان کن." });
    });
    ctx.waitUntil(Promise.all([ack, gh]));   // return 200 to Telegram right away
    return new Response("ok");
  },
};
