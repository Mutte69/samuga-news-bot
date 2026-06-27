"""Telegram, Buffer/Meta publishing and social queue worker."""
import io
import time
import base64
import threading
import requests
from datetime import datetime
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, CORE_TEAM_CHAT_ID, CONTENT_LAB_THREAD_ID,
    ALERT_THREAD_ID, BUFFER_TOKEN, BUFFER_PROFILE_IDS, SOCIAL_POST_GAP_SECONDS,
    SAMUGA_PUBLIC_SOURCE, log, SAMUGA_PUBLIC_LINK
)
from utils import samuga_public_caption, strip_source_links
from db import mark_status, publish_article_for_website, log_learning
from state import save_state

_social_queue = []
_social_lock = threading.Lock()
_last_social_post_time = [None]
_worker_started = [False]


def telegram_api(method, data=None, files=None, timeout=30):
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram token missing")
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", data=data or {}, files=files, timeout=timeout)
        if r.status_code != 200:
            log.warning(f"Telegram {method} HTTP {r.status_code}: {r.text[:250]}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} failed: {e}")
        return None


def send_text(chat_id, text, thread_id=None, disable_preview=True):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": disable_preview}
    if thread_id:
        data["message_thread_id"] = thread_id
    return telegram_api("sendMessage", data=data)


def send_photo(chat_id, img_buf, caption, thread_id=None):
    caption = samuga_public_caption(caption)
    data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id:
        data["message_thread_id"] = thread_id
    if hasattr(img_buf, "seek"):
        img_buf.seek(0)
    files = {"photo": ("samuga.png", img_buf, "image/png")}
    return telegram_api("sendPhoto", data=data, files=files, timeout=45)


def post_to_buffer(img_bytes, caption):
    if not BUFFER_TOKEN or not BUFFER_PROFILE_IDS:
        return False, "Buffer not configured"
    ok_any = False
    for profile_id in BUFFER_PROFILE_IDS:
        try:
            data = {
                "profile_ids[]": profile_id,
                "text": strip_source_links(caption)[:2000],
                "now": "true",
            }
            # Simple text post. Image upload can be added when IMGBB/media hosting is configured.
            r = requests.post("https://api.bufferapp.com/1/updates/create.json", data=data, headers={"Authorization": f"Bearer {BUFFER_TOKEN}"}, timeout=30)
            if r.status_code in (200, 201): ok_any = True
            else: log.warning(f"Buffer post failed {profile_id}: {r.status_code} {r.text[:180]}")
        except Exception as e:
            log.warning(f"Buffer error: {e}")
    return ok_any, "posted" if ok_any else "failed"


def queue_for_social(img_buf, caption, article=None, key_label="Post", post_telegram=True, priority=False):
    img_bytes = img_buf.getvalue() if hasattr(img_buf, "getvalue") else img_buf
    with _social_lock:
        item = {
            "img_bytes": base64.b64encode(img_bytes).decode("ascii"),
            "caption": samuga_public_caption(caption),
            "article_id": article.id if article else None,
            "title": article.title if article else "",
            "cat": article.cat if article else "LOCAL",
            "lang": article.lang if article else "en",
            "key_label": key_label,
            "post_telegram": post_telegram,
            "priority": bool(priority),
            "queued_at": datetime.utcnow().isoformat(),
        }
        if priority:
            _social_queue.insert(0, item)
        else:
            _social_queue.append(item)
    save_state()
    log.info(f"[QUEUE] {key_label} queued for social ({len(_social_queue)} waiting)")


def _publish_social_item(item):
    img_bytes = base64.b64decode(item["img_bytes"])
    buf = io.BytesIO(img_bytes)
    caption = item["caption"]
    tg_id = None
    if item.get("post_telegram", True):
        res = send_photo(TELEGRAM_CHANNEL_ID, buf, caption)
        try:
            tg_id = res.get("result", {}).get("message_id") if res else None
        except Exception:
            tg_id = None
    post_to_buffer(img_bytes, caption)
    if item.get("article_id"):
        mark_status(item["article_id"], "social_posted", posted=True, tg_message_id=tg_id)
    log.info(f"[QUEUE] Posted {item.get('key_label','Post')}")


def social_queue_worker():
    log.info(f"📲 Social queue worker started ({SOCIAL_POST_GAP_SECONDS}s gap between posts)")
    while True:
        try:
            item = None
            with _social_lock:
                if _social_queue:
                    item = _social_queue.pop(0)
            if item:
                _publish_social_item(item)
                _last_social_post_time[0] = datetime.utcnow()
                save_state()
                time.sleep(SOCIAL_POST_GAP_SECONDS)
            else:
                time.sleep(5)
        except Exception as e:
            log.error(f"Social queue worker: {e}")
            time.sleep(20)


def start_social_worker():
    if _worker_started[0]:
        return
    _worker_started[0] = True
    t = threading.Thread(target=social_queue_worker, daemon=True)
    t.start()


def send_approval_card(key, item, alert=False):
    title = item.get("title", "")
    caption = item.get("caption", "")
    img_bytes = base64.b64decode(item["card_b64"])
    buf = io.BytesIO(img_bytes)
    thread_id = ALERT_THREAD_ID if alert else CONTENT_LAB_THREAD_ID
    target_name = "Alert" if alert else "Content Lab"
    controls = f"\n\n<b>Commands:</b> /approve_{key}  /reject_{key}  /post_{key}"
    meta = f"\n<b>Confidence:</b> {item.get('confidence','low')} | <b>Score:</b> {item.get('score',0)} | <b>Lane:</b> {item.get('lane','')}"
    res = send_photo(CORE_TEAM_CHAT_ID, buf, f"<b>{title}</b>{meta}{controls}\n\n{caption[:700]}", thread_id=thread_id)
    log.info(f"📨 Sent {key} preview to {target_name}")
    return res


def public_post_now(article, card_buf, caption, key_label="Post"):
    # Breaking/high confidence: post to Telegram now, Buffer follows 2-min queue to avoid throttling.
    res = send_photo(TELEGRAM_CHANNEL_ID, card_buf, caption)
    tg_id = None
    try: tg_id = res.get("result", {}).get("message_id") if res else None
    except Exception: pass
    mark_status(article.id, "posted", posted=True, tg_message_id=tg_id)
    # Buffer/socials still queue with priority to prevent blocks.
    card_buf.seek(0)
    queue_for_social(card_buf, caption, article=article, key_label=key_label, post_telegram=False, priority=True)
    send_text(CORE_TEAM_CHAT_ID, f"✅ <b>{key_label}</b> posted publicly\n📰 {article.title[:120]}", thread_id=ALERT_THREAD_ID)
