"""JSON state persistence for Railway volumes."""
import os
import json
import threading
from config import SEEN_FILE, STATE_FILE, log

_state_lock = threading.Lock()
seen_articles = set()
approval_queue = {}
approval_counter = [0]
poll_offset = [0]
recent_posts = []
analytics = {"posted": 0, "skipped": 0, "website": 0}


def load_seen():
    global seen_articles
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                seen_articles = set(json.load(f))
    except Exception as e:
        log.error(f"load_seen: {e}")
        seen_articles = set()
    log.info(f"📚 Loaded {len(seen_articles)} seen articles")
    return seen_articles


def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_articles)[-5000:], f)
    except Exception as e:
        log.error(f"save_seen: {e}")


def mark_seen(article_id):
    if article_id:
        seen_articles.add(article_id)
        if len(seen_articles) % 25 == 0:
            save_seen()


def is_seen(article_id):
    return article_id in seen_articles


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"load_state: {e}")
        return {}


def save_state():
    try:
        with _state_lock:
            data = {
                "approval_queue": approval_queue,
                "approval_counter": approval_counter[0],
                "poll_offset": poll_offset[0],
                "recent_posts": recent_posts[-80:],
                "analytics": analytics,
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            os.replace(tmp, STATE_FILE)
            save_seen()
    except Exception as e:
        log.error(f"save_state: {e}")


def restore_state():
    data = load_state()
    if not data:
        log.info("📦 No saved state — starting fresh")
        load_seen()
        return
    approval_queue.clear(); approval_queue.update(data.get("approval_queue") or {})
    approval_counter[0] = int(data.get("approval_counter") or 0)
    poll_offset[0] = int(data.get("poll_offset") or 0)
    recent_posts.clear(); recent_posts.extend(data.get("recent_posts") or [])
    analytics.update(data.get("analytics") or {})
    load_seen()
    log.info(f"📦 State restored: {len(approval_queue)} pending cards, {len(recent_posts)} recent posts")


def store_pending_approval(item):
    approval_counter[0] += 1
    prefix = "dv" if item.get("lang") == "dv" else "en"
    key = f"{prefix}{approval_counter[0]}"
    item["key"] = key
    approval_queue[key] = item
    save_state()
    return key


def pop_pending(key):
    item = approval_queue.pop(key, None)
    save_state()
    return item


def get_pending(key):
    return approval_queue.get(key)
