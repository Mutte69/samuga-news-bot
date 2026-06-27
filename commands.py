"""Telegram command handlers, public bot chat, Content Lab approvals."""
import time
import io
import base64
import requests
from config import (
    TELEGRAM_BOT_TOKEN, BOT_USERNAME, CORE_TEAM_CHAT_ID, CONTENT_LAB_THREAD_ID, ALERT_THREAD_ID,
    log, SAMUGA_VERSION
)
from state import poll_offset, save_state, approval_queue, pop_pending
from publishing import send_text, queue_for_social, send_photo
from db import get_source_health, public_interest, mark_status, log_learning
from brain import public_samuga_ai_chat
from models import Article


def _is_core_chat(chat_id):
    return str(chat_id) == str(CORE_TEAM_CHAT_ID)


def _message_text(update):
    msg = update.get("message") or update.get("edited_message") or {}
    return msg, (msg.get("text") or msg.get("caption") or "").strip()


def cmd_diag(chat_id, thread_id=None):
    health = get_source_health(limit=40)
    lines = [f"🔎 <b>Samuga AI Diagnostics</b>", f"Version: <b>{SAMUGA_VERSION}</b>", ""]
    lines.append("<b>Source ladder health:</b>")
    if not health:
        lines.append("No source health yet. Wait for next scan.")
    else:
        for source, method, ok, count, note, checked_at in health[:20]:
            icon = "✅" if ok else "❌"
            lines.append(f"{icon} {source} / {method}: {count} item(s) — {note or ''}")
    lines.append("")
    lines.append(f"📋 Approval queue: {len(approval_queue)} waiting")
    interests = public_interest(days=1, limit=8)
    if interests:
        lines.append("\n<b>Public interest today:</b>")
        for topic, platform, count in interests:
            lines.append(f"• {topic} ({platform}): {count}")
    send_text(chat_id, "\n".join(lines)[:3900], thread_id=thread_id)


def cmd_help(chat_id, thread_id=None):
    send_text(chat_id, """<b>Samuga AI Commands</b>
/diag — source health + queue
/approve_KEY — approve pending card
/reject_KEY — reject pending card
/post_KEY — post immediately
/interest — public Samuga AI topic trends
/help — this guide""", thread_id=thread_id)


def approve_key(key, chat_id, thread_id=None, member="team"):
    item = pop_pending(key)
    if not item:
        send_text(chat_id, f"No pending card found for <b>{key}</b>", thread_id=thread_id); return
    art = Article.from_dict({"id": item["article_id"], "title": item["title"], "summary": item.get("summary",""), "cat": item.get("cat","LOCAL"), "lang": item.get("lang","en"), "score": item.get("score",0), "confidence": item.get("confidence","medium"), "is_breaking": item.get("is_breaking",False)})
    buf = io.BytesIO(base64.b64decode(item["card_b64"])); buf.seek(0)
    queue_for_social(buf, item["caption"], article=art, key_label=f"{key.upper()} approved", post_telegram=True, priority=bool(item.get("is_breaking")))
    mark_status(art.id, "queued")
    log_learning(art.id, "approved", member=member, category=art.cat, score=art.score, final_caption=item.get("caption",""), lang=art.lang)
    send_text(chat_id, f"✅ <b>{key.upper()}</b> approved and queued\n📰 {art.title[:100]}", thread_id=thread_id)


def post_key(key, chat_id, thread_id=None, member="team"):
    item = pop_pending(key)
    if not item:
        send_text(chat_id, f"No pending card found for <b>{key}</b>", thread_id=thread_id); return
    art = Article.from_dict({"id": item["article_id"], "title": item["title"], "summary": item.get("summary",""), "cat": item.get("cat","LOCAL"), "lang": item.get("lang","en"), "score": item.get("score",0), "confidence": item.get("confidence","medium"), "is_breaking": item.get("is_breaking",False)})
    buf = io.BytesIO(base64.b64decode(item["card_b64"])); buf.seek(0)
    send_photo("@samugacommunity", buf, item["caption"])
    mark_status(art.id, "posted", posted=True)
    log_learning(art.id, "posted_manual", member=member, category=art.cat, score=art.score, final_caption=item.get("caption",""), lang=art.lang)
    send_text(chat_id, f"🚀 <b>{key.upper()}</b> posted now\n📰 {art.title[:100]}", thread_id=thread_id)


def reject_key(key, chat_id, thread_id=None, member="team"):
    item = pop_pending(key)
    if not item:
        send_text(chat_id, f"No pending card found for <b>{key}</b>", thread_id=thread_id); return
    mark_status(item.get("article_id"), "rejected")
    log_learning(item.get("article_id"), "rejected", member=member, category=item.get("cat",""), score=item.get("score",0), original_caption=item.get("caption",""), lang=item.get("lang","en"))
    send_text(chat_id, f"🗑️ <b>{key.upper()}</b> rejected\n📰 {item.get('title','')[:100]}", thread_id=thread_id)


def cmd_interest(chat_id, thread_id=None):
    rows = public_interest(days=1, limit=20)
    if not rows:
        send_text(chat_id, "No public chat interest data yet.", thread_id=thread_id); return
    lines = ["📊 <b>What people are asking Samuga AI today</b>"]
    for topic, platform, count in rows:
        lines.append(f"• {topic} / {platform}: <b>{count}</b>")
    send_text(chat_id, "\n".join(lines), thread_id=thread_id)


def process_update(update):
    msg, text = _message_text(update)
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    thread_id = msg.get("message_thread_id")
    user = msg.get("from", {})
    user_key = str(user.get("id") or "anon")
    member = user.get("first_name") or user.get("username") or "team"
    lower = text.lower().strip()

    if not text:
        return
    if lower.startswith("/diag"):
        cmd_diag(chat_id, thread_id); return
    if lower.startswith("/help"):
        cmd_help(chat_id, thread_id); return
    if lower.startswith("/interest"):
        cmd_interest(chat_id, thread_id); return

    import re
    m = re.match(r"/(approve|reject|post)_([a-z]+\d+)", lower)
    if m:
        action, key = m.group(1), m.group(2)
        if action == "approve": approve_key(key, chat_id, thread_id, member)
        elif action == "reject": reject_key(key, chat_id, thread_id, member)
        elif action == "post": post_key(key, chat_id, thread_id, member)
        return

    # Public chat: DMs, group mentions, or the dedicated public bot.
    bot_mention = f"@{BOT_USERNAME}".lower()
    is_dm = chat.get("type") == "private"
    mentioned = bot_mention in lower or lower.startswith("samuga ai")
    if is_dm or mentioned:
        clean = text.replace(bot_mention, "").strip()
        res = public_samuga_ai_chat(clean, platform="telegram", session_id=f"tg:{chat_id}:{user_key}", user_key=user_key)
        send_text(chat_id, res["reply"], thread_id=thread_id)


def handle_updates():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram bot token missing; Telegram listener not started")
        return
    offset = poll_offset[0]
    log.info(f"💬 Chat listening for @{BOT_USERNAME}... (offset={offset})")
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            if r.status_code != 200:
                time.sleep(5); continue
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                poll_offset[0] = offset
                process_update(update)
            save_state()
        except Exception as e:
            log.error(f"handle_updates: {e}")
            time.sleep(5)


def start_telegram_listener():
    import threading
    t = threading.Thread(target=handle_updates, daemon=True)
    t.start()
